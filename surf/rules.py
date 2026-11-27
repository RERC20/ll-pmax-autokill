# surf/rules.py - the owner-approved "surf scaler" rules for PMax | Testing | UK (pure functions, no network).
#
# Time zones: the scaler thinks in UK hours/days (Europe/London); the Google Ads account (and so the DAILY BUDGET,
# the ad schedule and segments.date) is New York (America/New_York). Offsets are always whole hours, so every UK
# hour is exactly one New York hour.
import copy
import datetime as dt
import math
import os
from dataclasses import dataclass, fields
from zoneinfo import ZoneInfo

UK = ZoneInfo("Europe/London")
NY = ZoneInfo("America/New_York")
UTC = dt.timezone.utc
DOW = ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY"]
MINUTE = {"ZERO": 0, "FIFTEEN": 15, "THIRTY": 30, "FORTY_FIVE": 45}


@dataclass
class Cfg:
    base: float = 300.0          # base daily budget (USD)
    cap: float = 1500.0          # hard cap of the ladder
    first_mult: float = 2.0      # first step from base: x2
    step_mult: float = 1.5       # later steps up / one notch down
    up_roas: float = 2.2         # hour ROAS needed to step up
    day_roas: float = 2.2        # day-to-date ROAS needed to step up (and the day guard)
    down_roas: float = 1.75      # hour ROAS below this -> one notch down
    hard_roas: float = 1.2       # hour ROAS below this -> base pace for the rest of the day
    pace_share: float = 0.7      # hour spend must be >= 70% of budget / run hours of the day
    buffer: float = 0.10         # floor buffer
    max_changes: int = 8         # budget changes per UK day (resets included)
    max_budget: float = 1500.0   # never write a budget above this

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        kw = {}
        for f in fields(cls):
            v = env.get("SURF_" + f.name.upper())
            if v not in (None, ""):
                kw[f.name] = type(f.default)(float(v)) if isinstance(f.default, int) else float(v)
        c = cls(**kw)
        c.cap = min(c.cap, c.max_budget)
        return c


# ------------------------------------------------------------------ ad schedule
def parse_schedule(rows):
    """AD_SCHEDULE campaign_criterion rows (REST JSON) -> {DAY: [(start_min, end_min), ...]} in account (NY) time.
    None = no ad schedule = runs all day."""
    out = {}
    for r in rows or []:
        c = r.get("campaignCriterion", r)
        if c.get("negative"):
            continue
        a = c.get("adSchedule") or {}
        if not a.get("dayOfWeek"):
            continue
        s = int(a.get("startHour", 0) or 0) * 60 + MINUTE.get(a.get("startMinute", "ZERO"), 0)
        e = int(a.get("endHour", 24) or 0) * 60 + MINUTE.get(a.get("endMinute", "ZERO"), 0)
        out.setdefault(a["dayOfWeek"], []).append((s, e))
    return {k: sorted(v) for k, v in out.items()} or None


def ny_hour_runs(ny_t, sched):
    """Does the New York hour starting at ny_t fall inside the ad schedule? (hour start inside a slot)"""
    if sched is None:
        return True
    m = ny_t.hour * 60
    return any(s <= m < e for s, e in sched.get(DOW[ny_t.weekday()], []))


def _hours_of(d, tz):
    """Every real hour of local date d in tz, as UTC instants (23 / 25 on DST change days)."""
    t = dt.datetime(d.year, d.month, d.day, tzinfo=tz).astimezone(UTC)
    while t.astimezone(tz).date() == d:
        yield t
        t += dt.timedelta(hours=1)


def uk_run_hours(d, sched):
    """UK hour labels of UK date d in which Testing runs (schedule read in NY time, converted)."""
    out = []
    for t in _hours_of(d, UK):
        h = t.astimezone(UK).hour
        if ny_hour_runs(t.astimezone(NY), sched) and h not in out:
            out.append(h)
    return out


def ny_run_hours(d, sched):
    """NY hour labels of NY (account) date d in which Testing runs."""
    out = []
    for t in _hours_of(d, NY):
        h = t.astimezone(NY).hour
        if ny_hour_runs(t.astimezone(NY), sched) and h not in out:
            out.append(h)
    return out


