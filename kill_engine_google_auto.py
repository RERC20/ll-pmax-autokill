#!/usr/bin/env python3
# kill_engine_google_auto.py
# ---------------------------------------------------------------------------
# UNATTENDED Google-direct auto-kill — for running on a schedule (e.g. Claude
# schedule / GitHub repo, hourly). Runs once when CALLED, no user input:
#   1. Pull the live feed (Google Ads cost/clicks + Shopify revenue, UK tz).
#   2. Apply the SAME v4 rules (evaluate) as the manual engine.
#   3. DRAFT every flagged product in Shopify (no yes/no prompt).
#   4. NOTIFY:
#        - TELEGRAM every run  -> run stats + the .xlsx (instant push, no daily cap)
#        - RESEND email twice/day (SUMMARY_HOURS) -> a TEXT digest of the last 12h kills + the .xlsx
#
# Separate from kill_engine_google.py (which asks for confirmation) — it changes
# nothing in the other files; it only REUSES their tested functions.
#
# Usage:
#   python kill_engine_google_auto.py          # live: drafts the kills + notifies
#   python kill_engine_google_auto.py --dry     # safe test: computes + notifies, DRAFTS NOTHING
#   python kill_engine_google_auto.py --test    # like the run but also FORCES the 12h email now (testing)
#
# Setup (env vars, or the git-ignored _secrets_local.py):
#   TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID  (from @BotFather; drives the every-run push)
#   RESEND_API_KEY  (free key from resend.com; twice-daily digest; signup inbox == EMAIL_TO)
# Any unset channel is simply skipped (the run still drafts + logs).
# ---------------------------------------------------------------------------
import sys, os, csv, base64, html, datetime, collections, requests
from openpyxl import Workbook
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
from zoneinfo import ZoneInfo

# reuse the EXACT feed + rules + Shopify write + logging from the existing engines
from kill_engine_google import build_feed
from kill_engine_v4 import evaluate, shopify_token, shopify_draft, _Tee, SHOP, SHOP_API
from creds import cred                          # env -> _secrets_local.py -> '' (so local testing works too)

UK = ZoneInfo('Europe/London')                 # store + Google Ads account run on UK time

# ---- notifications ----
EMAIL_TO       = 'redacted@example.com'
RESEND_API_KEY = cred('RESEND_API_KEY')
RESEND_FROM    = cred('RESEND_FROM') or 'onboarding@resend.dev'
TELEGRAM_TOKEN = cred('TELEGRAM_BOT_TOKEN')     # from @BotFather — every-run push
TELEGRAM_CHAT  = cred('TELEGRAM_CHAT_ID')       # your numeric chat id (@userinfobot)
SUMMARY_HOURS  = (9, 21)                        # UK hours for the twice-a-day (every 12h) email digest
RUN_LOG        = 'kill_engine_auto_runs.log'
KILLS_LOG      = 'kills_log_auto.csv'

# ── Telegram kill formatting: plain-English tier + the rule's reason + core metrics ──
TIER_LABEL = {
    'Tier 1': 'Tier 1 — no sale (70+ clicks)', 'Tier 2': 'Tier 2 — no sale (£5+ or 40+ clicks)',
    'Tier 3': 'Tier 3 — Mon stale no-sale',    'Tier 4': 'Tier 4 — Mon ghost (<5 clicks)',
    'Tier 5': 'Tier 5 — stalled winner',       'Tier 6': 'Tier 6 — below 2.0 ROAS (7d)',
    'Tier 7': 'Tier 7 — slow dribbler (30d)'}
def _fmt_kill(p, tier, why, run_date):
    dl = (run_date - datetime.date.fromisoformat(p['pub'])).days
    return (f"• <b>{html.escape(p['name'][:46])}</b>\n"
            f"  <code>{p['pid']}</code> · <b>{TIER_LABEL.get(tier, tier)}</b> · {dl}d live\n"
            f"  ↳ why: {html.escape(str(why))}\n"
            f"  ↳ spend 7d £{p['cost7']:.2f} / 30d £{p['cost30']:.2f} · ROAS7 {p['roas7']:.2f} · "
            f"rev 30d £{p['rev30']:.2f} · {p['clicks30']} clk")

def _days_live(p, run_date):
    return (run_date - datetime.date.fromisoformat(p['pub'])).days

