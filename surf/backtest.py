# surf/backtest.py - replay the surf scaler over real hourly data (stores-monitor /api/hourly JSON, i.e. the same
# hourly.py Testing true-sales split the live scaler computes).
#   python -m surf.backtest FEED.json SCHEDULE_ROWS.json 2026-11-20 2026-11-26 [SCHEDULE_FROM]
# SCHEDULE_FROM (UK date): days before it are replayed with no ad schedule (Testing's schedule was created on
# 24 Nov 2026 07:42 New York; before that it ran all day).
# Assumptions: final (not as-of-:50) data, campaign ENABLED, no manual changes, actual hourly spend/sales regardless of
# the simulated budget (counterfactual: a higher budget would have bought more traffic), live ad schedule all days.
import datetime as dt
import json
import sys

from .rules import (NY, UK, UTC, Cfg, Snap, budget_floor, decide, decide_reset, new_day_state, ny_run_hours,
                    ny_spend_from_body, parse_schedule, remaining_ny_run_hours, testing_hours, uk_run_hours)


def simulate(body, sched, d, cfg=None):
    cfg = cfg or Cfg()
    rows = []
    reset_at = dt.datetime(d.year, d.month, d.day, 0, 5, tzinfo=UK).astimezone(UTC)
    rem0 = remaining_ny_run_hours(reset_at, sched)
    nyd0 = reset_at.astimezone(NY).date()
    fl0 = budget_floor(ny_spend_from_body(body, nyd0.isoformat(), reset_at), cfg.base, len(ny_run_hours(nyd0, sched)),
                       rem0, cfg.buffer)
    r = decide_reset(cfg, cfg.base, fl0, rem0)
    budget, st = r["new"], new_day_state()
    th = testing_hours(body, d.isoformat())
    run_h = uk_run_hours(d, sched)
    for H in range(24):
        now = (dt.datetime(d.year, d.month, d.day, H, tzinfo=UK).astimezone(UTC) + dt.timedelta(minutes=110))
        uk_now = now.astimezone(UK)
        nyd = now.astimezone(NY).date()
        nyc = len(ny_run_hours(nyd, sched))
        rem = remaining_ny_run_hours(now, sched)
        cur_start = now.replace(minute=0, second=0, microsecond=0)
        ny_spend = ny_spend_from_body(body, nyd.isoformat(), cur_start + dt.timedelta(hours=1))   # incl. current hour
        fl = budget_floor(ny_spend, cfg.base, nyc, rem, cfg.buffer)
        hs, hsa = th.get(H, (0, 0, 0, 0))[:2]
        ds = sum(v[0] for k, v in th.items() if k <= H)
        dsa = sum(v[1] for k, v in th.items() if k <= H)
        cur_run = uk_now.hour in uk_run_hours(uk_now.date(), sched)
        s = Snap(h=H, h_spend=hs, h_sales=hsa, day_spend=ds, day_sales=dsa, uk_run_hours=run_h, cur_run=cur_run,
                 ny_run_hours_count=nyc, floor=fl, budget=budget)
        dec, st = decide(cfg, s, st)
        rows.append({"run_uk": f"{uk_now:%d %H:%M}", **{k: dec[k] for k in ("h", "h_spend", "h_sales", "roas", "day_roas",
                                                                         "action", "old", "new", "floor")},
                     "would": dec.get("would"), "why": " | ".join(dec["reasons"])})
        budget = dec["new"]
    return rows, st


def main(argv):
    body = json.load(open(argv[0]))
    sched0 = parse_schedule(json.load(open(argv[1])))
    a, b = dt.date.fromisoformat(argv[2]), dt.date.fromisoformat(argv[3])
    sfrom = dt.date.fromisoformat(argv[4]) if len(argv) > 4 else a
    d = a
    while d <= b:
        sched = sched0 if d >= sfrom else None
        rows, st = simulate(body, sched, d)
        t = testing_hours(body, d.isoformat())
        sp, sa = sum(v[0] for v in t.values()), sum(v[1] for v in t.values())
        print(f"\n=== UK {d} | Testing day: spend ${sp:,.2f} true sales ${sa:,.2f} ROAS {sa / sp if sp else 0:.2f} | "
              f"max budget ${max(r['new'] for r in rows):,.0f} | changes {st['changes']} | lock={st['base_lock']} guard={st['guard']}"
              f" | schedule: {'live' if sched else 'none (all hours)'}")
        print(f"{'run':>8} {'H':>2} {'spend':>8} {'sales':>8} {'ROAS':>5} {'dROAS':>5} {'floor':>8} {'budget':>13}  action")
        for r in rows:
            if r["action"] == "no_eval" and r["h_spend"] == 0:
                continue
            bud = f"{r['old']:,.0f}->{r['new']:,.0f}" if r["new"] != r["old"] else f"{r['old']:,.0f}"
            w = f" (would {r['would']['action']}->{r['would']['new']:,.0f})" if r.get("would") else ""
            print(f"{r['run_uk']:>8} {r['h']:>2} {r['h_spend']:>8.2f} {r['h_sales']:>8.2f} {r['roas'] if r['roas'] is not None else '-':>5} "
                  f"{r['day_roas'] if r['day_roas'] is not None else '-':>5} {r['floor']:>8.2f} {bud:>13}  {r['action']}{w}")
        d += dt.timedelta(days=1)


if __name__ == "__main__":
    main(sys.argv[1:])