def remaining_ny_run_hours(now_utc, sched):
    """Run hours left in the CURRENT account (NY) day, the current hour included (it is still being spent)."""
    n = now_utc.astimezone(NY)
    return sum(1 for h in ny_run_hours(n.date(), sched) if h >= n.hour)


# ------------------------------------------------------------------ floor
def budget_floor(ny_spend_so_far, base, run_hours_in_day, remaining, buffer=0.10):
    """Lowest budget that keeps Testing serving for the rest of the account (NY) day:
    (Testing spend so far in the NY day + base hourly pace x remaining NY run hours) x (1 + buffer).
    Google stops serving for the rest of the day once spend reaches the daily budget."""
    pace = base / max(1, run_hours_in_day)
    return round((ny_spend_so_far + pace * remaining) * (1 + buffer), 2)


# ------------------------------------------------------------------ decision
def new_day_state():
    return {"evals": {}, "stepped_up": False, "guard": False, "base_lock": False, "changes": 0,
            "virtual_budget": None}


@dataclass
class Snap:
    """Everything one hourly decision needs. Money in USD (sales converted at the day's GBP->USD rate)."""
    h: int                     # evaluated UK hour H (the previous full UK hour)
    h_spend: float             # Testing spend in H
    h_sales: float             # Testing true sales in H
    day_spend: float           # Testing spend in the UK day up to and including H
    day_sales: float           # Testing true sales in the UK day up to and including H
    uk_run_hours: list         # Testing run hours (UK labels) of H's UK date
    cur_run: bool              # is the CURRENT UK hour (the run's hour) a Testing run hour?
    ny_run_hours_count: int    # run hours in the account (NY) day = the hourly pace divisor
    floor: float               # budget_floor(...) for the current NY day
    budget: float              # current daily budget (live, or the dry-run virtual budget)
    campaign_status: str = "ENABLED"
    manual_recent: tuple = ()  # budget changes in the last 60 min that the scaler did not make


def _roas(sales, spend):
    return None if not spend or spend <= 0 else sales / spend


def _dollars_up(x):
    return float(math.ceil(round(x, 6)))


