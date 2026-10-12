#!/usr/bin/env python3
# daily_product_count.py — DAILY ACTIVE-PRODUCT SNAPSHOT + Telegram, ALL STORES IN ONE MESSAGE
# ---------------------------------------------------------------------------
# Shopify keeps NO history of how many products were active on a past day, so we
# record it ourselves. Twice a day (UK time):
#   • START  (~00:11) : snapshot how many ACTIVE products the day is starting with
#   • END    (~23:49) : snapshot how many it ended with, and diff START->END to show
#                       exactly how many were PUBLISHED vs KILLED during the day.
# Each snapshot is pushed to Telegram as its OWN clearly-dated message (separate from
# the kill messages, so it's easy to spot). A permanent CSV history is also kept.
#
# 12 Oct 2026 (owner: "one for all the stores together in 1 msg ... first the blended one, then each store under each
# other"): LolaLooks + Loreya + SoleVair. Every message = BLENDED block first, then one block per store, same lines as
# before. LolaLooks is computed EXACTLY as before (its state/history files and its ad-spend day are unchanged).
# Loreya / SoleVair: credentials LOR_* / SOL_* (repo Secrets); their Google Ads accounts bill in USD on America/New_York
# days, so their spend is the account's HOURLY cost placed on the UK day (USD native; GBP = USD / the message's rate).
# A store that cannot be read is shown as "not read" - it never blocks the others.
#
# State (kept OUT of the public repo via the _*.json / _*.csv gitignore rules, and
# persisted across GitHub Actions runs via the private Actions cache — same pattern
# as kills_log_auto.csv):
#   _product_count_state.json    today's START snapshot (date, count, id list)          [LolaLooks]
#   _product_count_history.csv   permanent: date, start, end, published, killed, net   [LolaLooks]
#   _product_count_state_<store>.json / _product_count_history_<store>.csv              [loreya, solevair]
#
# Modes (mode is auto-resolved from UK local time if not given):
#   --mode start   record start-of-day  (auto when UK hour < 12)
#   --mode end     record end-of-day    (auto when UK hour >= 12)
#   --mode auto    decide by UK clock (default)
#   --dry          compute + print, but DO NOT send Telegram / write state (local testing)
#   --force        ignore the once-per-day idempotency guard (re-send)
#   --day D        END / SETTLE for UK day D instead of today / yesterday (testing)
# ---------------------------------------------------------------------------
import sys, os, json, csv, argparse, datetime, requests, time
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
from zoneinfo import ZoneInfo
from kill_engine_v4 import shopify_token, norm, SHOP, SHOP_API
from creds import cred

UK = ZoneInfo('Europe/London')
STATE_FILE   = '_product_count_state.json'
HISTORY_FILE = '_product_count_history.csv'
PNL_FILE     = '_eod_pnl.json'          # midnight PNL snapshot + settled-send marker (per day)
HIST_COLS    = ['date', 'start_count', 'end_count', 'published', 'killed', 'net']
# DEDICATED bot for the daily summary / active-product counts, so these land in a SEPARATE chat
# from the auto-kill messages (owner 2026-07-07). Falls back to the shared auto-kill bot if the
# SUMMARY_* vars aren't set yet, so nothing breaks before the 2nd bot is created.
TG_TOKEN = cred('TELEGRAM_SUMMARY_BOT_TOKEN') or cred('TELEGRAM_BOT_TOKEN')
TG_CHAT  = cred('TELEGRAM_SUMMARY_CHAT_ID')  or cred('TELEGRAM_CHAT_ID')
EXTRA_STORES = [('loreya', 'Loreya', 'LOR'), ('solevair', 'SoleVair', 'SOL')]   # (id, label, secret prefix)


