#!/usr/bin/env python3
# daily_product_count.py — DAILY ACTIVE-PRODUCT SNAPSHOT + Telegram
# ---------------------------------------------------------------------------
# Shopify keeps NO history of how many products were active on a past day, so we
# record it ourselves. Twice a day (UK time):
#   • START  (~00:11) : snapshot how many ACTIVE products the day is starting with
#   • END    (~23:49) : snapshot how many it ended with, and diff START->END to show
#                       exactly how many were PUBLISHED vs KILLED during the day.
# Each snapshot is pushed to Telegram as its OWN clearly-dated message (separate from
# the kill messages, so it's easy to spot). A permanent CSV history is also kept.
#
# State (kept OUT of the public repo via the _*.json / _*.csv gitignore rules, and
# persisted across GitHub Actions runs via the private Actions cache — same pattern
# as kills_log_auto.csv):
#   _product_count_state.json    today's START snapshot (date, count, id list)
#   _product_count_history.csv   permanent: date, start, end, published, killed, net
#
# Modes (mode is auto-resolved from UK local time if not given):
#   --mode start   record start-of-day  (auto when UK hour < 12)
#   --mode end     record end-of-day    (auto when UK hour >= 12)
#   --mode auto    decide by UK clock (default)
#   --dry          compute + print, but DO NOT send Telegram / write state (local testing)
#   --force        ignore the once-per-day idempotency guard (re-send)
# ---------------------------------------------------------------------------
import sys, os, json, csv, argparse, datetime, requests
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
from zoneinfo import ZoneInfo
from kill_engine_v4 import shopify_token, norm, SHOP, SHOP_API
from creds import cred

UK = ZoneInfo('Europe/London')
STATE_FILE   = '_product_count_state.json'
HISTORY_FILE = '_product_count_history.csv'
HIST_COLS    = ['date', 'start_count', 'end_count', 'published', 'killed', 'net']
# DEDICATED bot for the daily summary / active-product counts, so these land in a SEPARATE chat
# from the auto-kill messages (owner 2026-07-07). Falls back to the shared auto-kill bot if the
# SUMMARY_* vars aren't set yet, so nothing breaks before the 2nd bot is created.
TG_TOKEN = cred('TELEGRAM_SUMMARY_BOT_TOKEN') or cred('TELEGRAM_BOT_TOKEN')
TG_CHAT  = cred('TELEGRAM_SUMMARY_CHAT_ID')  or cred('TELEGRAM_CHAT_ID')

def _gql(tok, q, v=None):
    return requests.post(f"https://{SHOP}/admin/api/{SHOP_API}/graphql.json",
        headers={'X-Shopify-Access-Token': tok, 'Content-Type': 'application/json'},
        json={'query': q, 'variables': v or {}}, timeout=60).json()

def fetch_active_ids(tok):
    """Every live ACTIVE product id (paginated). len() = the active count."""
    Q = ('query($c:String){products(first:250,after:$c,query:"status:active"){'
         'pageInfo{hasNextPage endCursor} edges{node{legacyResourceId}}}}')
    ids = set(); cur = None
    while True:
        d = _gql(tok, Q, {'c': cur})['data']['products']
        for e in d['edges']:
            ids.add(norm(e['node']['legacyResourceId']))
        if d['pageInfo']['hasNextPage']: cur = d['pageInfo']['endCursor']
        else: break
    return ids

def send_telegram(text):
    if not (TG_TOKEN and TG_CHAT):
        print("!! TELEGRAM NOT SENT — TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing."); return False
    try:
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                          data={'chat_id': TG_CHAT, 'text': text, 'parse_mode': 'HTML'}, timeout=30)
        ok = (r.status_code == 200)
        print(f"Telegram sent to {TG_CHAT}." if ok else f"!! Telegram failed ({r.status_code}): {r.text[:180]}")
        return ok
    except Exception as ex:
        print(f"!! TELEGRAM FAILED: {ex}"); return False

def load_history():
    rows = {}
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, newline='', encoding='utf-8') as f:
            for r in csv.DictReader(f): rows[r['date']] = r
    return rows

def write_history(rows):
    with open(HISTORY_FILE, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=HIST_COLS); w.writeheader()
        for d in sorted(rows): w.writerow(rows[d])

def fmt_date(d): return d.strftime('%a %-d %b %Y') if os.name != 'nt' else d.strftime('%a ') + str(d.day) + d.strftime(' %b %Y')

