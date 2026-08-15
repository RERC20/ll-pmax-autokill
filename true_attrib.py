# -*- coding: utf-8 -*-
"""Owner-method TRUE ROAS per campaign per day since the Champions pause:
  - product sold BEFORE Aug 7  -> it was already a Winner -> revenue = Winners
  - FIRST-ever sale in window  -> credit the testing campaign that found it:
        CM2 tag  -> Testing | AW      US/CM/other -> Testing | UK
"""
import sys, requests, time, datetime, collections
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import google_ads_connect as ga
from kill_engine_v4 import shopify_token, SHOP, SHOP_API
from zoneinfo import ZoneInfo
UK = ZoneInfo("Europe/London")

START = datetime.date(2026, 8, 7)
today = datetime.datetime.now(UK).date()

# ---- Google spend per campaign per day ----
tok = ga.get_access_token(); H = ga._headers(tok)
def search(q):
    for a in range(5):
        r = requests.post(f"{ga.ADS_BASE}/customers/{ga.CUSTOMER_ID}/googleAds:search",
                          headers=H, json={"query": q}, timeout=60)
        if r.status_code == 200: return r.json().get("results", [])
        time.sleep(4 * (a + 1))
    r.raise_for_status()
CAMPS = {"24027270949": "Testing | UK", "24116871559": "Testing | AW", "23620737018": "Winners"}
spend = collections.defaultdict(lambda: collections.defaultdict(float))
for r in search(f"""SELECT campaign.id, segments.date, metrics.cost_micros FROM campaign
  WHERE campaign.id IN ({','.join(CAMPS)}) AND segments.date BETWEEN '{START}' AND '{today}'"""):
    spend[r["segments"]["date"]][CAMPS[str(r["campaign"]["id"])]] += int(r["metrics"].get("costMicros", 0)) / 1e6

# ---- Shopify: lifetime line items (first-sale detection) ----
stok = shopify_token()
def gql(q, v=None):
    return requests.post(f"https://{SHOP}/admin/api/{SHOP_API}/graphql.json",
                         headers={"X-Shopify-Access-Token": stok, "Content-Type": "application/json"},
                         json={"query": q, "variables": v or {}}, timeout=90).json()
Q = ('query($c:String){orders(first:100,after:$c,query:"created_at:>=2026-01-01 -status:cancelled"){'
     'pageInfo{hasNextPage endCursor} edges{node{createdAt lineItems(first:60){edges{node{'
     'product{legacyResourceId} discountedTotalSet{shopMoney{amount}}}}}}}}}')
first_sale = {}                                   # pid -> earliest date
win_lines = []                                    # (date, pid, amt) in window
cur = None
while True:
    d = gql(Q, {"c": cur})["data"]["orders"]
    for e in d["edges"]:
        dt = datetime.datetime.fromisoformat(e["node"]["createdAt"].replace("Z", "+00:00")).astimezone(UK).date()
        for le in e["node"]["lineItems"]["edges"]:
            p = le["node"].get("product") or {}
            pid = str(p.get("legacyResourceId"))
            if pid == "None": continue
            amt = float(le["node"]["discountedTotalSet"]["shopMoney"]["amount"])
            if pid not in first_sale or dt < first_sale[pid]:
                first_sale.setdefault(pid, dt)
                if dt < first_sale[pid]: first_sale[pid] = dt
            if dt >= START:
                win_lines.append((dt, pid, amt))
    if not d["pageInfo"]["hasNextPage"]: break
    cur = d["pageInfo"]["endCursor"]

# tags for window-sold pids
pids = sorted({pid for _, pid, _ in win_lines})
tags = {}
for i in range(0, len(pids), 50):
    d = gql('query($ids:[ID!]!){nodes(ids:$ids){... on Product{legacyResourceId tags}}}',
            {"ids": [f"gid://shopify/Product/{p}" for p in pids[i:i+50]]})["data"]["nodes"]
    for n in d:
        if n: tags[str(n["legacyResourceId"])] = [str(t).strip().lower() for t in n["tags"]]

def bucket(pid, dt):
    fs = first_sale.get(pid)
    if fs and fs < dt:                       # sold before this day (incl. pre-window) -> Winner
        return "Winners"
    t = tags.get(pid, [])
    if "cm2" in t: return "Testing | AW"
    return "Testing | UK"

rev = collections.defaultdict(lambda: collections.defaultdict(float))
for dt, pid, amt in win_lines:
    rev[dt.isoformat()][bucket(pid, dt)] += amt

names = ["Testing | UK", "Testing | AW", "Winners"]
print(f"owner-method attribution | window {START} -> {today} | sold pids {len(pids)}")
print(f"\n{'day':12}" + "".join(f"{n:>26}" for n in names))
tot_s = collections.defaultdict(float); tot_r = collections.defaultdict(float)
for dt in sorted(set(list(spend.keys()) + list(rev.keys()))):
    cells = []
    for n in names:
        s = spend[dt][n]; rv = rev[dt][n]
        tot_s[n] += s; tot_r[n] += rv
        cells.append(f"£{s:6.2f}/£{rv:7.2f}={rv/s if s else 0:5.2f}")
    print(f"{dt:12}" + "".join(f"{c:>26}" for c in cells))
print(f"\n{'TOTAL':12}" + "".join(
    f"{'£%.2f/£%.2f=%.2f' % (tot_s[n], tot_r[n], (tot_r[n]/tot_s[n] if tot_s[n] else 0)):>26}" for n in names))
gs = sum(tot_s.values()); gr = sum(tot_r.values())
print(f"BLENDED: spend £{gs:.2f} | true rev £{gr:.2f} | TRUE ROAS {gr/gs if gs else 0:.2f}")
print("(cells: spend / true revenue = TRUE ROAS)")