# ---------------------------------------------------------------- stores
class Store:
    """One store: Shopify reads + its Google Ads day spend. LolaLooks keeps the original code paths."""
    def __init__(self, sid, label, prefix=None):
        self.sid, self.label, self.prefix = sid, label, prefix
        self.state_file = STATE_FILE if prefix is None else f'_product_count_state_{sid}.json'
        self.history_file = HISTORY_FILE if prefix is None else f'_product_count_history_{sid}.csv'
        if prefix is None:
            self.shop, self.tok = SHOP, shopify_token()
        else:
            self.shop = cred(f'{prefix}_SHOPIFY_STORE').replace('https://', '').rstrip('/')
            if not self.shop:
                raise RuntimeError(f'{prefix}_SHOPIFY_STORE not set')
            r = requests.post(f"https://{self.shop}/admin/oauth/access_token", timeout=40, json={
                'grant_type': 'client_credentials', 'client_id': cred(f'{prefix}_SHOPIFY_CLIENT_ID'),
                'client_secret': cred(f'{prefix}_SHOPIFY_CLIENT_SECRET')})
            self.tok = r.json()['access_token']

    def gql(self, q, v=None):
        for a in range(8):
            j = requests.post(f"https://{self.shop}/admin/api/{SHOP_API}/graphql.json",
                              headers={'X-Shopify-Access-Token': self.tok, 'Content-Type': 'application/json'},
                              json={'query': q, 'variables': v or {}}, timeout=60).json()
            if 'errors' in j and 'THROTTLED' in str(j['errors']).upper():
                time.sleep(2 + 2 * a); continue
            return j
        return j

    def ad_spend(self, day):
        """-> (spend in the ACCOUNT currency, currency). LolaLooks: unchanged (the account's own day = GMT ~ UK).
        Others: every campaign's HOURLY cost whose hour falls inside the UK day (their accounts run on New York time)."""
        if self.prefix is None:
            import google_ads_connect as ga
            r = requests.post(f"{ga.ADS_BASE}/customers/{ga.CUSTOMER_ID}/googleAds:search",
                              headers=ga._headers(ga.get_access_token()),
                              json={'query': f"SELECT metrics.cost_micros FROM customer WHERE segments.date = '{day.isoformat()}'"}, timeout=60)
            if r.status_code != 200:
                raise RuntimeError(f"Google HTTP {r.status_code}")
            return sum(int(row['metrics'].get('costMicros', 0)) / 1e6 for row in r.json().get('results', [])), 'GBP'
        p = self.prefix
        tok = requests.post('https://oauth2.googleapis.com/token', timeout=30, data={
            'grant_type': 'refresh_token', 'client_id': cred(f'{p}_GOOGLE_CLIENT_ID'),
            'client_secret': cred(f'{p}_GOOGLE_CLIENT_SECRET'), 'refresh_token': cred(f'{p}_GOOGLE_REFRESH_TOKEN')}).json()['access_token']
        h = {'Authorization': f'Bearer {tok}', 'Content-Type': 'application/json'}
        if cred(f'{p}_GOOGLE_LOGIN_CUSTOMER_ID'):
            h['login-customer-id'] = cred(f'{p}_GOOGLE_LOGIN_CUSTOMER_ID').replace('-', '')
        url = f"https://googleads.googleapis.com/v22/customers/{cred(f'{p}_GOOGLE_CUSTOMER_ID').replace('-', '')}/googleAds:search"

        def search(q):
            rows, page = [], None
            while True:
                body = {'query': q}
                if page: body['pageToken'] = page
                j = requests.post(url, headers=h, json=body, timeout=60).json()
                if 'error' in j: raise RuntimeError(str(j['error'])[:160])
                rows += j.get('results', []); page = j.get('nextPageToken')
                if not page: return rows
        info = search("SELECT customer.currency_code, customer.time_zone FROM customer")[0]['customer']
        acc = ZoneInfo(info['timeZone'])
        q0, q1 = day - datetime.timedelta(days=1), day + datetime.timedelta(days=1)
        total = 0.0
        for row in search(f"SELECT segments.date, segments.hour, metrics.cost_micros FROM campaign "
                          f"WHERE segments.date BETWEEN '{q0}' AND '{q1}' AND metrics.cost_micros > 0"):
            start = datetime.datetime.combine(datetime.date.fromisoformat(row['segments']['date']),
                                              datetime.time(int(row['segments']['hour'])), acc)
            if start.astimezone(UK).date() == day:
                total += int(row['metrics']['costMicros']) / 1e6
        return total, info['currencyCode']