def do_start(tok, today, dry, force):
    if not force and os.path.exists(STATE_FILE):
        try: st = json.load(open(STATE_FILE, encoding='utf-8'))
        except Exception: st = {}
        if st.get('start_date') == today.isoformat():
            print(f"START already recorded for {today} (count={st.get('start_count')}). Skipping (idempotent)."); return
    ids = fetch_active_ids(tok); n = len(ids)
    msg = ("\U0001F4CA <b>PRODUCTS — START OF DAY</b>\n"
           f"\U0001F5D3 {fmt_date(today)}\n"
           "━━━━━━━━━━━\n"
           f"\U0001F7E2 Active at start: <b>{n}</b>")
    print(f"[START {today}] active={n}"); print(msg.replace('<b>','').replace('</b>',''))
    if dry: print("(dry-run: not sending / not writing state)"); return
    json.dump({'start_date': today.isoformat(), 'start_count': n, 'start_ids': sorted(ids)},
              open(STATE_FILE, 'w', encoding='utf-8'))
    rows = load_history()
    row = rows.get(today.isoformat(), {c: '' for c in HIST_COLS}); row['date'] = today.isoformat()
    row['start_count'] = n
    rows[today.isoformat()] = row; write_history(rows)
    send_telegram(msg)

def do_end(tok, today, dry, force):
    rows = load_history()
    existing = rows.get(today.isoformat())
    if not force and existing and str(existing.get('end_count') or '') != '':
        print(f"END already recorded for {today} (end={existing['end_count']}). Skipping (idempotent)."); return
    ids_now = fetch_active_ids(tok); end_n = len(ids_now)
    start_n = None; start_ids = None
    if os.path.exists(STATE_FILE):
        try:
            st = json.load(open(STATE_FILE, encoding='utf-8'))
            if st.get('start_date') == today.isoformat():
                start_n = st.get('start_count'); start_ids = set(st.get('start_ids') or [])
        except Exception: pass
    if start_ids is not None:
        published = len(ids_now - start_ids); killed = len(start_ids - ids_now); net = end_n - start_n
        net_line = (f"\U0001F7E2 +{net}" if net > 0 else (f"\U0001F53B −{abs(net)}" if net < 0 else "➖ 0"))
        msg = ("\U0001F4CA <b>PRODUCTS — END OF DAY</b>\n"
               f"\U0001F5D3 {fmt_date(today)}\n"
               "━━━━━━━━━━━\n"
               f"Started:  <b>{start_n}</b>\n"
               f"Ended:    <b>{end_n}</b>\n"
               f"Net:      {net_line}\n"
               f"   ➕ published: {published}\n"
               f"   \U0001F53B killed:    {killed}")
    else:
        published = killed = ''; net = ''
        msg = ("\U0001F4CA <b>PRODUCTS — END OF DAY</b>\n"
               f"\U0001F5D3 {fmt_date(today)}\n"
               "━━━━━━━━━━━\n"
               f"Active at end: <b>{end_n}</b>\n"
               "<i>(start-of-day snapshot missing — no diff this day)</i>")
    print(f"[END {today}] start={start_n} end={end_n} published={published} killed={killed} net={net}")
    print(msg.replace('<b>','').replace('</b>','').replace('<i>','').replace('</i>',''))
    if dry: print("(dry-run: not sending / not writing history)"); return
    row = rows.get(today.isoformat(), {c: '' for c in HIST_COLS}); row['date'] = today.isoformat()
    if start_n is not None and (row.get('start_count') in ('', None)): row['start_count'] = start_n
    row['end_count'] = end_n; row['published'] = published; row['killed'] = killed; row['net'] = net
    rows[today.isoformat()] = row; write_history(rows)
    send_telegram(msg)

def resolve_mode(now, event, explicit):
    """Decide start / end / skip.
      - explicit start|end always wins (manual / gh runs).
      - repository_dispatch = the ONE dedicated grid cron (fires 00:11, 00:49, 23:11, 23:49 UK).
        Minute-aware so only the two INTENDED snapshots act: 00:11->start, 23:49->end; the other
        two ticks -> skip (idempotency would catch them anyway; this just avoids a wasted count).
      - schedule / other = tolerant hour-only split (survives GitHub's best-effort lateness)."""
    if explicit in ('start', 'end'): return explicit
    h, m = now.hour, now.minute
    if event == 'repository_dispatch':
        if h < 12:  return 'start' if m < 30 else 'skip'
        return 'end' if m >= 30 else 'skip'
    return 'start' if h < 12 else 'end'

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--mode', choices=['start', 'end', 'auto'], default='auto')
    ap.add_argument('--dry', action='store_true')
    ap.add_argument('--force', action='store_true')
    a = ap.parse_args()
    now = datetime.datetime.now(UK); today = now.date()
    explicit = a.mode if a.mode in ('start', 'end') else None
    mode = resolve_mode(now, os.environ.get('GITHUB_EVENT_NAME', ''), explicit)
    print(f"daily_product_count | UK now={now:%Y-%m-%d %H:%M} | mode={mode}{' (dry)' if a.dry else ''}")
    if mode == 'skip':
        print("grid tick outside the intended 00:11 / 23:49 windows — nothing to do."); return
    tok = shopify_token()
    (do_start if mode == 'start' else do_end)(tok, today, a.dry, a.force)

if __name__ == '__main__':
    main()
