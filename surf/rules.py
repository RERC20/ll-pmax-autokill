# surf/rules.py - the owner-approved "surf scaler" rules for PMax | Testing | UK (pure functions, no network).
#
# Rules v2 (owner, 27 Nov 2026; revised the same day): every 30 minutes, judge a ROLLING 60-MINUTE WINDOW ending at
# the last complete 30-minute mark (spend from Google Ads, true Testing sales from Shopify order createdAt, hourly.py
# split rule).
#   step up  = window ROAS >= 2.5 (SURF_UP_ROAS; nothing else: no "spend rising" test, no day guard): x SURF_FIRST_STEP (2) from
#              base, then x SURF_STEP (1.5); no cap
#   hold     = 1.75 <= ROAS < 2.5
#   step down= ROAS < 1.75 (SURF_DOWN_ROAS): /1.5 one notch (SURF_STEP_DOWN), never below the floor, never below base;
#              two consecutive judged windows < 1.75: straight to base pace (max(base, floor)). No lock: a later
#              window >= 2.5 surfs up again from there.
#   "rising" (window spend > previous 60 min spend) is reported only.
#   Safety (owner, 27 Nov): a step-up needs window spend >= SURF_MIN_WINDOW_SPEND (default the base hourly pace =
#   base / NY run hours, ~$21); a window under half the pace is "not enough spend to judge" (no step down, no streak).
#   COUNT ONCE: after a step-up credited to the window ending at T, the next step-up needs a window ending at T+60 or
#   later. That window starts at T or later, so none of its orders (createdAt) or spend minutes were in the previous
#   step-up's window [T-60, T): no order and no spend is ever counted in two step-ups.
#   acts only while the run time is inside Testing's run hours; a decision earned in the last window before a schedule
#   gap is carried to the first run of the next run block (dropped if that run's window contradicts it)
#   no limit on changes per day; skip on incomplete data; 60-min pause after a manual change; 00:05 UK reset to base.
#
# Time zones: the scaler thinks in UK hours/days (Europe/London); the Google Ads account (and so the DAILY BUDGET,
# the ad schedule and segments.date) is New York (America/New_York). Offsets are always whole hours, so every
# 30-minute mark is the same instant in both.
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
HALF = dt.timedelta(minutes=30)
HOUR = dt.timedelta(hours=1)


ENV_NAMES = {"first_mult": "SURF_FIRST_STEP", "step_mult": "SURF_STEP", "down_div": "SURF_STEP_DOWN"}


@dataclass
class Cfg:
    base: float = 300.0          # base daily budget (USD); also the lowest budget the scaler ever sets
    first_mult: float = 2.0      # SURF_FIRST_STEP: first step up from base
    step_mult: float = 1.5       # SURF_STEP: every later step up (owner may set 2 = "keep doubling")
    down_div: float = 1.5        # SURF_STEP_DOWN: one notch down = budget / this
    up_roas: float = 2.5         # SURF_UP_ROAS: window ROAS needed to step up (owner 27 Nov: 2.5)
    down_roas: float = 1.75      # SURF_DOWN_ROAS: window ROAS below this -> one notch down (two in a row -> base pace)
    buffer: float = 0.10         # floor buffer
    min_change: float = 10.0     # a step down smaller than this (USD) is not written (floor noise)
    window_min: int = 60         # judged window length (minutes)
    min_window_spend: float = 0.0  # SURF_MIN_WINDOW_SPEND: window spend needed to step up (0 = base hourly pace,
                                   # i.e. base / run hours of the NY day, ~$21 with 14 run hours)
    min_down_share: float = 0.5  # SURF_MIN_DOWN_SHARE: window spend needed to step down / count as bad, as a share
                                 # of the base hourly pace (0.5 -> ~$11)
    # No budget cap and no daily change limit (owner, 27 Nov 2026): SURF_CAP / SURF_MAX_BUDGET / SURF_MAX_CHANGES
    # are no longer read. No day guard and no base-pace lock either.

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        kw = {}
        for f in fields(cls):
            v = env.get(ENV_NAMES.get(f.name, "SURF_" + f.name.upper()))
            if v not in (None, ""):
                kw[f.name] = int(float(v)) if isinstance(f.default, int) else float(v)
        c = cls(**kw)
        if c.first_mult <= 1 or c.step_mult <= 1 or c.down_div <= 1:
            raise ValueError("SURF_FIRST_STEP / SURF_STEP / SURF_STEP_DOWN must be > 1")
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


