#!/usr/bin/env python3
# kill_engine_google_auto.py
# ---------------------------------------------------------------------------
# UNATTENDED Google-direct auto-kill — for running on a schedule (e.g. Claude
# schedule / GitHub repo, hourly). Runs once when CALLED, no user input:
#   1. Pull the live feed (Google Ads cost/clicks + Shopify revenue, UK tz).
#   2. Apply the SAME v4 rules (evaluate) as the manual engine.
#   3. DRAFT every flagged product in Shopify (no yes/no prompt).
#   4. Build a detailed .xlsx report and EMAIL it to redacted@example.com
#      with the subject "<N> Products Draft".
#
# Separate from kill_engine_google.py (which asks for confirmation) — it changes
# nothing in the other files; it only REUSES their tested functions.
#
# Usage:
#   python kill_engine_google_auto.py          # live: drafts the kills + emails the report
#   python kill_engine_google_auto.py --dry     # safe test: computes + reports + emails, but DRAFTS NOTHING
#
# Email setup (one time): set ONE repo secret —
#   RESEND_API_KEY = a free API key from https://resend.com  (no Gmail / app-password / SMTP).
#   Sign up at resend.com with the SAME inbox as EMAIL_TO, so the default
#   onboarding@resend.dev sender can deliver to it without verifying a domain.
# If it's unset, the run still drafts + saves the report locally and just
# prints that the email was skipped.
# ---------------------------------------------------------------------------
import sys, os, csv, base64, datetime, collections, requests
from openpyxl import Workbook
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
from zoneinfo import ZoneInfo

# reuse the EXACT feed + rules + Shopify write + logging from the existing engines
from kill_engine_google import build_feed
from kill_engine_v4 import evaluate, shopify_token, shopify_draft, _Tee

UK = ZoneInfo('Europe/London')                 # store + Google Ads account run on UK time

EMAIL_TO       = 'redacted@example.com'
RESEND_API_KEY = os.environ.get('RESEND_API_KEY', '')           # free API key from resend.com (repo secret)
RESEND_FROM    = os.environ.get('RESEND_FROM', 'onboarding@resend.dev')  # default test sender (works without a domain)
RUN_LOG        = 'kill_engine_auto_runs.log'
KILLS_LOG      = 'kills_log_auto.csv'

def _days_live(p, run_date):
    return (run_date - datetime.date.fromisoformat(p['pub'])).days

def build_report(rows, outcomes, run_date, ts, n_active, n_kills, n_drafted, dry):
    wb = Workbook()
    s = wb.active; s.title = 'Summary'
    s.append(['the-store PMax — Google Auto-Kill Run Report'])
    s.append(['Run (UK time)', ts])
    s.append(['Data date', str(run_date), run_date.strftime('%A')])
    s.append(['Mode', 'DRY-RUN (nothing drafted)' if dry else 'LIVE (products drafted)'])
    s.append(['Active products analyzed', n_active])
    s.append(['Kills found by rules', n_kills])
    s.append(['Products drafted', n_drafted])
    tiers = collections.Counter(t for (_, t, _) in rows)
    s.append(['By tier'] + [f'{k}: {v}' for k, v in sorted(tiers.items())])
    s.append([])
    s.append(['Revenue is GROSS (refunds never deducted). Cost/clicks from Google Ads; revenue from Shopify; windows UK-aligned.'])

    d = wb.create_sheet('Drafted')
    d.append(['Product ID', 'Product Name', 'Kill Tier', 'Reason', 'days_live',
              'cost_7d', 'cost_30d', 'clicks_30d', 'rev_30d', 'rev_14d', 'rev_7d', 'roas_7d', 'outcome'])
    for p, tier, why in rows:
        d.append([int(p['pid']), p['name'], tier, why, _days_live(p, run_date),
                  round(p['cost7'], 2), round(p['cost30'], 2), p['clicks30'],
                  round(p['rev30'], 2), round(p['rev14'], 2), round(p['rev7'], 2),
                  round(p['roas7'], 2), outcomes.get(p['pid'], '')])
    for c in d['A'][1:]: c.number_format = '0'

    fname = f"auto_kill_report_{run_date}_{datetime.datetime.now(UK).strftime('%H%M')}.xlsx"
    wb.save(fname)
    return fname

def send_report(subject, body, xlsx_path=None):
    if not RESEND_API_KEY:
        print("!! EMAIL NOT SENT — set the RESEND_API_KEY secret (free key from resend.com).")
        if xlsx_path: print(f"   report saved locally: {xlsx_path}")
        return False
    try:
        payload = {'from': RESEND_FROM, 'to': [EMAIL_TO], 'subject': subject, 'text': body}
        if xlsx_path:
            with open(xlsx_path, 'rb') as f:
                payload['attachments'] = [{'filename': os.path.basename(xlsx_path),
                                           'content': base64.b64encode(f.read()).decode()}]
        r = requests.post('https://api.resend.com/emails',
                          headers={'Authorization': f'Bearer {RESEND_API_KEY}'}, json=payload, timeout=30)
        if r.status_code in (200, 201):
            print(f'Email sent to {EMAIL_TO} via Resend: "{subject}"')
            return True
        print(f"!! EMAIL FAILED (Resend {r.status_code}): {r.text[:200]}")
        return False
    except Exception as ex:
        print(f"!! EMAIL FAILED: {ex}   (report saved: {xlsx_path})")
        return False

