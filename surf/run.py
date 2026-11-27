# surf/run.py - the LolaLooks "surf scaler" for PMax | Testing | UK (Railway cron service lolalooks-surf-scaler).
#
#   python -m surf.run                 auto by the clock: minute 05-19 -> reset at UK 00:05, else idle; other minutes
#                                      -> 30-minute window decision
#   python -m surf.run --mode window   force a window decision (rolling 60 min ending at the last :00 / :30 mark)
#   python -m surf.run --mode reset    force the daily reset
#
# Railway cron is UTC: "5,20,50 * * * *". The :20 run judges the window ending at :00, the :50 run the window ending
# at :30. The :05 run only does something at UK 00:05 (= 00:05 UTC in winter, 23:05 UTC in summer), else it exits.
#
# Window spend (Google Ads has hourly segments only): every run stores the cumulative Testing spend of the current NY
# (account) day as Ads reports it, keyed by the run's 30-minute mark. Window spend = reading(mark) - reading(mark-60),
# previous 60 min = reading(mark-60) - reading(mark-120); every reading is taken ~20 min after its mark, so the Ads
# reporting lag is (nearly) the same at both ends and cancels. Across NY midnight the earlier day's final total is
# added. When a reading is missing (first runs, a missed run) both windows fall back to hourly segments: a finished hour
# split evenly over its two halves, the running hour pro rata (reported spend x 30 / minutes elapsed).
# True sales: Shopify orders by createdAt inside the window, Testing share by the hourly.py split rule.
#
# SURF_LIVE=1 writes the budget; anything else (default) is a DRY RUN: same reads, same decision, a "would do"
# Telegram message, and a virtual budget kept in the state file so the next runs follow the path it would take.
# Variables: GADS_* (Google Ads OAuth, as stores-monitor), SHOPIFY_NEWLOLA_* (new LolaLooks store), TELEGRAM_BOT_TOKEN /
# TELEGRAM_CHAT_ID, SURF_FIRST_STEP / SURF_STEP / SURF_STEP_DOWN and other SURF_* rule overrides (rules.Cfg),
# SURF_DATA_DIR (state + JSON-lines log; default /data when it exists). Secrets are never printed.
import argparse
import datetime as dt
import html
import json
import os
import sys
import traceback
from pathlib import Path

import requests

from . import gads, sales
from .rules import (HALF, NY, UK, UTC, Cfg, Snap, budget_floor, decide, decide_reset, last_mark, manual_changes,
                    new_day_state, ny_run_hours, parse_schedule, remaining_ny_run_hours, runs_at, window_figures)
from .shop import Shop

TITLE = "Surf scaler | PMax Testing UK"
CHANGE_ACTIONS = ("step_up", "step_down", "base_pace", "reset")


def live_mode():
    return (os.environ.get("SURF_LIVE") or "0").strip() == "1"


def data_dir():
    p = os.environ.get("SURF_DATA_DIR") or ("/data" if os.path.isdir("/data") else str(Path(__file__).parent / "_data"))
    Path(p).mkdir(parents=True, exist_ok=True)
    return Path(p)


class Store:
    """State file: {"v": 2, "days": {uk_date: day_state}, "writes": [{at, old_micros, new_micros}],
    "snaps": {mark_iso_utc: {at, ny_date, cum}}, "virtual_budget": dry-run budget (null = use the live budget)}."""

    def __init__(self, path=None):
        self.path = Path(path) if path else data_dir() / "surf_state.json"

    def load(self):
        try:
            st = json.loads(self.path.read_text())
        except Exception:
            st = {}
        if st.get("v") != 2:                      # v1 (hourly rules) day states are not compatible: start fresh days
            st = {"v": 2, "days": {}, "writes": st.get("writes", []), "virtual_budget": st.get("virtual_budget")}
        st.setdefault("days", {}); st.setdefault("writes", []); st.setdefault("snaps", {})
        return st

    def save(self, st):
        days = sorted(st.get("days", {}))
        st["days"] = {k: st["days"][k] for k in days[-10:]}
        st["writes"] = st.get("writes", [])[-100:]
        snaps = sorted(st.get("snaps", {}))
        st["snaps"] = {k: st["snaps"][k] for k in snaps[-16:]}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, indent=1, sort_keys=True))
        tmp.replace(self.path)


def log_line(rec):
    line = json.dumps(rec, default=str, sort_keys=True)
    print("SURF_JSON " + line, flush=True)
    try:
        with open(data_dir() / "surf_runs.jsonl", "a") as f:
            f.write(line + "\n")
    except Exception as e:                    # noqa: BLE001
        print(f"log file not written ({type(e).__name__})")


