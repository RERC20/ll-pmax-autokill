# surf/backtest.py - replay the surf scaler rules v2 (30-minute runs, rolling 60-minute window) over real data.
#   python -m surf.backtest FEED.json ORDERS.json SCHEDULE_ROWS.json 2026-11-20 2026-11-26 [SCHEDULE_FROM] [--detail]
# FEED.json   = stores-monitor /api/hourly body (Testing spend per UK hour, final figures; gbp_usd per UK date)
# ORDERS.json = [[created_utc_iso, testing_pennies_gbp, total_pennies], ...] (surf.sales split rule)
# SCHEDULE_FROM (UK date): days before it are replayed with no ad schedule (Testing's schedule was created on
# 24 Nov 2026 07:42 New York; before that it ran all day).
# How 30 minutes are approximated: Ads only has hourly segments and no intra-hour history exists, so spend is assumed
# even within each hour: at a :50 run the running hour is read as 50/60 of its final spend (what an Ads reading with
# no lag would show), which the live pro-rata rule turns into half of the hour for its first 30 minutes. Windows ending
# at :00 are exact full hours. Sales are exact: every order counted by its createdAt minute.
# Other assumptions: campaign ENABLED, no manual changes, actual spend/sales regardless of the simulated budget
# (counterfactual: a higher budget would have bought more traffic, a lower one less), no data lag (no skip_lag).
import datetime as dt
import json
import sys

from .rules import (HALF, HOUR, NY, UK, UTC, Cfg, Snap, budget_floor, decide, decide_reset, hour_of, new_day_state,
                    ny_run_hours, parse_schedule, remaining_ny_run_hours, runs_at, window_figures, window_label)


def load_seg(body):
    """{utc_hour_start: final Testing spend usd}, {uk_date: gbp_usd}"""
    seg, fx = {}, {}
    for d in body["days"]:
        y, m, dd = map(int, d["date"].split("-"))
        fx[d["date"]] = d["gbp_usd"]
        for h in d["hours"]:
            t = dt.datetime(y, m, dd, h["hour"], tzinfo=UK).astimezone(UTC)
            seg[t] = seg.get(t, 0.0) + float(h["split"]["testing"]["cost_usd"] or 0)
    return seg, fx


def as_read_at(seg, now):
    """What Ads would report at `now` with even spend inside the hour and no lag."""
    out = {}
    for t, v in seg.items():
        if t + HOUR <= now:
            out[t] = v
        elif t <= now:
            out[t] = v * (now - t).total_seconds() / 3600.0
    return out


def ny_totals(seg_now):
    out = {}
    for t, v in seg_now.items():
        k = t.astimezone(NY).date().isoformat()
        out[k] = out.get(k, 0.0) + v
    return out


def simulate(seg, fx, orders, sched, d, cfg=None, budget0=None):
    cfg = cfg or Cfg()
    rows = []
    day_start = dt.datetime(d.year, d.month, d.day, tzinfo=UK).astimezone(UTC)
    reset_at = day_start + dt.timedelta(minutes=5)
    sn = as_read_at(seg, reset_at)
    nyd0 = reset_at.astimezone(NY).date()
    fl0 = budget_floor(ny_totals(sn).get(nyd0.isoformat(), 0.0), cfg.base, len(ny_run_hours(nyd0, sched)),
                       remaining_ny_run_hours(reset_at, sched), cfg.buffer)
    r = decide_reset(cfg, budget0 or cfg.base, fl0, remaining_ny_run_hours(reset_at, sched))
    budget, st = r["new"], new_day_state()
    g2u = fx[d.isoformat()]
    t = day_start + dt.timedelta(minutes=20)
    while t < day_start + dt.timedelta(days=1):
        for now in (t, t + HALF):
            end = now.replace(minute=0 if now.minute < 30 else 30)
            uk_date = (end - HALF).astimezone(UK).date()
            sn = as_read_at(seg, now)
            tot = ny_totals(sn)
            nyd = now.astimezone(NY).date()
            fl = budget_floor(tot.get(nyd.isoformat(), 0.0), cfg.base, len(ny_run_hours(nyd, sched)),
                              remaining_ny_run_hours(now, sched), cfg.buffer)
            fig = window_figures(sn, tot, {}, orders, g2u, end, now, day_start, cfg.window_min)
            s = Snap(end=end, w_spend=fig["w_spend"], p_spend=fig["p_spend"], w_sales=fig["w_sales"],
                     day_spend=fig["day_spend"], day_sales=fig["day_sales"], end_run=runs_at(end - HALF, sched),
                     cur_run=runs_at(now, sched), floor=fl, budget=budget, same_day=uk_date == d,
                     last_half_spend=fig["last_half_spend"], pace=cfg.base / max(1, len(ny_run_hours(nyd, sched))))
            dec, st = decide(cfg, s, st)
            rows.append({"run": f"{now.astimezone(UK):%H:%M}", **dec})
            budget = dec["new"]
        t += HOUR
    return rows, st