def open_stores():
    """[(store or None, label, error)] - LolaLooks first. A store that cannot be opened is reported, not fatal."""
    out = []
    for sid, label, prefix in [('lolalooks', 'LolaLooks', None)] + EXTRA_STORES:
        try:
            out.append((Store(sid, label, prefix), label, None))
        except Exception as ex:
            out.append((None, label, f"{type(ex).__name__}: {str(ex)[:80]}"))
            print(f"!! {label}: cannot open ({ex})")
    return out


def fetch_active_ids(st):
    """Every live ACTIVE product id (paginated). len() = the active count."""
    Q = ('query($c:String){products(first:250,after:$c,query:"status:active"){'
         'pageInfo{hasNextPage endCursor} edges{node{legacyResourceId}}}}')
    ids = set(); cur = None
    while True:
        d = st.gql(Q, {'c': cur})['data']['products']
        for e in d['edges']:
            ids.add(norm(e['node']['legacyResourceId']))
        if d['pageInfo']['hasNextPage']: cur = d['pageInfo']['endCursor']
        else: break
    return ids

def published_on(st, day):
    """Active product ids published on the UK day (start-of-day estimate when no START snapshot exists)."""
    a = datetime.datetime.combine(day, datetime.time(0), UK).astimezone(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    b = datetime.datetime.combine(day + datetime.timedelta(days=1), datetime.time(0), UK).astimezone(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    Q = ('query($c:String,$q:String){products(first:250,after:$c,query:$q){'
         'pageInfo{hasNextPage endCursor} edges{node{legacyResourceId}}}}')
    ids = set(); cur = None
    while True:
        d = st.gql(Q, {'c': cur, 'q': f"status:active published_at:>='{a}' published_at:<'{b}'"})['data']['products']
        ids |= {norm(e['node']['legacyResourceId']) for e in d['edges']}
        if d['pageInfo']['hasNextPage']: cur = d['pageInfo']['endCursor']
        else: return ids

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

def load_history(path=HISTORY_FILE):
    rows = {}
    if os.path.exists(path):
        with open(path, newline='', encoding='utf-8') as f:
            for r in csv.DictReader(f): rows[r['date']] = r
    return rows

def write_history(rows, path=HISTORY_FILE):
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=HIST_COLS); w.writeheader()
        for d in sorted(rows): w.writerow(rows[d])

def fmt_date(d): return d.strftime('%a %-d %b %Y') if os.name != 'nt' else d.strftime('%a ') + str(d.day) + d.strftime(' %b %Y')

def usd_rate():
    """Live GBP->USD (open.er-api primary, frankfurter/ECB fallback, sanity-guarded, last-resort const)."""
    for url in ('https://open.er-api.com/v6/latest/GBP', 'https://api.frankfurter.app/latest?from=GBP&to=USD'):
        try:
            r = (requests.get(url, timeout=15).json().get('rates') or {}).get('USD')
            if r and 0.5 < float(r) < 3.0: return float(r)
        except Exception: pass
    return 1.27

def todays_money(st, today, rate):
    """Today's Shopify NET revenue (GBP, PNL basis) + order & item counts, and Google ad spend today (GBP + USD)."""
    since = (today - datetime.timedelta(days=1)).isoformat()
    Q = ('query($c:String){orders(first:100,after:$c,query:"created_at:>=%s -status:cancelled"){pageInfo{hasNextPage endCursor} '
         'edges{node{createdAt currentTotalPriceSet{shopMoney{amount}} '
         'transactions{kind status fees{amount{amount}}} '            # actual Shopify Payments fees = true card/txn cost
         'lineItems(first:100){edges{node{quantity}}}}}}}' % since)
    rev = 0.0; orders = 0; items = 0; fees = 0.0; cur = None
    while True:
        d = st.gql(Q, {'c': cur})['data']['orders']
        for e in d['edges']:
            dt = datetime.datetime.fromisoformat(e['node']['createdAt'].replace('Z', '+00:00')).astimezone(UK).date()
            if dt != today: continue
            orders += 1
            rev += float((e['node'].get('currentTotalPriceSet') or {}).get('shopMoney', {}).get('amount') or 0)
            items += sum(int(li['node'].get('quantity', 0) or 0) for li in e['node']['lineItems']['edges'])
            for t in (e['node'].get('transactions') or []):        # sum card fees on the day's successful sales (GROSS; refunds don't refund the fee)
                if t.get('kind') in ('SALE', 'CAPTURE') and t.get('status') == 'SUCCESS':
                    for f in (t.get('fees') or []):
                        fees += float((f.get('amount') or {}).get('amount') or 0)
        if not d['pageInfo']['hasNextPage']: break
        cur = d['pageInfo']['endCursor']
    cost_g = cost_u = None                                          # Google ad spend today; graceful if unavailable
    try:
        c, ccy = st.ad_spend(today)
        if ccy == 'USD': cost_u, cost_g = c, c / rate
        else: cost_g, cost_u = c, c * rate
    except Exception as ex:
        print(f"({st.label} ad spend unavailable: {str(ex)[:80]})")
    return dict(rev_gbp=rev, orders=orders, items=items, cost_gbp=cost_g, cost_usd=cost_u, fees_gbp=fees)

def blend(ms):
    ms = [m for m in ms if m]
    if not ms: return None
    costs = [m for m in ms if m['cost_gbp'] is not None]
    return dict(rev_gbp=sum(m['rev_gbp'] for m in ms), orders=sum(m['orders'] for m in ms), items=sum(m['items'] for m in ms),
                fees_gbp=sum(m['fees_gbp'] or 0 for m in ms),
                cost_gbp=sum(m['cost_gbp'] for m in costs) if costs else None,
                cost_usd=sum(m['cost_usd'] for m in costs) if costs else None)

def money_block(m, rate):
    """USD-first money summary appended under the product stats in the END message."""
    rev_u = m['rev_gbp'] * rate
    cost_g = m['cost_gbp']
    cost_u = m['cost_usd'] if m.get('cost_usd') is not None else ((cost_g * rate) if cost_g is not None else None)
    fee_g = m.get('fees_gbp'); fee_u = (fee_g * rate) if fee_g is not None else None   # real Shopify Payments card fees
    roas = (m['rev_gbp'] / cost_g) if (cost_g and cost_g > 0) else None
    cpa = (cost_u / m['orders']) if (cost_u is not None and m['orders'] > 0) else None   # ad spend / orders = customer-acquisition cost (NOT the card fee)
    L = [f"Sales revenue  ${rev_u:,.2f}  (£{m['rev_gbp']:,.2f})",
         "Ad spend       " + (f"${cost_u:,.2f}  (£{cost_g:,.2f})" if cost_u is not None else "n/a"),
         "Card fees      " + (f"${fee_u:,.2f}  (£{fee_g:,.2f})" if fee_u is not None else "-"),
         "ROAS           " + (f"{roas:.2f}" if roas is not None else "-"),
         "CPA            " + (f"${cpa:,.2f}" if cpa is not None else "-"),
         f"Orders         {m['orders']}",
         f"Items ordered  {m['items']}"]
    return f"\U0001F4B0 <b>DAY PNL</b>  (USD @ {rate:.3f})\n<pre>" + "\n".join(L) + "</pre>"

def product_lines(start_n, end_n, published, killed, note=''):
    if start_n is None:
        return f"Active at end: <b>{end_n}</b>\n<i>(start-of-day snapshot missing — no diff this day)</i>"
    net = end_n - start_n
    net_line = (f"\U0001F7E2 +{net}" if net > 0 else (f"\U0001F53B −{abs(net)}" if net < 0 else "➖ 0"))
    return (f"Started:  <b>{start_n}</b>\n"
            f"Ended:    <b>{end_n}</b>\n"
            f"Net:      {net_line}\n"
            f"   ➕ published: {published}\n"
            f"   \U0001F53B killed:    {killed}" + (f"\n<i>{note}</i>" if note else ""))

def do_start(stores, today, dry, force):
    lola = stores[0][0]
    if not force and lola is not None and os.path.exists(lola.state_file):
        try: st0 = json.load(open(lola.state_file, encoding='utf-8'))
        except Exception: st0 = {}
        if st0.get('start_date') == today.isoformat():
            print(f"START already recorded for {today} (count={st0.get('start_count')}). Skipping (idempotent)."); return
    counts = []
    for st, label, err in stores:
        if st is None:
            counts.append((label, None)); continue
        try:
            ids = fetch_active_ids(st); n = len(ids)
        except Exception as ex:
            print(f"!! {label}: START count failed ({ex})"); counts.append((label, None)); continue
        counts.append((label, n))
        if dry: continue
        json.dump({'start_date': today.isoformat(), 'start_count': n, 'start_ids': sorted(ids)},
                  open(st.state_file, 'w', encoding='utf-8'))
        rows = load_history(st.history_file)
        row = rows.get(today.isoformat(), {c: '' for c in HIST_COLS}); row['date'] = today.isoformat()
        row['start_count'] = n
        rows[today.isoformat()] = row; write_history(rows, st.history_file)
    total = sum(n for _, n in counts if n is not None)
    msg = ("\U0001F4CA <b>PRODUCTS — START OF DAY</b>\n"
           f"\U0001F5D3 {fmt_date(today)}\n"
           "━━━━━━━━━━━\n"
           f"\U0001F7E2 Active at start: <b>{total}</b>  (all stores)\n"
           + "\n".join(f"   {label}: <b>{n}</b>" if n is not None else f"   {label}: not read" for label, n in counts))
    print(f"[START {today}] {counts}"); print(msg.replace('<b>','').replace('</b>',''))
    if dry: print("(dry-run: not sending / not writing state)"); return
    send_telegram(msg)

def do_end(stores, today, dry, force):
    lola = stores[0][0]
    existing = load_history(lola.history_file).get(today.isoformat()) if lola is not None else None
    if not force and existing and str(existing.get('end_count') or '') != '':
        print(f"END already recorded for {today} (end={existing['end_count']}). Skipping (idempotent)."); return
    rate = usd_rate()
    blocks, ms, snap = [], [], {}
    sums = dict(start=0, end=0, pub=0, kill=0, ok=True, all_end=0, missing=0)   # start/end/pub/kill: stores WITH a start only
    for st, label, err in stores:
        if st is None:
            blocks.append(f"\U0001F3EC <b>{label}</b>\n⚠️ not read ({err})"); sums['ok'] = False; continue
        try:
            ids_now = fetch_active_ids(st); end_n = len(ids_now)
            start_n = start_ids = None; note = ''
            if os.path.exists(st.state_file):
                try:
                    s0 = json.load(open(st.state_file, encoding='utf-8'))
                    if s0.get('start_date') == today.isoformat():
                        start_n = s0.get('start_count'); start_ids = set(s0.get('start_ids') or [])
                except Exception: pass
            if start_ids is None and st.prefix is not None:          # a store's first day: estimate the start
                start_ids = ids_now - published_on(st, today); start_n = len(start_ids)
                note = 'start estimated (first snapshot day) — kills not counted'; sums['ok'] = False
            sums['all_end'] += end_n
            if start_ids is not None:
                published = len(ids_now - start_ids); killed = len(start_ids - ids_now)
                sums['start'] += start_n; sums['end'] += end_n; sums['pub'] += published; sums['kill'] += killed
            else:
                published = killed = ''; sums['ok'] = False; sums['missing'] += 1
            m = None
            try:
                m = todays_money(st, today, rate); ms.append(m)
            except Exception as ex:
                print(f"[END {today}] {label} money block FAILED: {str(ex)[:140]}")
            blocks.append(f"\U0001F3EC <b>{label}</b>\n" + product_lines(start_n, end_n, published, killed, note)
                          + ("\n" + money_block(m, rate) if m else "\n⚠️ money not read"))
            print(f"[END {today}] {label} start={start_n} end={end_n} published={published} killed={killed}"
                  + (f" | rev£{m['rev_gbp']:.2f} spend£{(m['cost_gbp'] or 0):.2f} orders={m['orders']} items={m['items']}" if m else ""))
            if not dry:
                rows = load_history(st.history_file)
                row = rows.get(today.isoformat(), {c: '' for c in HIST_COLS}); row['date'] = today.isoformat()
                if start_n is not None and (row.get('start_count') in ('', None)): row['start_count'] = start_n
                row['end_count'] = end_n; row['published'] = published; row['killed'] = killed
                row['net'] = (end_n - start_n) if start_n is not None else ''
                rows[today.isoformat()] = row; write_history(rows, st.history_file)
            if m is not None:
                snap[st.sid] = {'cost_gbp': m['cost_gbp'], 'rev_gbp': m['rev_gbp'], 'orders': m['orders']}
        except Exception as ex:
            blocks.append(f"\U0001F3EC <b>{label}</b>\n⚠️ not read ({type(ex).__name__})"); sums['ok'] = False
            print(f"!! {label}: END failed ({ex})")
    b = blend(ms)
    if sums['missing'] or not sums['start']:          # a store without a start count: no blended diff
        prod = (f"Active at end: <b>{sums['all_end']}</b>  (all stores)\n"
                f"<i>(a store's start-of-day count is missing — diff per store below)</i>")
    else:
        prod = product_lines(sums['start'], sums['end'], sums['pub'], sums['kill'],
                             '' if sums['ok'] else 'includes an estimated store — see below')
    blended = "\U0001F310 <b>BLENDED — ALL STORES</b>\n" + prod + ("\n" + money_block(b, rate) if b else "")
    msg = ("\U0001F4CA <b>PRODUCTS — END OF DAY</b>\n"
           f"\U0001F5D3 {fmt_date(today)}\n"
           "━━━━━━━━━━━\n" + blended + "\n━━━━━━━━━━━\n" + "\n━━━━━━━━━━━\n".join(blocks))
    print(msg.replace('<b>','').replace('</b>','').replace('<i>','').replace('</i>','').replace('<pre>','').replace('</pre>',''))
    if dry: print("(dry-run: not sending / not writing history)"); return
    if b is not None:                                # midnight PNL snapshot for the 3h settled re-pull
        json.dump({'date': today.isoformat(), 'cost_gbp': b['cost_gbp'], 'rev_gbp': b['rev_gbp'],
                   'orders': b['orders'], 'stores': snap}, open(PNL_FILE, 'w', encoding='utf-8'))
    send_telegram(msg)


def do_settle(stores, day, dry, force):
    """~3h after midnight: re-pull the CLOSED day's money (Google restates overnight ad spend for
    a few hours after day close) and send a clearly-bannered SETTLED PNL. Idempotent per day."""
    snap = {}
    if os.path.exists(PNL_FILE):
        try: snap = json.load(open(PNL_FILE, encoding='utf-8'))
        except Exception: snap = {}
    if not force and snap.get('settled_date') == day.isoformat():
        print(f"SETTLED already sent for {day}. Skipping (idempotent)."); return
    rate = usd_rate()
    ms, blocks = [], []
    for st, label, err in stores:
        if st is None:
            blocks.append(f"\U0001F3EC <b>{label}</b>\n⚠️ not read ({err})"); continue
        try:
            m = todays_money(st, day, rate); ms.append(m)
            blocks.append(f"\U0001F3EC <b>{label}</b>\n" + money_block(m, rate))
            print(f"[SETTLE {day}] {label} rev£{m['rev_gbp']:.2f} spend£{(m['cost_gbp'] or 0):.2f} orders={m['orders']}")
        except Exception as ex:
            blocks.append(f"\U0001F3EC <b>{label}</b>\n⚠️ not read ({type(ex).__name__})")
            print(f"[SETTLE {day}] {label} money re-pull FAILED: {str(ex)[:140]}")
    b = blend(ms)
    if b is None:
        print(f"[SETTLE {day}] no store readable - nothing sent"); return
    msg = ("⏱ <b>DAY PNL — SETTLED</b>  <i>(3h re-pull)</i>\n"
           f"\U0001F5D3 {fmt_date(day)}\n"
           "<i>Google restates overnight ad spend after day close — these are the final figures; "
           "trust these over the midnight PNL.</i>\n━━━━━━━━━━━\n"
           "\U0001F310 <b>BLENDED — ALL STORES</b>\n" + money_block(b, rate))
    # delta vs the midnight snapshot (blended), when we have it for the same day
    if snap.get('date') == day.isoformat() and snap.get('cost_gbp') is not None and b['cost_gbp'] is not None:
        c0, c1 = float(snap['cost_gbp']), float(b['cost_gbp'])
        r0 = (float(snap['rev_gbp']) / c0) if c0 > 0 else None
        r1 = (b['rev_gbp'] / c1) if c1 > 0 else None
        if abs(c1 - c0) >= 0.005:
            msg += (f"\nΔ vs midnight: ad spend £{c0:,.2f} → £{c1:,.2f} ({'+' if c1 >= c0 else '−'}£{abs(c1-c0):,.2f})"
                    + (f", ROAS {r0:.2f} → {r1:.2f}" if r0 and r1 else ""))
        else:
            msg += "\nΔ vs midnight: no change — midnight figures were already final."
    msg += "\n━━━━━━━━━━━\n" + "\n━━━━━━━━━━━\n".join(blocks)
    print(msg.replace('<b>','').replace('</b>','').replace('<i>','').replace('</i>','').replace('<pre>','').replace('</pre>',''))
    if dry: print("(dry-run: not sending / not writing state)"); return
    snap['settled_date'] = day.isoformat()
    json.dump(snap, open(PNL_FILE, 'w', encoding='utf-8'))
    send_telegram(msg)

def do_test():
    """Owner 12 Oct 2026: 'do a test on Telegram now, fake numbers' - the END OF DAY layout with made-up figures, clearly
    labelled TEST. Reads and writes NOTHING (no store, no state, no history)."""
    rate = 1.323
    fake = [('LolaLooks', (6435, 6421, 49, 63), dict(rev_gbp=2668.40, orders=70, items=74, fees_gbp=153.72, cost_gbp=1299.80)),
            ('Loreya', (242, 1236, 994, 0), dict(rev_gbp=212.85, orders=6, items=7, fees_gbp=12.40, cost_gbp=74.10)),
            ('SoleVair', (227, 1215, 992, 4), dict(rev_gbp=158.60, orders=5, items=5, fees_gbp=9.35, cost_gbp=71.45))]
    ms, blocks, s = [], [], [0, 0, 0, 0]
    for label, (a, b_, p, k), m in fake:
        m = dict(m, cost_usd=m['cost_gbp'] * rate); ms.append(m)
        for i, v in enumerate((a, b_, p, k)): s[i] += v
        blocks.append(f"\U0001F3EC <b>{label}</b>\n" + product_lines(a, b_, p, k) + "\n" + money_block(m, rate))
    msg = ("\U0001F9EA <b>TEST — FAKE NUMBERS</b> (layout check only)\n"
           "\U0001F4CA <b>PRODUCTS — END OF DAY</b>\n"
           f"\U0001F5D3 {fmt_date(datetime.datetime.now(UK).date())}\n"
           "━━━━━━━━━━━\n\U0001F310 <b>BLENDED — ALL STORES</b>\n" + product_lines(*s) + "\n" + money_block(blend(ms), rate)
           + "\n━━━━━━━━━━━\n" + "\n━━━━━━━━━━━\n".join(blocks))
    print(msg)
    send_telegram(msg)

def resolve_mode(now, event, explicit):
    """Decide rollover / settle / start / end / skip.
      - explicit start|end|rollover|settle always wins (manual / gh runs).
      - 'rollover' fires just after midnight (the grid's 00:11/00:49 UK ticks): it closes out
        YESTERDAY (END + midnight PNL) AND opens TODAY (START) in one run.
      - 'settle' fires ~3h later (the 03:11 UK tick): Google restates overnight ad spend after
        day close, so we re-pull yesterday's money and send a bannered SETTLED PNL. The settle
        run also re-runs rollover first (idempotent), so it doubles as a free retry if both
        midnight ticks were dropped.
      - all afternoon/evening ticks (23:11, 23:49 UK) -> skip."""
    if explicit in ('start', 'end', 'rollover', 'settle'): return explicit
    if now.hour < 2:  return 'rollover'
    if now.hour < 12: return 'settle'
    return 'skip'

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--mode', choices=['start', 'end', 'rollover', 'settle', 'auto', 'test'], default='auto')
    ap.add_argument('--dry', action='store_true')
    ap.add_argument('--force', action='store_true')
    ap.add_argument('--day', help='END / SETTLE for this UK day (YYYY-MM-DD) instead of today / yesterday (testing)')
    a = ap.parse_args()
    if a.mode == 'test':                            # fake-number layout check (no store read, no state)
        do_test(); return
    now = datetime.datetime.now(UK); today = now.date()
    explicit = a.mode if a.mode in ('start', 'end', 'rollover', 'settle') else None
    mode = resolve_mode(now, os.environ.get('GITHUB_EVENT_NAME', ''), explicit)
    print(f"daily_product_count | UK now={now:%Y-%m-%d %H:%M} | mode={mode}{' (dry)' if a.dry else ''}")
    if mode == 'skip':
        print("grid tick outside the rollover/settle windows — nothing to do."); return
    stores = open_stores()
    if stores[0][0] is None:
        raise SystemExit("LolaLooks store could not be opened - nothing sent")
    yest = today - datetime.timedelta(days=1)
    if a.day:                                       # testing: close THIS day (END/SETTLE), START the next
        yest = datetime.date.fromisoformat(a.day); today = yest + datetime.timedelta(days=1)
    if mode == 'rollover':                          # just after midnight: close yesterday, open today
        do_end(stores, yest, a.dry, a.force)        # yesterday's END + midnight PNL (reads yesterday's START)
        do_start(stores, today, a.dry, a.force)     # today's START (overwrites state)
    elif mode == 'settle':                          # ~03:11 UK: settled PNL re-send (+ free rollover retry)
        do_end(stores, yest, a.dry, False)          # idempotent — only acts if midnight ticks were dropped
        do_start(stores, today, a.dry, False)
        do_settle(stores, yest, a.dry, a.force)
    else:
        (do_start if mode == 'start' else do_end)(stores, yest if a.day else today, a.dry, a.force)

if __name__ == '__main__':
    main()