def telegram(text, html_mode=True):
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (tok and chat):
        print("telegram: not configured - skipped"); return False
    data = {"chat_id": chat, "text": text[:4000]}
    if html_mode:
        data["parse_mode"] = "HTML"
    try:
        r = requests.post(f"https://api.telegram.org/bot{tok}/sendMessage", data=data, timeout=30)
        print("telegram: sent" if r.status_code == 200 else f"telegram: refused (HTTP {r.status_code})")
        return r.status_code == 200
    except Exception as e:                    # noqa: BLE001  (the URL holds the token: print the type only)
        print(f"telegram: failed ({type(e).__name__})"); return False


def _ctx(now_utc):
    """Live campaign facts shared by both modes."""
    camp = gads.campaign()
    problems = []
    if camp["budget_rn"] != gads.BUDGET_RN:
        problems.append(f"campaign budget is {camp['budget_rn']}, expected {gads.BUDGET_RN}")
    if camp["explicitly_shared"]:
        problems.append("budget is shared")
    if camp["period"] not in (None, "DAILY"):
        problems.append(f"budget period {camp['period']}")
    sched = parse_schedule(gads.schedule_rows())
    return camp, problems, sched


def _write(state, old, new, dec):
    """Read back, write amount_micros, verify. Mutates dec."""
    now = gads.campaign()
    if now["amount_micros"] != int(round(old * 1e6)):
        dec["would"] = {"action": dec["action"], "new": new}; dec["action"] = "refused"; dec["new"] = now["amount_micros"] / 1e6
        dec["reasons"].append(f"REFUSED: budget moved to ${now['amount_micros'] / 1e6:,.2f} between read and write")
        return False
    at = gads.set_budget(int(round(new * 1e6)))
    state.setdefault("writes", []).append({"at": at.isoformat(), "old_micros": int(round(old * 1e6)),
                                            "new_micros": int(round(new * 1e6))})
    after = gads.campaign()["amount_micros"]
    dec["verified"] = after == int(round(new * 1e6))
    if not dec["verified"]:
        dec["reasons"].append(f"VERIFY FAILED: budget reads ${after / 1e6:,.2f} after writing ${new:,.2f}")
    return True


# ------------------------------------------------------------------ Telegram table
COLS = [("window", 11, "<"), ("spend", 7, ">"), ("prev", 7, ">"), ("rise", 4, "<"), ("sales", 7, ">"),
        ("ROAS", 5, ">"), ("dROAS", 5, ">"), ("action", 12, "<"), ("old→new", 11, ">"), ("floor", 6, ">")]


def action_label(dec):
    a = dec["action"]
    if a == "step_up":
        return f"UP x{dec.get('mult') or 0:g}" + ("*" if dec.get("carried") else "")
    if a == "carry":
        w = (dec.get("would") or {}).get("action", "")
        return "CARRY " + {"step_up": "UP", "step_down": "DOWN", "base_pace": "BASE"}.get(w, w)
    return {"step_down": "DOWN /1.5", "base_pace": "BASE PACE", "hold": "HOLD", "skip_lag": "SKIP data",
            "no_eval": "NO EVAL", "refused": "REFUSED", "already_evaluated": "DONE", "reset": "RESET",
            "reset_noop": "RESET ok"}.get(a, a.upper())


def table_row(dec):
    n = lambda x, f="{:.2f}": "-" if x is None else f.format(x)          # noqa: E731
    bud = f"{dec['old']:,.0f}→{dec['new']:,.0f}" if abs(dec["new"] - dec["old"]) >= 0.5 else f"{dec['old']:,.0f}"
    if dec.get("would") and dec["action"] in ("carry", "refused"):
        bud = f"({dec['old']:,.0f}→{dec['would']['new']:,.0f})"
    rise = "-" if dec.get("rising") is None else ("yes" if dec["rising"] else "no")
    return {"window": dec.get("window", ""), "spend": n(dec.get("w_spend")), "prev": n(dec.get("p_spend")),
            "rise": rise if "w_spend" in dec else "-", "sales": n(dec.get("w_sales")), "ROAS": n(dec.get("roas")),
            "dROAS": n(dec.get("day_roas")), "action": action_label(dec), "old→new": bud,
            "floor": n(dec.get("floor"), "{:,.0f}")}


def render_table(rows):
    def line(vals):
        return " ".join(f"{str(vals[c])[:w]:{al}{w}}" for c, w, al in COLS)
    return "\n".join([line({c: c for c, _, _ in COLS})] + [line(r) for r in rows])