def decide(cfg, s, state):
    """-> (decision dict, new day state). The state is the UK day of H (evals, flags, change count)."""
    st = copy.deepcopy(state) if state else new_day_state()
    roas, droas = _roas(s.h_sales, s.h_spend), _roas(s.day_sales, s.day_spend)
    d = {"action": "hold", "old": s.budget, "new": s.budget, "would": None, "reasons": [],
         "h": s.h, "h_spend": round(s.h_spend, 2), "h_sales": round(s.h_sales, 2),
         "roas": None if roas is None else round(roas, 2),
         "day_spend": round(s.day_spend, 2), "day_sales": round(s.day_sales, 2),
         "day_roas": None if droas is None else round(droas, 2), "floor": s.floor}
    R = d["reasons"].append

    if s.h not in s.uk_run_hours:
        d["action"] = "no_eval"; R(f"hour {s.h:02d} is outside Testing's run hours")
        return d, st
    if s.h_spend <= 0 and s.campaign_status != "ENABLED":
        d["action"] = "refused"; R(f"REFUSED: campaign is {s.campaign_status}, not ENABLED (no spend in hour {s.h:02d})")
        return d, st
    if s.h_spend <= 0:
        d["action"] = "skip_lag"
        R(f"Ads spend for hour {s.h:02d} is $0 while the campaign is {s.campaign_status} and in schedule - data looks incomplete, skipped")
        return d, st
    if str(s.h) in st["evals"]:
        d["action"] = "already_evaluated"; R(f"hour {s.h:02d} was already evaluated today")
        return d, st

    bad, very = roas < cfg.down_roas, roas < cfg.hard_roas
    i = s.uk_run_hours.index(s.h)
    prev = s.uk_run_hours[i - 1] if i > 0 else None
    prev_bad = prev is not None and st["evals"].get(str(prev), {}).get("bad", False)
    two = bad and prev_bad
    st["evals"][str(s.h)] = {"roas": round(roas, 4), "spend": round(s.h_spend, 2), "sales": round(s.h_sales, 2),
                             "bad": bad, "very_bad": very, "day_roas": None if droas is None else round(droas, 4)}
    if st["stepped_up"] and (droas is None or droas < cfg.day_roas) and not st["guard"]:
        st["guard"] = True; R(f"day guard ON: day-to-date ROAS {droas:.2f} < {cfg.day_roas} after a step-up - no more step-ups today")

    above = s.budget > cfg.base + 0.5
    act, target = "hold", s.budget
    if above and (very or two):
        st["base_lock"] = True
        act, target = "base_pace", cfg.base
        R(f"ROAS {roas:.2f} < {cfg.hard_roas}" if very else f"two run hours in a row < {cfg.down_roas} ({prev:02d}, {s.h:02d})")
        R("base pace for the rest of the day")
    elif above and bad:
        act, target = "step_down", max(cfg.base, s.budget / cfg.step_mult)
        R(f"ROAS {roas:.2f} < {cfg.down_roas}: one notch down (/{cfg.step_mult})")
    elif st["base_lock"]:
        if above:
            act, target = "base_pace", cfg.base; R("base pace locked for the rest of the day")
        else:
            R("base pace locked for the rest of the day")
    else:
        need = cfg.pace_share * s.budget / max(1, s.ny_run_hours_count)
        fails = []
        if roas < cfg.up_roas:
            fails.append(f"hour ROAS {roas:.2f} < {cfg.up_roas}")
        if s.h_spend < need:
            fails.append(f"hour spend ${s.h_spend:.2f} < 70% of hourly pace ${need:.2f}")
        if droas is None or droas < cfg.day_roas:
            fails.append(f"day ROAS {0 if droas is None else droas:.2f} < {cfg.day_roas}")
        if st["guard"]:
            fails.append("day guard is ON")
        if s.budget >= min(cfg.cap, cfg.max_budget) - 0.5:
            fails.append(f"budget at cap ${min(cfg.cap, cfg.max_budget):,.0f}")
        if bad:
            fails.insert(0, f"ROAS {roas:.2f} < {cfg.down_roas} but budget already at base")
        if fails:
            R("hold: " + "; ".join(fails))
        else:
            mult = cfg.first_mult if not above else cfg.step_mult
            act, target = "step_up", min(s.budget * mult, cfg.cap, cfg.max_budget)
            R(f"ROAS {roas:.2f} >= {cfg.up_roas}, spend ${s.h_spend:.2f} >= ${need:.2f}, day ROAS {droas:.2f} >= {cfg.day_roas}: x{mult:g}")

    new = s.budget
    if act in ("step_down", "base_pace"):
        new = round(target) if target >= s.floor else _dollars_up(s.floor)
        if target < s.floor:
            R(f"floor ${s.floor:,.2f} keeps the budget above ${target:,.0f} so Testing slows down instead of stopping")
        new = min(new, s.budget)                 # a bad hour never raises the budget
        if new >= s.budget - 0.5:
            R("floor already at/above the current budget - no change"); act, new = "hold", s.budget
    elif act == "step_up":
        new = min(max(round(target), _dollars_up(s.floor)), cfg.cap, cfg.max_budget)
        if new <= s.budget + 0.5:
            act, new = "hold", s.budget

    if act != "hold":
        block = None
        if not s.cur_run:
            block = "current hour is outside Testing's run hours - not acting this hour"
        elif s.campaign_status != "ENABLED":
            block = f"REFUSED: campaign is {s.campaign_status}, not ENABLED"
        elif s.manual_recent:
            block = "REFUSED: budget changed manually in the last 60 min (" + "; ".join(s.manual_recent) + ") - respecting the override"
        elif st["changes"] >= cfg.max_changes:
            block = f"REFUSED: {st['changes']} changes today already (max {cfg.max_changes})"
        if block:
            d["would"] = {"action": act, "new": new}
            d["action"] = "refused" if block.startswith("REFUSED") else "hold_outside_run"
            R(block)
            return d, st
        st["changes"] += 1
        if act == "step_up":
            st["stepped_up"] = True
    d["action"], d["new"] = act, float(new)
    return d, st