def ny_time_runs(ny_t, sched):
    """Is New York time ny_t inside the ad schedule (minute resolution)?"""
    if sched is None:
        return True
    m = ny_t.hour * 60 + ny_t.minute
    return any(s <= m < e for s, e in sched.get(DOW[ny_t.weekday()], []))


def ny_hour_runs(ny_t, sched):
    """Does the New York hour starting at ny_t fall inside the ad schedule? (hour start inside a slot)"""
    return ny_time_runs(ny_t.replace(minute=0, second=0, microsecond=0), sched)


def runs_at(t_utc, sched):
    return ny_time_runs(t_utc.astimezone(NY), sched)


def _hours_of(d, tz):
    """Every real hour of local date d in tz, as UTC instants (23 / 25 on DST change days)."""
    t = dt.datetime(d.year, d.month, d.day, tzinfo=tz).astimezone(UTC)
    while t.astimezone(tz).date() == d:
        yield t
        t += HOUR


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


# ------------------------------------------------------------------ 60-minute window figures
def last_mark(now_utc):
    """The last complete 30-minute mark at or before now (UTC; UK/NY offsets are whole hours)."""
    return now_utc.replace(minute=0 if now_utc.minute < 30 else 30, second=0, microsecond=0)


def hour_of(t):
    return t.replace(minute=0, second=0, microsecond=0)


def spend_hourly(seg, a, b, now_utc):
    """Spend in [a, b) (30-minute marks, UTC) from Google Ads hourly segments {utc_hour_start: usd}.
    A finished hour is split evenly over its two halves (Ads has no finer segment). The hour still running at now_utc
    is a partial reading: its first half = reported spend x 30 / minutes elapsed in the hour (pro rata; >= 30)."""
    tot, t = 0.0, a
    while t < b:
        h = hour_of(t)
        v = float(seg.get(h, 0.0))
        if h <= now_utc < h + HOUR:
            if t.minute == 0:
                tot += v * 30.0 / max(30.0, (now_utc - h).total_seconds() / 60.0)
        else:
            tot += v / 2.0
        t += HALF
    return round(tot, 2)


def running_total(snap, day_totals):
    """Testing spend from the start of the earliest NY date in day_totals up to the snapshot's reading, or None."""
    nd = snap.get("ny_date")
    if nd not in day_totals:
        return None
    return sum(v for d, v in day_totals.items() if d < nd) + float(snap["cum"])


def spend_snapshots(snaps, a, b, day_totals):
    """Spend in [a, b) from two stored cumulative NY-day spend readings (one per 30-minute mark, taken ~20 min after
    it; the reading lag is the same at both ends so it cancels). Across a NY midnight the earlier day's final total
    (read now) is added. None if a reading is missing or the difference is negative."""
    sa, sb = snaps.get(a.isoformat()), snaps.get(b.isoformat())
    if not sa or not sb:
        return None
    ta, tb = running_total(sa, day_totals), running_total(sb, day_totals)
    if ta is None or tb is None or tb < ta - 0.005:
        return None
    return round(tb - ta, 2)


def sales_between(orders, a, b, gbp_usd):
    """True Testing sales (USD) of orders created in [a, b). orders: [(created_utc, testing_pennies_gbp)]."""
    return round(sum(p for t, p in orders if a <= t < b) / 100.0 * gbp_usd, 2)


def window_figures(seg, day_totals, snaps, orders, gbp_usd, end, now_utc, day_start, window_min=60):
    """The judged window [end-60, end), the previous one [end-120, end-60) and day-to-date [day_start, end).
    Spend: cumulative-spend snapshot deltas when the three readings exist, else hourly segments (see spend_hourly)."""
    w = dt.timedelta(minutes=window_min)
    a, pa = end - w, end - 2 * w
    ws, ps = spend_snapshots(snaps, a, end, day_totals), spend_snapshots(snaps, pa, a, day_totals)
    method = "snapshots"
    if ws is None or ps is None:
        ws, ps, method = spend_hourly(seg, a, end, now_utc), spend_hourly(seg, pa, a, now_utc), "hourly"
    return {"w_spend": ws, "p_spend": ps, "spend_method": method,
            "w_sales": sales_between(orders, a, end, gbp_usd), "p_sales": sales_between(orders, pa, a, gbp_usd),
            "day_spend": spend_hourly(seg, day_start, end, now_utc),
            "day_sales": sales_between(orders, day_start, end, gbp_usd),
            "last_half_spend": spend_hourly(seg, end - HALF, end, now_utc)}