# ── WINNERS (w_campaign): tag + exempt (owner-approved 2026-07-11) ──────────
# A product with >=1 Shopify sale (GROUND TRUTH — never depends on Google's
# under-reporting) that is still ACTIVE is a "winner". It gets the w_campaign
# tag, which does two things:
#   (a) Simprosys rule maps tag -> custom_label_1=w_campaign -> the product hops
#       from the Testing PMax campaign to the Winners campaign on the next
#       feed sync (near-real-time, tag changes fire Shopify webhooks);
#   (b) EXEMPTS it from this engine's kill rules — winners will get their OWN
#       rules later; until then NO product with a sale is ever drafted here.
#       Only never-sold products (Tiers 1-4 territory) keep dying.
# First sales always appear in rev30 (live Shopify orders pull) within one
# 8-min run. Sales older than 30d were tagged by the one-off backfill
# (backfill_winner_tags.py, run 2026-07-11) — so rev30>0 is a complete signal
# for every NEW first sale going forward.
WINNER_TAG = 'w_campaign'

def shopify_add_tag(tok, pid, tag):
    m = 'mutation($id:ID!,$t:[String!]!){tagsAdd(id:$id,tags:$t){userErrors{message}}}'
    try:
        j = requests.post(f"https://{SHOP}/admin/api/{SHOP_API}/graphql.json",
                          headers={'X-Shopify-Access-Token': tok, 'Content-Type': 'application/json'},
                          json={'query': m, 'variables': {'id': f"gid://shopify/Product/{pid}", 't': [tag]}},
                          timeout=30).json()
        errs = (j.get('data', {}).get('tagsAdd') or {}).get('userErrors') or []
        return 'ok' if not errs else f"err: {errs[0].get('message', '?')[:60]}"
    except Exception as ex:
        return f"err: {ex}"

def tag_new_winners(feed, dry):
    """Tag every ACTIVE product with a Shopify sale (rev30>0) not yet tagged w_campaign."""
    new = [p for p in feed if p['rev30'] > 0 and WINNER_TAG not in p['tags']]
    if not new:
        return []
    tok = None if dry else shopify_token()
    for p in new:
        res = 'DRY (not tagged)' if dry else shopify_add_tag(tok, p['pid'], WINNER_TAG)
        print(f"  {'would tag' if dry else 'tag'} winner {p['pid']} -> {res} | {p['name'][:42]}")
        p['tags'].append(WINNER_TAG)     # exempt from kill rules in THIS same run too
    return new

def build_report(rows, outcomes, run_date, ts, n_active, n_kills, n_drafted, dry):
    wb = Workbook()
    s = wb.active; s.title = 'Summary'
    s.append(['PMax — Google Auto-Kill Run Report'])
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

def send_telegram(text, xlsx_path=None):
    """Every-run push: a text summary + the .xlsx as a document. No daily cap on Telegram."""
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT):
        print("!! TELEGRAM NOT SENT — set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID (env or _secrets_local.py).")
        return False
    base = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"; ok = False
    try:
        r = requests.post(f"{base}/sendMessage",
                          data={'chat_id': TELEGRAM_CHAT, 'text': text, 'parse_mode': 'HTML'}, timeout=30)
        ok = (r.status_code == 200)
        if not ok: print(f"!! Telegram message failed ({r.status_code}): {r.text[:200]}")
        if xlsx_path and os.path.exists(xlsx_path):
            with open(xlsx_path, 'rb') as f:
                rd = requests.post(f"{base}/sendDocument",
                                   data={'chat_id': TELEGRAM_CHAT}, files={'document': f}, timeout=60)
            if rd.status_code != 200: print(f"!! Telegram document failed ({rd.status_code}): {rd.text[:200]}")
        if ok: print(f"Telegram sent to chat {TELEGRAM_CHAT}.")
    except Exception as ex:
        print(f"!! TELEGRAM FAILED: {ex}")
    return ok

def _kills_last_12h():
    """Rows actually DRAFTED (outcome 'ok') in the last 12h, from kills_log_auto.csv."""
    if not os.path.exists(KILLS_LOG): return []
    cutoff = datetime.datetime.now(UK) - datetime.timedelta(hours=12); out = []
    with open(KILLS_LOG, encoding='utf-8') as f:
        for row in csv.DictReader(f):
            try: t = datetime.datetime.fromisoformat(row['timestamp']).replace(tzinfo=UK)
            except Exception: continue
            if t >= cutoff and row.get('outcome') == 'ok': out.append(row)
    return out

def build_12h_report(rows, ts):
    wb = Workbook(); s = wb.active; s.title = '12h drafted'
    s.append(['Auto-Kill — 12h digest', ts]); s.append(['Products drafted (12h)', len(rows)])
    s.append(['By tier'] + [f'{k}: {v}' for k, v in sorted(collections.Counter(r['tier'] for r in rows).items())])
    s.append([]); s.append(['timestamp', 'product_id', 'name', 'tier', 'reason', 'cost_30d', 'clicks_30d', 'roas_7d'])
    for r in rows:
        s.append([r['timestamp'], int(r['product_id']), r['name'], r['tier'], r['reason'],
                  r['cost_30d'], r['clicks_30d'], r['roas_7d']])
    fname = f"auto_kill_12h_{datetime.datetime.now(UK).strftime('%Y-%m-%d_%H%M')}.xlsx"; wb.save(fname); return fname