def decide_reset(cfg, budget, floor, remaining, campaign_status="ENABLED", manual_recent=(), changes=0):
    """00:05 UK: back to base. If the account (NY) day still has run hours left (not the case with the current
    schedule: UK 00:05 = NY 19:05/20:05), the floor still applies so Testing is never stopped."""
    target = cfg.base if remaining <= 0 else max(cfg.base, _dollars_up(floor))
    d = {"action": "reset", "old": budget, "new": float(target), "would": None, "floor": floor,
         "remaining_ny_run_hours": remaining, "reasons": ["daily reset to base"]}
    if remaining > 0 and target > cfg.base:
        d["reasons"].append(f"NY day still has {remaining} run hours: floor ${floor:,.2f} applies")
    if abs(budget - target) < 0.5:
        d["action"], d["new"] = "reset_noop", budget
        d["reasons"].append(f"budget already ${budget:,.0f}")
        return d
    block = None
    if campaign_status != "ENABLED":
        block = f"REFUSED: campaign is {campaign_status}, not ENABLED"
    elif manual_recent:
        block = "REFUSED: budget changed manually in the last 60 min (" + "; ".join(manual_recent) + ")"
    elif changes >= cfg.max_changes:
        block = f"REFUSED: {changes} changes today already (max {cfg.max_changes})"
    if block:
        d["would"] = {"action": "reset", "new": float(target)}
        d["action"], d["new"] = "refused", budget
        d["reasons"].append(block)
    return d


# ------------------------------------------------------------------ manual-change detection
def parse_ads_time(s):
    """change_event.change_date_time ('2026-11-26 16:06:59.992031', account time zone) -> aware UTC."""
    s = str(s).strip()
    fmt = "%Y-%m-%d %H:%M:%S.%f" if "." in s else "%Y-%m-%d %H:%M:%S"
    return dt.datetime.strptime(s, fmt).replace(tzinfo=NY).astimezone(UTC)


def manual_changes(events, own_writes, now_utc, minutes=60, slack_s=300):
    """Budget change_events in the last `minutes` that do not match one of the scaler's own writes
    (same new amount within slack_s seconds). -> list of short descriptions (no e-mail addresses)."""
    out = []
    for r in events or []:
        e = r.get("changeEvent", r)
        t = parse_ads_time(e["changeDateTime"])
        if now_utc - t > dt.timedelta(minutes=minutes) or t - now_utc > dt.timedelta(minutes=5):
            continue
        new = int(((e.get("newResource") or {}).get("campaignBudget") or {}).get("amountMicros", -1))
        old = int(((e.get("oldResource") or {}).get("campaignBudget") or {}).get("amountMicros", -1))
        mine = any(abs((dt.datetime.fromisoformat(w["at"]) - t).total_seconds()) <= slack_s and int(w["new_micros"]) == new
                   for w in own_writes or [])
        if not mine:
            out.append(f"{t.astimezone(UK):%H:%M} UK ${old / 1e6:,.0f}->${new / 1e6:,.0f} via {e.get('clientType', '?')}")
    return out


# ------------------------------------------------------------------ hourly feed helpers (stores-monitor hourly.py JSON)
def testing_hours(body, uk_date):
    """{uk_hour: (spend_usd, sales_usd, spend_gbp, sales_gbp)} for Testing on uk_date from an hourly.py body."""
    for d in body["days"]:
        if d["date"] == uk_date:
            out = {}
            for h in d["hours"]:
                t = h["split"]["testing"]
                out[h["hour"]] = (float(t["cost_usd"] or 0), float(t["sales_usd"] or 0),
                                  float(t["cost_gbp"] or 0), float(t["sales_gbp"] or 0))
            return out
    return {}


def ny_spend_from_body(body, ny_date, upto_utc):
    """Testing spend of NY date ny_date in hours that started before upto_utc (backtest stand-in for the live Ads query)."""
    tot = 0.0
    for d in body["days"]:
        y, m, dd = map(int, d["date"].split("-"))
        for h in d["hours"]:
            try:
                t = dt.datetime(y, m, dd, h["hour"], tzinfo=UK).astimezone(UTC)
            except Exception:
                continue
            if t < upto_utc and t.astimezone(NY).date().isoformat() == ny_date:
                tot += float(h["split"]["testing"]["cost_usd"] or 0)
    return round(tot, 2)
