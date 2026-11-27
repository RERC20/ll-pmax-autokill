# surf/backtest3.py - replay the surf scaler rules v3 (rules.decide_v3) over real data, score its decisions and
# estimate the profit effect with the ~/surfwork/sweep counterfactual model.
#   python -m surf.backtest3 SPEND.json ORDERS.json SCHEDULE_ROWS.json ACTUAL_BUDGETS.json START_UK END_UK OUT.csv
# SPEND.json   {"seg": {utc_hour_start_iso: Testing usd}, "gbp_usd": {uk_date: [rate, rate_date]}} (Ads segments)
# ORDERS.json  [[created_utc_iso, testing_pennies_gbp, total_pennies, credit_reason], ...] (hourly.py split; test /
#              cancelled / old-order / imported already removed; credit_reason "store_credit" rows are dropped here)
# ACTUAL_BUDGETS.json [[ny_local_iso, usd], ...] Testing budget change history (change_event), for the profit model
# START_UK / END_UK: first mark and data end ("2026-11-17T17:00"), UK local time. Marks: every :00 / :30 in
# [START, END]. Spend is the final hourly Ads figure, spread evenly inside each hour.
# Assumptions: no live-only checks (data lag, manual override), campaign ENABLED, actual spend and sales whatever the
# simulated budget, floor from actual NY-day spend up to the mark, schedule: none (all hours) before SCHED_SINCE.
import csv
import datetime as dt
import json
import random
import sys

from .rules import (HOUR, NY, UK, UTC, CfgV3, SnapV3, decide_v3, floor_v3, new_state_v3, ny_run_hours,
                    parse_schedule, remaining_ny_run_hours, reset_v3, runs_at, spend_prorated, v3_window,
                    window_sales_orders)

SCHED_SINCE = dt.datetime(2026, 11, 24, 7, 42, tzinfo=NY).astimezone(UTC)   # Testing ad schedule created
HALF = dt.timedelta(minutes=30)
MARGIN = 1 - 0.335 - 0.095                                                   # 0.57, as ~/surfwork/sweep
WRITE_LAG = dt.timedelta(minutes=20)                                         # live: the :00 mark runs at :20
CSV_COLS = ["mark_uk", "window_start", "window_end", "spend", "sales", "orders", "raw_roas", "smoothed_roas",
            "action", "budget_before", "budget_after", "floor", "reason"]


def load(spend_path, orders_path, sched_path):
    sp = json.load(open(spend_path))
    seg = {dt.datetime.fromisoformat(k): float(v) for k, v in sp["seg"].items()}
    fx = {k: (v[0] if isinstance(v, list) else v) for k, v in sp["gbp_usd"].items()}
    orders, dropped = [], []
    for row in json.load(open(orders_path)):
        t = dt.datetime.fromisoformat(row[0])
        if len(row) > 3 and row[3] == "store_credit":
            dropped.append(row); continue
        orders.append((t, int(row[1]) / 100.0 * fx[t.astimezone(UK).date().isoformat()]))
    return seg, sorted(orders), parse_schedule(json.load(open(sched_path))), fx, dropped


def sched_at(t, sched):
    return sched if t >= SCHED_SINCE else None


def floor_at(seg, t, sched, cfg):
    s = sched_at(t, sched)
    nyd = t.astimezone(NY).date()
    ny0 = dt.datetime(nyd.year, nyd.month, nyd.day, tzinfo=NY).astimezone(UTC)
    so_far = spend_prorated(seg, ny0, t)
    return floor_v3(so_far, len(ny_run_hours(nyd, s)), remaining_ny_run_hours(t, s), cfg), so_far


def simulate(seg, orders, sched, start, end, cfg=None, budget0=None):
    """-> (rows, budget_changes [(utc, usd)]). One row per mark in [start, end]."""
    cfg = cfg or CfgV3()
    st = new_state_v3(budget0 or cfg.base)
    budget, rows, changes = st["budget"], [], []
    t = start
    while t <= end:
        uk = t.astimezone(UK)
        if uk.hour == 0 and uk.minute == 30:                 # the 00:05 UK reset happened since the 00:00 mark
            r, st = reset_v3(cfg, st, budget)
            if r["budget_after"] != budget:
                changes.append((t - dt.timedelta(minutes=25), r["budget_after"]))
            budget = r["budget_after"]
        a, b = v3_window(t, cfg)
        spend = spend_prorated(seg, a, b)
        sales, n = window_sales_orders(orders, a, b)
        fl, _ = floor_at(seg, t, sched, cfg)
        s = SnapV3(mark=t, spend=spend, sales=sales, orders=n, in_hours=runs_at(t, sched_at(t, sched)), floor=fl,
                   budget=budget)
        d, st = decide_v3(cfg, s, st)
        if d["budget_after"] != budget:
            changes.append((t + WRITE_LAG, d["budget_after"]))
        budget = d["budget_after"]
        rows.append(d)
        t += HALF
    return rows, changes