def window_label(end):
    a = (end - HOUR).astimezone(UK)
    return f"{a:%H:%M}-{end.astimezone(UK):%H:%M}"


# ------------------------------------------------------------------ decision
def new_day_state():
    return {"evals": {}, "stepped_up": False, "changes": 0, "last_bad": False, "pending": None, "rows": [],
            "last_up_end": None, "up_windows": []}


@dataclass
class Snap:
    """Everything one 30-minute decision needs. Money in USD (sales converted at the day's GBP->USD rate)."""
    end: dt.datetime           # window end (UTC, a 30-minute mark); window = [end - 60 min, end)
    w_spend: float             # Testing spend in the window
    p_spend: float             # Testing spend in the previous 60 minutes (reported only)
    w_sales: float             # Testing true sales in the window
    day_spend: float           # Testing spend in the UK day up to the window end (reported only)
    day_sales: float           # Testing true sales in the UK day up to the window end (reported only)
    end_run: bool              # the window's last 30 minutes are inside Testing's run hours (window is judged)
    cur_run: bool              # the run time is inside Testing's run hours (may act)
    floor: float               # budget_floor(...) for the current NY day
    budget: float              # current daily budget (live, or the dry-run virtual budget)
    campaign_status: str = "ENABLED"
    manual_recent: tuple = ()  # budget changes in the last 60 min that the scaler did not make
    same_day: bool = True      # the window belongs to the current UK day (False: after the 00:05 reset)
    last_half_spend: float = None   # spend in the window's last 30 min (hourly segments); 0 = data incomplete
    pace: float = None         # base hourly pace = base / run hours of the NY day (None: base / 24)


def _roas(sales, spend):
    return None if not spend or spend <= 0 else sales / spend


def _dollars_up(x):
    return float(math.ceil(round(x, 6)))


def _f(x):
    return "n/a" if x is None else f"{x:.2f}"


def _step_up_target(cfg, budget, floor):
    first = cfg.base * cfg.first_mult
    if budget < first - 0.5:
        t, m = first, cfg.first_mult
    else:
        t, m = budget * cfg.step_mult, cfg.step_mult
    return max(t, _dollars_up(floor)), m