def message(live, now_uk, dec, rows, foot):
    tag = "LIVE" if live else "DRY RUN"
    out = [f"<b>{html.escape(TITLE)} | {tag} | {now_uk:%a %d %b %H:%M} UK</b>",
           "<pre>" + html.escape(render_table(rows)) + "</pre>"]
    a = dec["action"]
    if a in CHANGE_ACTIONS:
        out.append(("Budget" if live else "Would set budget") + f": ${dec['old']:,.0f} → ${dec['new']:,.0f} ({a})")
    out += ["• " + html.escape(r) for r in dec["reasons"]]
    out += [html.escape(x) for x in foot if x]
    if live and a in CHANGE_ACTIONS:
        out.append("Verified: " + ("yes" if dec.get("verified") else "NO - check the account"))
    return "\n".join(out)


def _keep_snap(snaps, mark, now_utc, ny_date, cum):
    """One reading per mark; keep the one taken closest to 20 min after the mark (the cron reading)."""
    k, new = mark.isoformat(), {"at": now_utc.isoformat(), "ny_date": ny_date, "cum": cum}
    old = snaps.get(k)
    off = lambda s: abs((dt.datetime.fromisoformat(s["at"]) - mark).total_seconds() - 1200)   # noqa: E731
    if old is None or off(new) < off(old):
        snaps[k] = new


def window_run(now_utc, cfg, live, store):
    end = last_mark(now_utc)
    uk_now = now_utc.astimezone(UK)
    uk_date = (end - HALF).astimezone(UK).date()
    day_start = dt.datetime(uk_date.year, uk_date.month, uk_date.day, tzinfo=UK).astimezone(UTC)
    w = dt.timedelta(minutes=cfg.window_min)
    camp, problems, sched = _ctx(now_utc)
    ny_today = now_utc.astimezone(NY).date()
    ny_from = min((min(end - 2 * w, day_start)).astimezone(NY).date(), ny_today - dt.timedelta(days=1))
    seg, day_totals = gads.testing_spend(ny_from.isoformat(), ny_today.isoformat())
    ny_spend = day_totals.get(ny_today.isoformat(), 0.0)
    nyc = len(ny_run_hours(ny_today, sched))
    rem = remaining_ny_run_hours(now_utc, sched)
    floor = budget_floor(ny_spend, cfg.base, nyc, rem, cfg.buffer)

    orders = sales.testing_orders(Shop("NEWLOLA"), min(end - 2 * w, day_start), end)
    g2u, fx_src = sales.gbp_usd(uk_date)
    if g2u is None:
        raise RuntimeError("GBP->USD rate not available")

    st = store.load()
    _keep_snap(st["snaps"], end, now_utc, ny_today.isoformat(), ny_spend)
    fig = window_figures(seg, day_totals, st["snaps"], orders, g2u, end, now_utc, day_start, cfg.window_min)
    key = uk_date.isoformat()
    day = st["days"].get(key) or new_day_state()
    live_budget = camp["amount_micros"] / 1e6
    budget = live_budget if live else (st.get("virtual_budget") or live_budget)
    manual = manual_changes(gads.budget_events(now_utc), st.get("writes"), now_utc)
    status = camp["status"] if not problems else "MISCONFIGURED (" + "; ".join(problems) + ")"
    end_run, cur_run = runs_at(end - HALF, sched), runs_at(now_utc, sched)
    snap = Snap(end=end, w_spend=fig["w_spend"], p_spend=fig["p_spend"], w_sales=fig["w_sales"],
                day_spend=fig["day_spend"], day_sales=fig["day_sales"], end_run=end_run, cur_run=cur_run, floor=floor,
                budget=budget, campaign_status=status, manual_recent=tuple(manual),
                same_day=uk_date == uk_now.date(), last_half_spend=fig["last_half_spend"],
                pace=cfg.base / max(1, nyc))
    dec, day2 = decide(cfg, snap, day)
    if dec["action"] in CHANGE_ACTIONS:
        if live:
            if not _write(st, budget, dec["new"], dec):
                day2["changes"] -= 1
        else:
            st["virtual_budget"] = dec["new"]
    day2.setdefault("rows", []).append(table_row(dec))
    day2["rows"] = day2["rows"][-60:]
    st["days"][key] = day2
    store.save(st)

    foot = [f"Spend method: {fig['spend_method']} | day {uk_date:%d %b}: spend ${fig['day_spend']:,.2f}, "
            f"true sales ${fig['day_sales']:,.2f} | GBP→USD {g2u}",
            f"Floor ${floor:,.2f} = (NY-day Testing spend ${ny_spend:,.2f} + ${cfg.base:,.0f}/{nyc} x {rem} run hrs left) x "
            f"{1 + cfg.buffer:g} | live budget ${live_budget:,.0f} | in run hours: window {'yes' if end_run else 'no'}, "
            f"now {'yes' if cur_run else 'no'}",
            f"Steps: x{cfg.first_mult:g} from base, then x{cfg.step_mult:g}; down /{cfg.down_div:g}; no cap | "
            f"min spend: up ${dec.get('need_up', 0):,.2f}, down ${dec.get('need_down', 0):,.2f} | "
            + (f"last step-up window ends {dt.datetime.fromisoformat(day2['last_up_end']).astimezone(UK):%H:%M} | "
               if day2.get("last_up_end") else "") +
            f"changes today {day2['changes']}" + (f" | pending: {day2['pending']['action']}" if day2.get("pending") else "")]
    rec = {"mode": "window", "live": live, "at": now_utc.isoformat(), "uk_now": uk_now.isoformat(), "uk_date": key,
           "window_end": end.isoformat(), "end_run": end_run, "cur_run": cur_run, "ny_date": ny_today.isoformat(),
           "ny_run_hours": nyc, "remaining_ny_run_hours": rem, "ny_spend_usd": ny_spend, "live_budget": live_budget,
           "campaign_status": camp["status"], "problems": problems, "manual_recent": manual, "fx_gbp_usd": g2u,
           "figures": fig, "decision": dec, "day_state": {k: v for k, v in day2.items() if k not in ("evals", "rows")}}
    rec["notified"] = telegram(message(live, uk_now, dec, day2["rows"][-8:], foot))
    log_line(rec)
    return rec