def csv_rows(rows):
    f = lambda x: "" if x is None else f"{x:.2f}"            # noqa: E731
    for d in rows:
        yield {"mark_uk": f"{d['mark'].astimezone(UK):%Y-%m-%d %H:%M}",
               "window_start": f"{d['window_start'].astimezone(UK):%Y-%m-%d %H:%M}",
               "window_end": f"{d['window_end'].astimezone(UK):%Y-%m-%d %H:%M}",
               "spend": f(d["spend"]), "sales": f(d["sales"]), "orders": d["orders"], "raw_roas": f(d["raw_roas"]),
               "smoothed_roas": f(d["smoothed_roas"]), "action": d["action"],
               "budget_before": f"{d['budget_before']:.0f}", "budget_after": f"{d['budget_after']:.0f}",
               "floor": f(d["floor"]), "reason": d["reason"]}


def day_summary(rows, seg, orders, start, end):
    out = []
    days = sorted({d["mark"].astimezone(UK).date() for d in rows})
    for day in days:
        a = max(start, dt.datetime(day.year, day.month, day.day, tzinfo=UK).astimezone(UTC))
        b = min(end, dt.datetime.combine(day + dt.timedelta(days=1), dt.time(), UK).astimezone(UTC))
        sp = spend_prorated(seg, a, b)
        sa, n = window_sales_orders(orders, a, b)
        rs = [d for d in rows if d["mark"].astimezone(UK).date() == day]
        c = lambda k: sum(1 for d in rs if d["action"] == k)  # noqa: E731
        out.append({"day": day.isoformat(), "from": f"{a.astimezone(UK):%H:%M}", "to": f"{b.astimezone(UK):%H:%M}",
                    "spend": sp, "sales": sa, "orders": n, "roas": sa / sp if sp else None, "ups": c("step_up"),
                    "downs": c("step_down"), "base": c("base_pace"), "holds": c("hold"), "skips": c("skip"),
                    "off": c("out_of_hours"), "max_budget": max(max(d["budget_before"], d["budget_after"]) for d in rs),
                    "end_budget": rs[-1]["budget_after"]})
    return out


# ------------------------------------------------------------------ decision quality
def outcome(seg, orders, t, data_end, mins=120, min_spend=5.0):
    """True ROAS of [t, t + mins) from actual data; None when it runs past the data or spend <= min_spend."""
    e = t + dt.timedelta(minutes=mins)
    if e > data_end:
        return None
    sp = spend_prorated(seg, t, e)
    sa, _ = window_sales_orders(orders, t, e)
    return None if sp <= min_spend else {"spend": sp, "sales": sa, "roas": sa / sp}


def perm_p(outs, idx, n=5000, seed=1):
    """P(mean of shuffled outcomes at idx >= observed); outcomes shuffled n times with random.seed(seed)."""
    if not idx:
        return None
    obs = sum(outs[i] for i in idx) / len(idx)
    random.seed(seed)
    o, ge = list(outs), 0
    for _ in range(n):
        random.shuffle(o)
        ge += sum(o[i] for i in idx) / len(idx) >= obs - 1e-12
    return ge / n


def quality(rows, seg, orders, data_end, actions, test, n=5000, seed=1):
    base = [(d, outcome(seg, orders, d["mark"], data_end)) for d in rows]
    base = [(d, o) for d, o in base if o is not None]
    outs = [test(o["roas"]) for _, o in base]
    idx = [i for i, (d, _) in enumerate(base) if d["action"] in actions]
    sel = [base[i] for i in idx]
    judged = [i for i, (d, _) in enumerate(base) if d["action"] in ("step_up", "step_down", "base_pace", "hold")]
    decided = [d for d in rows if d["action"] in actions]
    return {"decisions": len(decided), "scored": len(sel), "unscored": len(decided) - len(sel),
            "items": [(d, o) for d, o in sel],
            "hit": (sum(outs[i] for i in idx) / len(idx)) if idx else None,
            "base_n": len(base), "base_rate": sum(outs) / len(outs) if outs else None,
            "judged_n": len(judged), "judged_rate": (sum(outs[i] for i in judged) / len(judged)) if judged else None,
            "pooled": (sum(o["sales"] for _, o in sel) / sum(o["spend"] for _, o in sel)) if sel else None,
            "base_pooled": sum(o["sales"] for _, o in base) / sum(o["spend"] for _, o in base) if base else None,
            "p": perm_p(outs, idx, n, seed)}