def decide(cfg, s, state):
    """-> (decision dict, new day state). The state is the UK day of the window."""
    st = copy.deepcopy(state) if state else new_day_state()
    for k, v in new_day_state().items():
        st.setdefault(k, copy.deepcopy(v))
    mark = s.end.astimezone(UK).strftime("%H:%M")
    roas, droas = _roas(s.w_sales, s.w_spend), _roas(s.day_sales, s.day_spend)
    rising = s.w_spend > s.p_spend
    d = {"action": "hold", "old": s.budget, "new": s.budget, "would": None, "reasons": [], "mark": mark,
         "window": window_label(s.end), "w_spend": round(s.w_spend, 2), "p_spend": round(s.p_spend, 2),
         "rising": rising, "w_sales": round(s.w_sales, 2), "roas": None if roas is None else round(roas, 2),
         "day_spend": round(s.day_spend, 2), "day_sales": round(s.day_sales, 2),
         "day_roas": None if droas is None else round(droas, 2), "floor": s.floor, "carried": False, "mult": None}
    R = d["reasons"].append
    low = max(cfg.base, _dollars_up(s.floor))          # the lowest budget a step down / base pace may set
    pend = st.get("pending")
    act, target = "hold", s.budget
    pace = s.pace if s.pace else cfg.base / 24.0
    need_up = cfg.min_window_spend if cfg.min_window_spend > 0 else pace
    need_down = cfg.min_down_share * pace
    d["need_up"], d["need_down"] = round(need_up, 2), round(need_down, 2)
    up_end = s.end                                       # the window a step-up is credited to (count-once rule)

    if not s.same_day:
        d["action"] = "no_eval"; R("window belongs to the previous UK day (the 00:05 reset already applied)")
        return d, st
    if not s.end_run:
        if not (pend and s.cur_run):
            d["action"] = "no_eval"; R(f"window {d['window']} ends outside Testing's run hours")
            if pend:
                R(f"carried decision waiting for the next run block: {pend['action']} (earned {pend['mark']})")
            return d, st
        # first run of a new run block whose window has no serving time: nothing contradicts the carried decision
        st["pending"] = None
        d["carried"] = True
        act = pend["action"]
        R(f"carried from window ending {pend['mark']} (ROAS {_f(pend.get('roas'))}), earned just before the schedule "
          "gap; this window has no run time to contradict it")
        if act == "step_up":
            target, d["mult"] = _step_up_target(cfg, s.budget, s.floor)
            up_end = dt.datetime.fromisoformat(pend["end"]) if pend.get("end") else s.end
        elif act == "step_down":
            target = max(s.budget / cfg.down_div, low)
        else:
            target = low
    else:
        if s.w_spend <= 0 and s.campaign_status != "ENABLED":
            d["action"] = "refused"; R(f"REFUSED: campaign is {s.campaign_status}, not ENABLED (no spend in the window)")
            return d, st
        if s.w_spend <= 0 or s.last_half_spend == 0:
            d["action"] = "skip_lag"
            R(f"Ads spend for {'the window' if s.w_spend <= 0 else 'the last 30 min'} is $0 while the campaign is "
              f"{s.campaign_status} and in schedule - data looks incomplete, skipped")
            return d, st
        if mark in st["evals"]:
            d["action"] = "already_evaluated"; R(f"window ending {mark} was already judged")
            return d, st

        thin = s.w_spend < need_down                     # too little spend to judge anything
        bad = roas < cfg.down_roas and not thin
        two = bad and st["last_bad"]
        if not thin:                                     # a thin window neither extends nor breaks a bad streak
            st["last_bad"] = bad
        st["evals"][mark] = {"roas": round(roas, 4), "spend": round(s.w_spend, 2), "prev": round(s.p_spend, 2),
                             "sales": round(s.w_sales, 2), "bad": bad, "thin": thin, "rising": rising}
        last_up = dt.datetime.fromisoformat(st["last_up_end"]) if st.get("last_up_end") else None
        fresh_from = last_up + dt.timedelta(minutes=cfg.window_min) if last_up else None
        if thin and roas < cfg.up_roas:
            R(f"hold: not enough spend to judge (${s.w_spend:.2f} < half the base pace ${need_down:.2f})"
              + (f"; ROAS {roas:.2f}" if roas is not None else ""))
        elif two:
            act, target = "base_pace", low
            R(f"two checks in a row < {cfg.down_roas} (ROAS {roas:.2f}): base pace (surfs again on a window >= {cfg.up_roas})")
        elif bad:
            act, target = "step_down", max(s.budget / cfg.down_div, low)
            R(f"ROAS {roas:.2f} < {cfg.down_roas}: one notch down (/{cfg.down_div:g})")
        elif roas >= cfg.up_roas:
            fails = []
            if s.w_spend < need_up:
                fails.append(f"not enough spend to judge (${s.w_spend:.2f} < base pace ${need_up:.2f})")
            if fresh_from and s.end < fresh_from:
                fails.append(f"sales and spend before {last_up.astimezone(UK):%H:%M} already counted in the step-up "
                             f"at {last_up.astimezone(UK):%H:%M}; next step-up needs a fresh window ending "
                             f"{fresh_from.astimezone(UK):%H:%M} or later")
            if fails:
                R(f"hold: ROAS {roas:.2f} >= {cfg.up_roas} but " + "; ".join(fails))
            else:
                target, d["mult"] = _step_up_target(cfg, s.budget, s.floor)
                act = "step_up"
                R(f"ROAS {roas:.2f} >= {cfg.up_roas} on ${s.w_spend:.2f} spend: x{d['mult']:g}")
        else:
            R(f"hold: ROAS {roas:.2f} between {cfg.down_roas} and {cfg.up_roas}")

        if pend and s.cur_run:                      # first judged window of the next run block decides on its own
            st["pending"] = None
            same = (pend["action"] == "step_up") == (act == "step_up") and act != "hold"
            R(f"carried {pend['action']} from window ending {pend['mark']} "
              + ("confirmed by this window" if same else "dropped: contradicted by this window"))
            d["carried"] = same

    new = s.budget
    if act in ("step_down", "base_pace"):
        new = min(float(max(round(target), low)), s.budget)     # a bad window never raises the budget
        if act == "base_pace" or target <= low + 0.5:
            R(f"never below max(floor ${s.floor:,.2f}, base ${cfg.base:,.0f}) = ${low:,.0f}: Testing slows down, never stops")
        if s.budget - new < cfg.min_change:
            R("floor / base keeps the budget where it is - no change"); act, new = "hold", s.budget
    elif act == "step_up":
        new = float(round(target))
        if new <= s.budget + 0.5:
            act, new = "hold", s.budget

    if act != "hold":
        if not s.cur_run:
            st["pending"] = {"action": act, "mark": mark, "roas": None if roas is None else round(roas, 4),
                             "end": s.end.isoformat()}
            d["would"] = {"action": act, "new": new}
            d["action"] = "carry"
            R("run time is outside Testing's run hours: carried to the first run of the next run block")
            return d, st
        block = None
        if s.campaign_status != "ENABLED":
            block = f"REFUSED: campaign is {s.campaign_status}, not ENABLED"
        elif s.manual_recent:
            block = "REFUSED: budget changed manually in the last 60 min (" + "; ".join(s.manual_recent) + ") - respecting the override"
        if block:
            d["would"] = {"action": act, "new": new}
            d["action"] = "refused"
            R(block)
            return d, st
        st["changes"] += 1
        if act == "step_up":
            st["stepped_up"] = True
            st["last_up_end"] = up_end.isoformat()
            st["up_windows"] = (st.get("up_windows") or [])[-40:] + [[window_label(up_end), mark]]
    d["action"], d["new"] = act, float(new)
    return d, st