def _write_kills_log(rows, outcomes, run_date, ts, dry):
    new = not os.path.exists(KILLS_LOG)
    with open(KILLS_LOG, 'a', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        if new:
            w.writerow(['timestamp', 'data_date', 'mode', 'product_id', 'name', 'tier', 'reason', 'days_live',
                        'cost_7d', 'cost_30d', 'clicks_30d', 'rev_30d', 'rev_14d', 'rev_7d', 'roas_7d', 'outcome'])
        mode = 'DRY' if dry else 'LIVE'
        for p, tier, why in rows:
            w.writerow([ts, run_date, mode, p['pid'], p['name'], tier, why, _days_live(p, run_date),
                        round(p['cost7'], 2), round(p['cost30'], 2), p['clicks30'],
                        round(p['rev30'], 2), round(p['rev14'], 2), round(p['rev7'], 2),
                        round(p['roas7'], 2), outcomes.get(p['pid'], '')])

def main():
    dry = '--dry' in sys.argv or '--dry-run' in sys.argv
    run_date = datetime.datetime.now(UK).date()
    is_monday = (run_date.weekday() == 0)
    ts = datetime.datetime.now(UK).strftime('%Y-%m-%d %H:%M:%S')

    logf = open(RUN_LOG, 'a', encoding='utf-8')
    logf.write(f"\n{'#'*72}\n# AUTO RUN {ts} (UK) | GOOGLE-DIRECT | {'DRY' if dry else 'LIVE'} | data-date {run_date}\n{'#'*72}\n")
    real = sys.stdout; sys.stdout = _Tee(real, logf)
    try:
        print(f"== kill_engine GOOGLE AUTO == {run_date} ({run_date.strftime('%A')}) | "
              f"Mon tiers 3&4 {'ON' if is_monday else 'OFF'} | {'DRY-RUN' if dry else 'LIVE'} | {ts}")
        feed = build_feed(run_date)
        print(f"feed: Google Ads + live Shopify -> {len(feed)} active products")

        # GLITCH GUARD: a live store ALWAYS has some Shopify revenue over 30 days. If the orders
        # pull comes back empty (£0 across EVERY product) it's a data glitch (failed/empty Shopify
        # response), not reality — every spending product would falsely read £0-revenue and get
        # killed. So abort + alert and draft NOTHING. This caps nothing real: a legit big batch
        # still has normal order data; only the empty-data glitch trips it.
        total_rev30 = sum(p.get('rev30', 0) for p in feed)
        if total_rev30 <= 0:
            msg = (f"ABORTED — Shopify returned £0 revenue across ALL {len(feed)} active products over 30 days. "
                   f"That's an empty/failed orders pull, not real sales. NOTHING was drafted; re-run after Shopify recovers.")
            print("!! " + msg)
            send_report("ALERT: auto-kill ABORTED — no Shopify orders data",
                        f"the-store PMax auto-kill — {ts} (UK)\nMode: {'DRY-RUN' if dry else 'LIVE'}\n\n{msg}")
            return

        kills = []
        for p in feed:
            dec, tier, why = evaluate(p, run_date, is_monday)
            if dec == 'KILL':
                kills.append((p, tier, why))

        # no cap — draft EVERY product the rules flag
        to_draft = kills
        print(f"kills found: {len(kills)}")
        outcomes = {}
        if to_draft:
            wtok = None if dry else shopify_token()
            for p, tier, why in to_draft:
                res = 'DRY (not drafted)' if dry else shopify_draft(wtok, p['pid'])
                outcomes[p['pid']] = res
                print(f"  {'would draft' if dry else 'draft'} {p['pid']} [{tier}] -> {res} | {p['name'][:42]}")
        drafted = 0 if dry else sum(1 for v in outcomes.values() if v == 'ok')

        xlsx = build_report(to_draft, outcomes, run_date, ts, len(feed), len(kills), drafted, dry)
        print(f"report: {xlsx}")

        n_for_subject = len(to_draft) if dry else drafted
        subject = f"{n_for_subject} Products Draft" + (" (DRY-RUN)" if dry else "")
        body = (f"the-store PMax auto-kill run — {ts} (UK)\n"
                f"Mode: {'DRY-RUN (nothing drafted)' if dry else 'LIVE'}\n"
                f"Data date: {run_date} ({run_date.strftime('%A')})\n"
                f"Active products analyzed: {len(feed)}\n"
                f"Kills found by rules: {len(kills)}\n"
                f"Products {'that WOULD be drafted' if dry else 'drafted'}: {len(to_draft) if dry else drafted}\n"
                + "\nFull breakdown with reasons + metrics is in the attached Excel report.")
        send_report(subject, body, xlsx)

        _write_kills_log(to_draft, outcomes, run_date, ts, dry)
        print(f"logs: run -> {RUN_LOG} | kills -> {KILLS_LOG}")
    except Exception:
        import traceback
        err = traceback.format_exc()
        print("!! AUTO-KILL RUN FAILED — nothing further drafted:\n" + err)
        try:
            send_report(f"AUTO-KILL FAILED — {run_date}",
                        f"the-store auto-kill run FAILED at {ts} (UK).\n\nError:\n{err}")
        except Exception:
            pass
    finally:
        sys.stdout = real
        logf.close()

if __name__ == '__main__':
    main()