def reset_run(now_utc, cfg, live, store):
    uk_now = now_utc.astimezone(UK)
    camp, problems, sched = _ctx(now_utc)
    ny_today = now_utc.astimezone(NY).date()
    ny_spend = gads.ny_day_spend(ny_today.isoformat())
    nyc, rem = len(ny_run_hours(ny_today, sched)), remaining_ny_run_hours(now_utc, sched)
    floor = budget_floor(ny_spend, cfg.base, nyc, rem, cfg.buffer)
    st = store.load()
    key = uk_now.date().isoformat()
    day = st["days"].get(key) or new_day_state()
    live_budget = camp["amount_micros"] / 1e6
    budget = live_budget if live else (st.get("virtual_budget") or live_budget)
    manual = manual_changes(gads.budget_events(now_utc), st.get("writes"), now_utc)
    status = camp["status"] if not problems else "MISCONFIGURED (" + "; ".join(problems) + ")"
    dec = decide_reset(cfg, budget, floor, rem, status, tuple(manual))
    dec["window"] = f"{uk_now:%H:%M} reset"
    if dec["action"] == "reset":
        if not live:
            st["virtual_budget"] = dec["new"]; day["changes"] += 1
        elif _write(st, budget, dec["new"], dec):
            day["changes"] += 1
    day.setdefault("rows", []).append(table_row(dec))
    st["days"][key] = day
    store.save(st)
    foot = [f"Floor ${floor:,.2f} = (NY-day Testing spend ${ny_spend:,.2f} + ${cfg.base:,.0f}/{nyc} x {rem} run hrs left) x "
            f"{1 + cfg.buffer:g} | live budget ${live_budget:,.0f}"]
    rec = {"mode": "reset", "live": live, "at": now_utc.isoformat(), "uk_now": uk_now.isoformat(), "ny_date": ny_today.isoformat(),
           "remaining_ny_run_hours": rem, "ny_spend_usd": ny_spend, "live_budget": live_budget,
           "campaign_status": camp["status"], "problems": problems, "manual_recent": manual, "decision": dec}
    rec["notified"] = telegram(message(live, uk_now, dec, day["rows"][-8:], foot))
    log_line(rec)
    return rec


def auto_mode(now_utc):
    uk = now_utc.astimezone(UK)
    if 5 <= uk.minute < 20:
        return "reset" if uk.hour == 0 else "idle"
    return "window"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["auto", "window", "hourly", "reset"], default=os.environ.get("SURF_MODE") or "auto")
    a = ap.parse_args(argv)
    now = dt.datetime.now(UTC)
    uk = now.astimezone(UK)
    mode = {"hourly": "window"}.get(a.mode, a.mode)
    if mode == "auto":
        mode = auto_mode(now)
    live = live_mode()
    if mode == "idle":
        log_line({"mode": "idle", "at": now.isoformat(), "uk_now": uk.isoformat(), "live": live,
                  "note": "the :05 run only resets at UK 00:05"})
        return 0
    try:
        cfg = Cfg.from_env()
        (window_run if mode == "window" else reset_run)(now, cfg, live, Store())
        return 0
    except Exception as e:                    # noqa: BLE001
        traceback.print_exc()
        msg = (f"{TITLE} | {'LIVE' if live else 'DRY RUN'} | {uk:%a %d %b %H:%M} UK\nFAILED ({mode}): "
               f"{type(e).__name__}: {str(e)[:300]}\nNothing was written.")
        log_line({"mode": mode, "at": now.isoformat(), "live": live, "error": f"{type(e).__name__}: {str(e)[:300]}",
                  "notified": telegram(msg, html_mode=False)})
        return 1


if __name__ == "__main__":
    sys.exit(main())