def maybe_send_12h_email(ts, force=False):
    """Twice a day (SUMMARY_HOURS) email a TEXT digest of the last 12h kills + the .xlsx."""
    now = datetime.datetime.now(UK)
    if not (force or (now.hour in SUMMARY_HOURS and now.minute < 8)): return
    rows = _kills_last_12h()
    tiers = collections.Counter(r['tier'] for r in rows)
    lines = [f"- {r['product_id']} [{r['tier']}] {r['name'][:44]} | £{r['cost_30d']}/30d, {r['clicks_30d']} clk"
             for r in rows[:50]]
    body = (f"Auto-Kill — 12-hour digest\n{ts} (UK)\n\n"
            f"PRODUCTS DRAFTED (last 12h): {len(rows)}\n"
            f"By tier: {dict(tiers) or '-'}\n\n"
            + ("\n".join(lines) if lines else "(nothing drafted in the last 12 hours)")
            + (f"\n...and {len(rows)-50} more" if len(rows) > 50 else "")
            + "\n\n(Full list also in the attached Excel.)")
    send_report(f"12h Auto-Kill — {len(rows)} drafted", body, build_12h_report(rows, ts) if rows else None)

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
            send_telegram(f"⚠️ <b>Auto-Kill ABORTED</b>\n{ts} UK\n{msg}")
            send_report("ALERT: auto-kill ABORTED — no Shopify orders data",
                        f"PMax auto-kill — {ts} (UK)\nMode: {'DRY-RUN' if dry else 'LIVE'}\n\n{msg}")
            return

        # WINNERS first: tag new 1+ sale actives, then EXEMPT all tagged from the kill rules
        new_winners = tag_new_winners(feed, dry)
        exempt = sum(1 for p in feed if WINNER_TAG in p['tags'])
        print(f"winners: +{len(new_winners)} newly tagged | {exempt} total exempt from kill rules")

        kills = []
        for p in feed:
            if WINNER_TAG in p['tags']:
                continue                      # winners: separate rules later — never killed here
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

        # log FIRST so the 12h digest can include this run's kills
        _write_kills_log(to_draft, outcomes, run_date, ts, dry)
        print(f"logs: run -> {RUN_LOG} | kills -> {KILLS_LOG}")

        # TELEGRAM — every run: run stats + the .xlsx
        n = len(to_draft) if dry else drafted
        tg = (f"🤖 <b>Auto-Kill</b> — {n} drafted{' (DRY)' if dry else ''}\n"
              f"{ts} UK · {run_date.strftime('%a')}\n"
              f"Active: {len(feed)} | winners exempt: {exempt} | kills found: {len(kills)} | drafted: {n}")
        if new_winners:
            tg += ("\n🏆 <b>new winners → w_campaign:</b> "
                   + ", ".join(f"<code>{p['pid']}</code>" for p in new_winners[:10])
                   + (f" +{len(new_winners)-10} more" if len(new_winners) > 10 else ""))
        if to_draft:
            DETAIL = 15                                     # full reason+metrics for up to 15; rest in the Excel
            tg += "\n\n" + "\n\n".join(_fmt_kill(p, tier, why, run_date) for p, tier, why in to_draft[:DETAIL])
            if len(to_draft) > DETAIL:
                tg += f"\n\n…+{len(to_draft)-DETAIL} more — full reasons &amp; metrics in the attached Excel."
        send_telegram(tg, xlsx)

        # RESEND email — twice a day (SUMMARY_HOURS): TEXT digest of last 12h + the .xlsx
        maybe_send_12h_email(ts, force=('--test' in sys.argv or '--email' in sys.argv))
    except Exception:
        import traceback
        err = traceback.format_exc()
        print("!! AUTO-KILL RUN FAILED — nothing further drafted:\n" + err)
        try:
            send_telegram(f"❌ <b>Auto-Kill FAILED</b>\n{ts} UK\n<pre>{err[-500:]}</pre>")
            send_report(f"AUTO-KILL FAILED — {run_date}",
                        f"auto-kill run FAILED at {ts} (UK).\n\nError:\n{err}")
        except Exception:
            pass
    finally:
        sys.stdout = real
        logf.close()

if __name__ == '__main__':
    main()