def decide_reset(cfg, budget, floor, remaining, campaign_status="ENABLED", manual_recent=()):
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


# ====================================================================================================================
# Rules v3 (owner spec, 27 Nov 2026; selected with SURF_RULESET=v3, v2 above stays the default and stays selectable).
#   Decision marks: every :00 and :30 UK while the mark is inside Testing's run hours (outside: no action, no carry).
#   Judged window: [mark - 60 - 180 min, mark - 60 min) = 3 hours ending 60 minutes before the mark.
#     spend  = Testing spend in it (Ads hourly segments, partial hours pro-rated evenly)
#     sales  = Testing true sales of Shopify orders created in it (hourly.py split; no test / cancelled / old-order /
#              imported / store-credit orders; refunds not subtracted)
#     orders = count of those orders with Testing sales > 0
#   smoothed ROAS = (sales + 2.46 x 20) / (spend + 20); decisions use it, raw ROAS is reported.
#   SKIP      spend < $10, or (live only) the window's last hour looks incomplete. Not judged: no state change.
#   STEP UP   smoothed >= 3.0 and spend >= $45 and orders >= 3 and count-once: at base -> x2, else x1.3 (whole $)
#   COUNT ONCE the next step-up needs a mark >= the last step-up's mark + 180 min (its window starts at or after the
#             earlier window's end). Step-downs are not blocked.
#   STEP DOWN smoothed < 1.75 and spend >= $30: budget / 1.3 (whole $), never below the floor, never below $300
#   BASE PACE the previous judged mark also met the step-down conditions: budget = max(floor, $300)
#   HOLD      anything else (1.75 <= smoothed < 3.0, or step-up conditions not met, or a low-spend bad window)
#   FLOOR     (Testing spend so far in the NY account day + $300 / NY-day run hours x remaining NY run hours incl.
#             the current hour) x 1.1
#   Manual override (live only): a budget change in the last 60 min that is not the scaler's own pauses the run.
#   Reset 00:05 UK -> $300.
# Interpretations (recorded): "at base" = budget is $300 or was set by base pace and not changed since; x2 / x1.3 /
# /1.3 multiply the current budget and round half-up to the dollar; the floor is rounded UP to the dollar when it is
# used as a budget; step-down / base pace take max(result, floor, $300) even if that is above the current budget
# ("never below the floor"); the bad streak and count-once are kept across non-judged marks and across the 00:05 reset.
@dataclass
class CfgV3:
    base: float = 300.0
    up_roas: float = 3.0           # smoothed ROAS needed to step up
    down_roas: float = 1.75        # smoothed ROAS below this (with enough spend) = step-down conditions
    prior_roas: float = 2.46       # shrinkage target (pooled Testing ROAS)
    prior_spend: float = 20.0      # pseudo-spend of the prior
    up_min_spend: float = 45.0     # window spend needed to step up
    up_min_orders: int = 3         # window orders (Testing sales > 0) needed to step up
    down_min_spend: float = 30.0   # window spend needed for step-down conditions
    skip_spend: float = 10.0       # window spend below this: skip
    first_mult: float = 2.0        # step up from base
    step_mult: float = 1.3         # every other step up
    down_div: float = 1.3          # step down
    buffer: float = 0.10           # floor buffer
    window_min: int = 180          # judged window length
    lag_min: int = 60              # window ends this long before the mark
    once_min: int = 180            # count-once spacing between step-up marks

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        kw = {}
        for f in fields(cls):
            v = env.get("SURF3_" + f.name.upper())
            if v not in (None, ""):
                kw[f.name] = int(float(v)) if isinstance(f.default, int) else float(v)
        c = cls(**kw)
        if c.first_mult <= 1 or c.step_mult <= 1 or c.down_div <= 1:
            raise ValueError("SURF3_FIRST_MULT / SURF3_STEP_MULT / SURF3_DOWN_DIV must be > 1")
        return c