# ------------------------------------------------------------------ counterfactual profit (~/surfwork/sweep model)
def step_fn(changes, default=300.0):
    ch = sorted(changes)

    def f(t):
        b = default
        for a, v in ch:
            if a <= t:
                b = v
            else:
                break
        return b
    return f


def hour_avg(fn, h):
    return sum(fn(h + dt.timedelta(minutes=m + 0.5)) for m in range(60)) / 60.0


class Model:
    """evaluate.py model_delta: an hour's spend scales as (budget/300)^alpha, its ROAS as (budget/300)^-k; the $300
    reference ROAS of each hour is shrunk toward the pooled ROAS with W pseudo-dollars. profit = 0.57 x sales - spend."""

    def __init__(self, seg, orders, act_fn, pooled_from, data_end):
        self.hours = sorted(t for t in seg if pooled_from <= t < data_end)
        self.pooled = window_sales_orders(orders, pooled_from, data_end)[0] / sum(seg[t] for t in self.hours)
        self.stat = {t: (seg[t], window_sales_orders(orders, t, t + HOUR)[0], hour_avg(act_fn, t) / 300.0)
                     for t in self.hours}

    def delta(self, simf, k, a, W, lo=None):
        tot = xs = 0.0
        for t, (s, sa, ra) in self.stat.items():
            if s <= 0 or (lo and t < lo):
                continue
            sref = s / ra ** a
            rref = sa / (sref + (s - sref) * ra ** (-k))
            rref = (rref * sref + W * self.pooled) / (sref + W)
            r = hour_avg(simf, t) / 300.0
            if r <= 1.0001:
                continue
            dsp = sref * (r ** a - 1)
            tot += MARGIN * dsp * rref * r ** (-k) - dsp
            xs += dsp
        return tot, xs

    def flat300(self, k, a, lo):
        """modelled profit of a flat $300 budget over hours >= lo (unshrunk reference sales)."""
        p = 0.0
        for t, (s, sa, ra) in self.stat.items():
            if s <= 0 or t < lo:
                continue
            sref = s / ra ** a
            p += MARGIN * sa * sref / (sref + (s - sref) * ra ** (-k)) - sref
        return p


W_OF_K = {0.25: 20, 0.40: 40, 0.55: 80}                     # sweep SCEN: opt k.25 W20, mid k.40 W40, pess k.55 W80