def main(argv):
    detail = "--detail" in argv
    argv = [a for a in argv if a != "--detail"]
    body = json.load(open(argv[0]))
    orders = [(dt.datetime.fromisoformat(o[0]), int(o[1])) for o in json.load(open(argv[1]))]
    sched0 = parse_schedule(json.load(open(argv[2])))
    a, b = dt.date.fromisoformat(argv[3]), dt.date.fromisoformat(argv[4])
    sfrom = dt.date.fromisoformat(argv[5]) if len(argv) > 5 else a
    cfg = Cfg.from_env()
    seg, fx = load_seg(body)
    print(f"Rules v2: up x{cfg.first_mult:g} then x{cfg.step_mult:g} at window ROAS >= {cfg.up_roas}; "
          f"down /{cfg.down_div:g} < {cfg.down_roas}; 2 in a row -> base pace; no cap / guard / lock; "
          f"min window spend: up = base pace (or ${cfg.min_window_spend:g}), down = {cfg.min_down_share:g} x pace; count once")
    summary = []
    d = a
    while d <= b:
        sched = sched0 if d >= sfrom else None
        rows, st = simulate(seg, fx, orders, sched, d, cfg)
        day_sp = sum(v for t, v in seg.items() if t.astimezone(UK).date() == d)
        day_sa = sum(p for t, p in orders if t.astimezone(UK).date() == d) / 100 * fx[d.isoformat()]
        cnt = {}
        for r in rows:
            cnt[r["action"]] = cnt.get(r["action"], 0) + 1
        mx = max(max(r["new"], r["old"]) for r in rows)
        mx_at = next(r["run"] for r in rows if max(r["new"], r["old"]) == mx)
        summary.append((d, day_sp, day_sa, cnt, mx, mx_at, rows[-1]["new"], sched is not None))
        if detail:
            print(f"\n=== UK {d} | Testing day: spend ${day_sp:,.2f} true sales ${day_sa:,.2f} ROAS "
                  f"{day_sa / day_sp if day_sp else 0:.2f} | max budget ${mx:,.0f} at {mx_at} | "
                  f"schedule: {'live' if sched else 'none (all hours)'}")
            print(f"{'run':>5} {'window':>11} {'spend':>7} {'prev':>7} {'rise':>4} {'sales':>7} {'ROAS':>5} {'dROAS':>5} "
                  f"{'action':>17} {'old->new':>12} {'floor':>7}")
            for r in rows:
                if r["action"] == "no_eval" and not r.get("carried"):
                    continue
                bud = f"{r['old']:,.0f}->{r['new']:,.0f}" if r["new"] != r["old"] else f"{r['old']:,.0f}"
                act = r["action"] + ("(carried)" if r.get("carried") else "")
                if r.get("would"):
                    act += f"->{r['would']['new']:,.0f}"
                f2 = lambda x: "-" if x is None else f"{x:.2f}"              # noqa: E731
                print(f"{r['run']:>5} {r['window']:>11} {r['w_spend']:>7.2f} {r['p_spend']:>7.2f} "
                      f"{'yes' if r['rising'] else 'no':>4} {r['w_sales']:>7.2f} {f2(r['roas']):>5} {f2(r['day_roas']):>5} "
                      f"{act:>17} {bud:>12} {r['floor']:>7,.0f}")
        d += dt.timedelta(days=1)
    print("\nday         spend    sales  ROAS  judged  up  down  base  hold  carry  max budget (at)   end")
    for d, sp, sa, c, mx, at, endb, live_s in summary:
        judged = sum(c.get(k, 0) for k in ("step_up", "step_down", "base_pace", "hold", "carry"))
        print(f"{d}  {sp:7.2f}  {sa:7.2f}  {sa / sp if sp else 0:4.2f}  {judged:6d}  {c.get('step_up', 0):2d}  "
              f"{c.get('step_down', 0):4d}  {c.get('base_pace', 0):4d}  {c.get('hold', 0):4d}  {c.get('carry', 0):5d}  "
              f"${mx:>9,.0f} ({at})  ${endb:,.0f}" + ("" if live_s else "  [no schedule]"))


if __name__ == "__main__":
    main(sys.argv[1:])