def ruleset(env=None):
    """SURF_RULESET: 'v3' selects rules v3; anything else (default) keeps v2."""
    env = os.environ if env is None else env
    return "v3" if (env.get("SURF_RULESET") or "v2").strip().lower() == "v3" else "v2"


def v3_window(mark, cfg=None):
    cfg = cfg or CfgV3()
    end = mark - dt.timedelta(minutes=cfg.lag_min)
    return end - dt.timedelta(minutes=cfg.window_min), end


def spend_prorated(seg, a, b):
    """Spend in [a, b) from hourly segments {utc_hour_start: usd}, every hour spread evenly over its 60 minutes."""
    tot, t = 0.0, a
    while t < b:
        h = hour_of(t)
        nxt = min(b, h + HOUR)
        tot += float(seg.get(h, 0.0)) * (nxt - t).total_seconds() / 3600.0
        t = nxt
    return tot


def window_sales_orders(orders, a, b):
    """orders: [(created_utc, testing_usd)] (store-credit etc. already removed) -> (sales usd, orders with sales > 0)."""
    sel = [v for t, v in orders if a <= t < b]
    return sum(sel), sum(1 for v in sel if v > 0)


def smoothed_roas(sales, spend, cfg=None):
    cfg = cfg or CfgV3()
    return (sales + cfg.prior_roas * cfg.prior_spend) / (spend + cfg.prior_spend)


def round_dollar(x):
    """Half-up rounding to the whole dollar (x2 / x1.3 / 1.3 results)."""
    return float(math.floor(round(x, 6) + 0.5))


def floor_v3(ny_spend_so_far, run_hours_in_day, remaining, cfg=None):
    cfg = cfg or CfgV3()
    return budget_floor(ny_spend_so_far, cfg.base, run_hours_in_day, remaining, cfg.buffer)


def new_state_v3(budget=300.0):
    return {"budget": float(budget), "at_base": True, "last_bad": False, "last_up_mark": None, "last_mark": None}


@dataclass
class SnapV3:
    mark: dt.datetime            # decision mark (UTC instant of a UK :00 / :30)
    spend: float                 # window spend (USD)
    sales: float                 # window true Testing sales (USD)
    orders: int                  # window orders with Testing sales > 0
    in_hours: bool               # the mark is inside Testing's run hours
    floor: float                 # floor_v3 at the mark
    budget: float                # budget before the decision (live, or the dry-run virtual budget)
    live: bool = False
    last_hour_incomplete: bool = False   # live only: the window's last hour has no Ads data yet
    manual_recent: tuple = ()    # live only: budget changes in the last 60 min that the scaler did not make
    campaign_status: str = "ENABLED"


