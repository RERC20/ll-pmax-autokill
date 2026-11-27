# surf/gads.py - Google Ads reads and the ONE write the surf scaler may make (campaign_budget.amount_micros of the
# Testing budget). Reuses the stores-monitor hourly.py Ads client (GADS_* variables). Never prints credentials.
import datetime as dt
import os

import requests

from . import hourly
from .rules import NY, UTC

CID = (os.environ.get("GADS_CUSTOMER_ID") or "7783704709").replace("-", "")
CAMPAIGN_ID = os.environ.get("SURF_CAMPAIGN_ID", "24425197606")
BUDGET_ID = os.environ.get("SURF_BUDGET_ID", "15991878680")
BUDGET_RN = f"customers/{CID}/campaignBudgets/{BUDGET_ID}"


def ads():
    return hourly.ADS


def campaign(a=None):
    a = a or ads()
    r = a.search("SELECT campaign.id, campaign.name, campaign.status, campaign.serving_status, campaign.campaign_budget, "
                 "campaign_budget.resource_name, campaign_budget.amount_micros, campaign_budget.explicitly_shared, "
                 f"campaign_budget.period FROM campaign WHERE campaign.id = {int(CAMPAIGN_ID)}")
    if not r:
        raise RuntimeError(f"campaign {CAMPAIGN_ID} not found")
    c, b = r[0]["campaign"], r[0]["campaignBudget"]
    return {"id": c["id"], "name": c.get("name"), "status": c.get("status"), "serving_status": c.get("servingStatus"),
            "budget_rn": c.get("campaignBudget"), "amount_micros": int(b.get("amountMicros", 0)),
            "explicitly_shared": bool(b.get("explicitlyShared")), "period": b.get("period")}


def schedule_rows(a=None):
    a = a or ads()
    return a.search("SELECT campaign_criterion.criterion_id, campaign_criterion.negative, "
                    "campaign_criterion.ad_schedule.day_of_week, campaign_criterion.ad_schedule.start_hour, "
                    "campaign_criterion.ad_schedule.start_minute, campaign_criterion.ad_schedule.end_hour, "
                    "campaign_criterion.ad_schedule.end_minute FROM campaign_criterion "
                    f"WHERE campaign.id = {int(CAMPAIGN_ID)} AND campaign_criterion.type = AD_SCHEDULE "
                    "AND campaign_criterion.status != REMOVED")


def budget_events(now_utc, a=None):
    a = a or ads()
    lo = (now_utc - dt.timedelta(hours=3)).astimezone(NY).strftime("%Y-%m-%d %H:%M:%S")
    hi = (now_utc + dt.timedelta(hours=1)).astimezone(NY).strftime("%Y-%m-%d %H:%M:%S")
    return a.search("SELECT change_event.change_date_time, change_event.client_type, change_event.change_resource_name, "
                    "change_event.changed_fields, change_event.old_resource, change_event.new_resource "
                    f"FROM change_event WHERE change_event.change_date_time BETWEEN '{lo}' AND '{hi}' "
                    "AND change_event.change_resource_type = 'CAMPAIGN_BUDGET' "
                    f"AND change_event.change_resource_name = '{BUDGET_RN}' "
                    "ORDER BY change_event.change_date_time DESC LIMIT 50")


def ny_day_spend(ny_date, a=None):
    """Testing spend (USD) so far in account day ny_date."""
    a = a or ads()
    r = a.search(f"SELECT metrics.cost_micros FROM campaign WHERE campaign.id = {int(CAMPAIGN_ID)} "
                 f"AND segments.date = '{ny_date}'")
    return round(sum(int(x.get("metrics", {}).get("costMicros", 0)) for x in r) / 1e6, 2)


def set_budget(micros, a=None):
    """The only write: campaign_budget.amount_micros of BUDGET_RN."""
    a = a or ads()
    micros = int(micros)
    if micros % 10000:
        raise ValueError("amount must be a whole cent")
    h = {"Authorization": "Bearer " + a.token(), "developer-token": hourly._env("GADS_DEVELOPER_TOKEN")}
    if a.login():
        h["login-customer-id"] = a.login()
    body = {"operations": [{"update": {"resourceName": BUDGET_RN, "amountMicros": str(micros)},
                            "updateMask": "amount_micros"}]}
    r = requests.post(f"https://googleads.googleapis.com/{hourly.ADS_V}/customers/{CID}/campaignBudgets:mutate",
                      headers=h, json=body, timeout=60)
    if r.status_code != 200:
        try:
            msg = r.json().get("error", {}).get("message", "")
        except Exception:
            msg = ""
        raise RuntimeError(f"budget mutate HTTP {r.status_code}: {msg[:200]}")
    return dt.datetime.now(UTC)
