# surf/run.py - the LolaLooks "surf scaler" for PMax | Testing | UK (Railway cron service lolalooks-surf-scaler).
#
#   python -m surf.run                 auto by the UK clock: minute >= 30 -> hourly decision; UK 00:00-00:29 -> reset
#   python -m surf.run --mode hourly   force an hourly decision (evaluates the previous full UK hour)
#   python -m surf.run --mode reset    force the daily reset
#
# Railway cron is UTC: "5,50 * * * *". Every :50 run evaluates the previous full UK hour; the :05 run only does
# something at UK 00:05 (the daily reset; = 00:05 UTC in winter, 23:05 UTC in summer), otherwise it exits.
#
# SURF_LIVE=1 writes the budget; anything else (default) is a DRY RUN: same reads, same decision, a "would do"
# Telegram message, and a virtual budget kept in the state file so the next hours follow the path it would take.
# Variables: GADS_* (Google Ads OAuth, as stores-monitor), SHOPIFY_NEWLOLA_* (new LolaLooks store, as the stores-monitor
# lola feed), TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID (the kill engine's bot), SURF_* rule overrides (rules.Cfg),
# SURF_DATA_DIR (state + JSON-lines log; default /data when it exists). Secrets are never printed.
import argparse
import datetime as dt
import json
import os
import sys
import traceback
from pathlib import Path

import requests

from . import gads, hourly
from .rules import (NY, UK, UTC, Cfg, Snap, budget_floor, decide, decide_reset, manual_changes, new_day_state,
                    ny_run_hours, parse_schedule, remaining_ny_run_hours, testing_hours, uk_run_hours)
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
    """State file: {"days": {uk_date: day_state}, "writes": [{at, old_micros, new_micros}]} (last 10 days kept)."""

    def __init__(self, path=None):
        self.path = Path(path) if path else data_dir() / "surf_state.json"

    def load(self):
        try:
            return json.loads(self.path.read_text())
        except Exception:
            return {"days": {}, "writes": []}

    def save(self, st):
        days = sorted(st.get("days", {}))
        st["days"] = {k: st["days"][k] for k in days[-10:]}
        st["writes"] = st.get("writes", [])[-100:]
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


def telegram(text):
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (tok and chat):
        print("telegram: not configured - skipped"); return False
    try:
        r = requests.post(f"https://api.telegram.org/bot{tok}/sendMessage", data={"chat_id": chat, "text": text[:4000]},
                          timeout=30)
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
    ny_today = now_utc.astimezone(NY).date()
    nyc = len(ny_run_hours(ny_today, sched))
    rem = remaining_ny_run_hours(now_utc, sched)
    ny_spend = gads.ny_day_spend(ny_today.isoformat())
    return camp, problems, sched, ny_today, nyc, rem, ny_spend


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


def money(x):
    return "n/a" if x is None else f"${x:,.2f}"


def message(mode_tag, live, now_uk, dec, extra):
    tag = "LIVE" if live else "DRY RUN"
    a = dec["action"]
    lines = [f"{TITLE} | {tag} | {now_uk:%a %d %b %H:%M} UK"]
    if "h" in dec:
        h = dec["h"]
        lines.append(f"Hour {h:02d}:00-{(h + 1) % 24:02d}:00 UK: spend {money(dec['h_spend'])} (GBP {extra.get('h_spend_gbp', 0):,.2f}), "
                     f"true sales {money(dec['h_sales'])} (GBP {extra.get('h_sales_gbp', 0):,.2f}), ROAS {dec['roas'] if dec['roas'] is not None else 'n/a'}")
        lines.append(f"Day to date: spend {money(dec['day_spend'])}, true sales {money(dec['day_sales'])}, ROAS {dec['day_roas'] if dec['day_roas'] is not None else 'n/a'}")
    changed = a in CHANGE_ACTIONS
    if changed:
        verb = "Budget" if live else "Would set budget"
        lines.append(f"{verb}: ${dec['old']:,.0f} -> ${dec['new']:,.0f} ({a})")
    else:
        lines.append(f"Decision: {a.upper()} - budget ${dec['old']:,.0f} -> ${dec['new']:,.0f} (unchanged)")
        if dec.get("would"):
            lines.append(f"(would have: {dec['would']['action']} -> ${dec['would']['new']:,.0f})")
    lines += ["- " + r for r in dec["reasons"]]
    lines.append(f"Floor ${dec.get('floor', 0):,.2f} = (NY-day Testing spend {money(extra.get('ny_spend'))} + base pace x "
                 f"{extra.get('rem')} run hrs left) x 1.1 | live budget ${extra.get('live_budget', 0):,.0f}")
    if extra.get("flags"):
        lines.append(extra["flags"])
    if live and changed:
        lines.append("Verified: " + ("yes" if dec.get("verified") else "NO - check the account"))
    return "\n".join(lines)