def main(argv):
    seg_p, ord_p, sch_p, act_p, s_uk, e_uk, out_csv = argv[:7]
    seg, orders, sched, fx, dropped = load(seg_p, ord_p, sch_p)
    start = dt.datetime.fromisoformat(s_uk).replace(tzinfo=UK).astimezone(UTC)
    end = dt.datetime.fromisoformat(e_uk).replace(tzinfo=UK).astimezone(UTC)
    seg = {t: v for t, v in seg.items() if t < end}
    orders = [(t, v) for t, v in orders if t < end]
    cfg = CfgV3.from_env()
    rows, changes = simulate(seg, orders, sched, start, end, cfg)
    with open(out_csv, "w", newline="") as fh:
        w = csv.DictWriter(fh, CSV_COLS)
        w.writeheader()
        w.writerows(csv_rows(rows))
    print(f"rows {len(rows)} -> {out_csv}; store-credit orders dropped: {len(dropped)} "
          f"(GBP {sum(r[1] for r in dropped) / 100:.2f} Testing)")

    print("\n(b) per UK day (spend/sales over the backtest part of the day)")
    print(f"{'day':10} {'part':11} {'spend':>8} {'sales':>8} {'ROAS':>5} {'ord':>3} {'ups':>3} {'down':>4} {'base':>4} "
          f"{'hold':>4} {'skip':>4} {'off':>3} {'max$':>6} {'end$':>6}")
    tot = {"spend": 0.0, "sales": 0.0}
    for r in day_summary(rows, seg, orders, start, end):
        tot["spend"] += r["spend"]; tot["sales"] += r["sales"]
        print(f"{r['day']:10} {r['from']}-{r['to']} {r['spend']:8.2f} {r['sales']:8.2f} "
              f"{(r['roas'] or 0):5.2f} {r['orders']:3d} {r['ups']:3d} {r['downs']:4d} {r['base']:4d} {r['holds']:4d} "
              f"{r['skips']:4d} {r['off']:3d} {r['max_budget']:6.0f} {r['end_budget']:6.0f}")
    print(f"TOTAL {tot['spend']:.2f} spend, {tot['sales']:.2f} sales, ROAS {tot['sales'] / tot['spend']:.3f}")

    print("\n(c) decision quality: true ROAS of [mark, mark + 2 h) from actual data (spend > $5, inside the data)")
    for lab, acts, test, desc in (("step-ups", ("step_up",), lambda x: x >= 2.2, "next 2h >= 2.2"),
                                  ("step-downs", ("step_down",), lambda x: x < 1.75, "next 2h < 1.75"),
                                  ("step-downs + base pace", ("step_down", "base_pace"), lambda x: x < 1.75,
                                   "next 2h < 1.75")):
        q = quality(rows, seg, orders, end, acts, test)
        print(f"  {lab}: {q['decisions']} decisions, {q['scored']} scored ({q['unscored']} without a complete next 2h)")
        for d, o in q["items"]:
            print(f"    {d['mark'].astimezone(UK):%d %b %H:%M} smoothed {d['smoothed_roas']:.2f} "
                  f"{d['budget_before']:.0f}->{d['budget_after']:.0f} | next 2h spend {o['spend']:.2f} sales "
                  f"{o['sales']:.2f} ROAS {o['roas']:.2f} {'HIT' if test(o['roas']) else 'miss'}")
        if q["scored"]:
            print(f"  -> hit rate ({desc}) {q['hit']:.3f} vs base rate {q['base_rate']:.3f} over all {q['base_n']} "
                  f"marks (judged marks only: {q['judged_rate']:.3f}, n={q['judged_n']}); pooled next-2h ROAS "
                  f"{q['pooled']:.3f} (all marks {q['base_pooled']:.3f}); permutation p = {q['p']:.4f} "
                  f"(5,000 shuffles, seed 1)")

    print("\n(d) counterfactual profit vs flat $300 (sweep model; profit = 0.57 x sales - spend)")
    act = json.load(open(act_p))
    act_fn = step_fn([(dt.datetime.fromisoformat(a).replace(tzinfo=NY).astimezone(UTC), float(v)) for a, v in act])
    pooled_from = dt.datetime(2026, 11, 14, tzinfo=UK).astimezone(UTC)
    m = Model(seg, orders, act_fn, pooled_from, end)
    v3f = step_fn(changes)
    v3now = step_fn([(t - WRITE_LAG if t.minute in (20, 50) else t, v) for t, v in changes])
    f390 = lambda t: 390.0 if t >= start else 300.0         # noqa: E731
    f450 = lambda t: 450.0 if t >= start else 300.0         # noqa: E731
    print(f"  pooled ROAS for the shrink (14 Nov 00:00 UK - data end) {m.pooled:.3f}; budget changes in v3 path "
          f"{len(changes)}, effective 20 min after the mark")
    print(f"  {'k':>4} {'a':>4} {'W':>3} {'flat300 $':>10} {'v3 d$':>8} {'v3 xspend':>9} {'390 d$':>8} {'450 d$':>8} "
          f"{'v3-390':>8} {'v3-450':>8} {'v3@mark':>8}")
    for k in (0.25, 0.40, 0.55):
        for a in (0.85, 0.95):
            W = W_OF_K[k]
            v3d, xs = m.delta(v3f, k, a, W, start)
            d390, _ = m.delta(f390, k, a, W, start)
            d450, _ = m.delta(f450, k, a, W, start)
            v3m, _ = m.delta(v3now, k, a, W, start)
            print(f"  {k:4.2f} {a:4.2f} {W:3d} {m.flat300(k, a, start):10.2f} {v3d:8.2f} {xs:9.2f} {d390:8.2f} "
                  f"{d450:8.2f} {v3d - d390:8.2f} {v3d - d450:8.2f} {v3m:8.2f}")
    sp = spend_prorated(seg, start, end)
    sa = window_sales_orders(orders, start, end)[0]
    print(f"  actual (real budgets): spend {sp:.2f}, sales {sa:.2f}, profit {MARGIN * sa - sp:.2f}")
    return rows


if __name__ == "__main__":
    main(sys.argv[1:])