def decide_v3(cfg, s, state):
    """-> (decision dict, new state). Pure."""
    st = dict(new_state_v3(s.budget), **(state or {}))
    a, b = v3_window(s.mark, cfg)
    raw = s.sales / s.spend if s.spend > 0 else None
    sm = smoothed_roas(s.sales, s.spend, cfg)
    low = max(cfg.base, float(math.ceil(round(s.floor, 6))))
    d = {"mark": s.mark, "window_start": a, "window_end": b, "spend": round(s.spend, 2), "sales": round(s.sales, 2),
         "orders": s.orders, "raw_roas": None if raw is None else round(raw, 4), "smoothed_roas": round(sm, 4),
         "action": "hold", "budget_before": s.budget, "budget_after": s.budget, "floor": round(s.floor, 2),
         "would": None, "reason": ""}
    at_base = st["at_base"] or abs(s.budget - cfg.base) < 0.5

    if not s.in_hours:
        d["action"], d["reason"] = "out_of_hours", "mark outside Testing's run hours: no action"
        return d, st
    if st.get("last_mark") == s.mark.isoformat():
        d["action"], d["reason"] = "already_evaluated", "this mark was already judged"
        return d, st
    if s.spend < cfg.skip_spend:
        d["action"], d["reason"] = "skip", f"window spend ${s.spend:.2f} < ${cfg.skip_spend:g}: skip"
        return d, st
    if s.live and s.last_hour_incomplete:
        d["action"], d["reason"] = "skip", "Ads data for the window's last hour looks incomplete: skip"
        return d, st
    if s.live and (s.manual_recent or s.campaign_status != "ENABLED"):
        why = (f"campaign is {s.campaign_status}" if s.campaign_status != "ENABLED" else
               "budget changed manually in the last 60 min (" + "; ".join(s.manual_recent) + ")")
        d["action"], d["reason"] = "paused", f"paused: {why} - not judged"
        return d, st

    down_cond = sm < cfg.down_roas and s.spend >= cfg.down_min_spend
    last_up = dt.datetime.fromisoformat(st["last_up_mark"]) if st.get("last_up_mark") else None
    next_up = last_up + dt.timedelta(minutes=cfg.once_min) if last_up else None
    once_ok = next_up is None or s.mark >= next_up
    rs = f"smoothed {sm:.2f} (raw {'n/a' if raw is None else f'{raw:.2f}'}) on ${s.spend:.2f} spend, {s.orders} orders"
    act, new = "hold", s.budget
    if down_cond and st.get("last_bad"):
        act, new = "base_pace", low
        d["reason"] = f"{rs}: second judged mark in a row with smoothed < {cfg.down_roas:g} and spend >= " \
                      f"${cfg.down_min_spend:g} -> base pace = max(floor ${s.floor:,.2f}, ${cfg.base:,.0f})"
    elif down_cond:
        act, new = "step_down", max(round_dollar(s.budget / cfg.down_div), low)
        d["reason"] = f"{rs}: smoothed < {cfg.down_roas:g} and spend >= ${cfg.down_min_spend:g} -> /{cfg.down_div:g}" \
                      + (f", held at max(floor, ${cfg.base:,.0f}) = ${low:,.0f}" if new == low else "")
    elif sm >= cfg.up_roas:
        fails = []
        if s.spend < cfg.up_min_spend:
            fails.append(f"spend ${s.spend:.2f} < ${cfg.up_min_spend:g}")
        if s.orders < cfg.up_min_orders:
            fails.append(f"{s.orders} orders < {cfg.up_min_orders}")
        if not once_ok:
            fails.append(f"count-once: last step-up at {last_up.astimezone(UK):%d %b %H:%M}, next allowed from "
                         f"{next_up.astimezone(UK):%H:%M}")
        if fails:
            d["reason"] = f"{rs}: hold, smoothed >= {cfg.up_roas:g} but " + "; ".join(fails)
        else:
            m = cfg.first_mult if at_base else cfg.step_mult
            act, new = "step_up", round_dollar(s.budget * m)
            d["mult"] = m
            d["reason"] = f"{rs}: step up x{m:g}" + (" from base" if at_base else "")
    elif sm < cfg.down_roas:
        d["reason"] = f"{rs}: hold, smoothed < {cfg.down_roas:g} but spend < ${cfg.down_min_spend:g}"
    else:
        d["reason"] = f"{rs}: hold, smoothed between {cfg.down_roas:g} and {cfg.up_roas:g}"

    st["last_bad"] = bool(down_cond)
    st["last_mark"] = s.mark.isoformat()
    if act == "step_up":
        st["last_up_mark"] = s.mark.isoformat()
        st["at_base"] = False
    elif act == "base_pace":
        st["at_base"] = True
    elif act == "step_down":
        st["at_base"] = abs(new - cfg.base) < 0.5
    st["budget"] = float(new)
    d["action"], d["budget_after"] = act, float(new)
    return d, st


def reset_v3(cfg, state, budget):
    """00:05 UK: budget back to $300 (base). Count-once and the bad streak are kept."""
    st = dict(new_state_v3(budget), **(state or {}))
    st["budget"], st["at_base"] = cfg.base, True
    return {"action": "reset" if abs(budget - cfg.base) >= 0.5 else "reset_noop", "budget_before": budget,
            "budget_after": cfg.base, "reason": "daily reset 00:05 UK -> base"}, st