def hourly_run(now_utc, cfg, live, store):
    cur_start = now_utc.replace(minute=0, second=0, microsecond=0)
    H = (cur_start - dt.timedelta(hours=1)).astimezone(UK)
    uk_now = now_utc.astimezone(UK)
    camp, problems, sched, ny_today, nyc, rem, ny_spend = _ctx(now_utc)
    run_h = uk_run_hours(H.date(), sched)
    cur_run = uk_now.hour in uk_run_hours(uk_now.date(), sched)
    floor = budget_floor(ny_spend, cfg.base, nyc, rem, cfg.buffer)

    body = hourly.build(H.date(), H.date(), Shop("NEWLOLA"))
    errs = [k for k in ("ads", "shopify") if not body[k]["ok"]] + ([] if body["split_rules"]["ok"] else ["split"])
    if body["fx"]["gbp_usd_source"] is None:
        errs.append("fx")
    if errs:
        raise RuntimeError(f"hourly data not complete ({', '.join(errs)}): "
                           f"{body['ads'].get('error') or ''} {body['shopify'].get('error') or ''} {body['split_rules'].get('error') or ''}"[:300])
    th = testing_hours(body, H.date().isoformat())
    hs, hsa, hsg, hsag = th.get(H.hour, (0.0, 0.0, 0.0, 0.0))
    ds = sum(v[0] for k, v in th.items() if k <= H.hour)
    dsa = sum(v[1] for k, v in th.items() if k <= H.hour)

    st = store.load()
    key = H.date().isoformat()
    day = st["days"].get(key) or new_day_state()
    live_budget = camp["amount_micros"] / 1e6
    budget = live_budget if live else (day.get("virtual_budget") or live_budget)
    manual = manual_changes(gads.budget_events(now_utc), st.get("writes"), now_utc)
    status = camp["status"] if not problems else "MISCONFIGURED (" + "; ".join(problems) + ")"
    snap = Snap(h=H.hour, h_spend=hs, h_sales=hsa, day_spend=ds, day_sales=dsa, uk_run_hours=run_h, cur_run=cur_run,
                ny_run_hours_count=nyc, floor=floor, budget=budget, campaign_status=status, manual_recent=tuple(manual))
    dec, day2 = decide(cfg, snap, day)
    if dec["action"] in CHANGE_ACTIONS:
        if live:
            if not _write(st, budget, dec["new"], dec):
                day2["changes"] -= 1
        else:
            day2["virtual_budget"] = dec["new"]
    st["days"][key] = day2
    store.save(st)

    flags = (f"Flags: base_lock={day2['base_lock']} day_guard={day2['guard']} stepped_up={day2['stepped_up']} "
             f"changes_today={day2['changes']}/{cfg.max_changes} | run hours UK {H.date():%d %b}: {run_h}")
    extra = {"h_spend_gbp": hsg, "h_sales_gbp": hsag, "ny_spend": ny_spend, "rem": rem, "live_budget": live_budget,
             "flags": flags}
    rec = {"mode": "hourly", "live": live, "at": now_utc.isoformat(), "uk_now": uk_now.isoformat(),
           "h_date": key, "cur_run": cur_run, "uk_run_hours": run_h, "ny_date": ny_today.isoformat(),
           "ny_run_hours": nyc, "remaining_ny_run_hours": rem, "ny_spend_usd": ny_spend, "live_budget": live_budget,
           "campaign_status": camp["status"], "problems": problems, "manual_recent": manual,
           "fx_gbp_usd": next((d["gbp_usd"] for d in body["days"] if d["date"] == key), None),
           "decision": dec, "day_state": {k: v for k, v in day2.items() if k != "evals"}}
    notify = cur_run or H.hour in run_h or dec["action"] not in ("no_eval",) or os.environ.get("SURF_NOTIFY_ALL") == "1"
    rec["notified"] = telegram(message("hourly", live, uk_now, dec, extra)) if notify else False
    log_line(rec)
    return rec


def reset_run(now_utc, cfg, live, store):
    uk_now = now_utc.astimezone(UK)
    camp, problems, sched, ny_today, nyc, rem, ny_spend = _ctx(now_utc)
    floor = budget_floor(ny_spend, cfg.base, nyc, rem, cfg.buffer)
    st = store.load()
    key = uk_now.date().isoformat()
    day = st["days"].get(key) or new_day_state()
    live_budget = camp["amount_micros"] / 1e6
    budget = live_budget if live else (day.get("virtual_budget") or live_budget)
    manual = manual_changes(gads.budget_events(now_utc), st.get("writes"), now_utc)
    status = camp["status"] if not problems else "MISCONFIGURED (" + "; ".join(problems) + ")"
    dec = decide_reset(cfg, budget, floor, rem, status, tuple(manual), day["changes"])
    if dec["action"] == "reset":
        if not live:
            day["virtual_budget"] = dec["new"]; day["changes"] += 1
        elif _write(st, budget, dec["new"], dec):
            day["changes"] += 1
    st["days"][key] = day
    store.save(st)
    extra = {"ny_spend": ny_spend, "rem": rem, "live_budget": live_budget}
    rec = {"mode": "reset", "live": live, "at": now_utc.isoformat(), "uk_now": uk_now.isoformat(), "ny_date": ny_today.isoformat(),
           "remaining_ny_run_hours": rem, "ny_spend_usd": ny_spend, "live_budget": live_budget,
           "campaign_status": camp["status"], "problems": problems, "manual_recent": manual, "decision": dec}
    rec["notified"] = telegram(message("reset", live, uk_now, dec, extra))
    log_line(rec)
    return rec


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["auto", "hourly", "reset"], default=os.environ.get("SURF_MODE") or "auto")
    a = ap.parse_args(argv)
    now = dt.datetime.now(UTC)
    uk = now.astimezone(UK)
    mode = a.mode
    if mode == "auto":
        mode = "hourly" if uk.minute >= 30 else ("reset" if uk.hour == 0 else "idle")
    live, cfg = live_mode(), Cfg.from_env()
    if mode == "idle":
        log_line({"mode": "idle", "at": now.isoformat(), "uk_now": uk.isoformat(), "live": live,
                  "note": "the :05 run only resets at UK 00:05"})
        return 0
    try:
        (hourly_run if mode == "hourly" else reset_run)(now, cfg, live, Store())
        return 0
    except Exception as e:                    # noqa: BLE001
        traceback.print_exc()
        msg = f"{TITLE} | {'LIVE' if live else 'DRY RUN'} | {uk:%a %d %b %H:%M} UK\nFAILED ({mode}): {type(e).__name__}: {str(e)[:300]}\nNothing was written."
        log_line({"mode": mode, "at": now.isoformat(), "live": live, "error": f"{type(e).__name__}: {str(e)[:300]}",
                  "notified": telegram(msg)})
        return 1


if __name__ == "__main__":
    sys.exit(main())
