# newstore/data.py - READ-ONLY data for the new-store rules: Shopify (gwvmrw-vm) products + orders, Google Ads 778
# spend, ECB USD->GBP rates. Pure helpers (fill_rates, align_to_uk, sales_by_product) are unit-tested.
import collections
import datetime
import json
import os
import time
from zoneinfo import ZoneInfo

import requests

from . import config as C
from .rules import Product

UK = ZoneInfo(C.STORE_TIMEZONE)
NY = ZoneInfo(C.ACCOUNT_TIMEZONE)
PT = ZoneInfo('America/Los_Angeles')       # Google's quota day
ACCOUNT_SPEND_FROM = datetime.date.fromisoformat(C.ACCOUNT_SPEND_FROM)   # first possible spend day of the counted campaigns
ADS_VERSION = os.environ.get('NS_ADS_API_VERSION', 'v24')


# ======================================================================= pure helpers
def fill_rates(raw, d0, d1):
    """raw: {date: GBP per USD} on ECB business days. Returns a rate for EVERY day d0..d1, each day taking the last
    published rate on or before it (weekends/holidays = the previous business day, as the daily P&L does)."""
    out, last = {}, None
    for d in sorted(raw):
        if d < d0:
            last = raw[d]
    day = d0
    while day <= d1:
        last = raw.get(day, last)
        if last is None:
            raise RuntimeError(f'no USD->GBP rate on or before {day}')
        out[day] = last
        day += datetime.timedelta(days=1)
    return out


def uk_split(ny_day, hourly=None):
    """How one New York account day spreads over UK days: {uk_date: share}. With the campaign's hourly cost for that
    day (hour -> cost) the split follows where the money was actually spent; without it, by the hours in each UK day."""
    shares = collections.defaultdict(float)
    tot = sum((hourly or {}).values())
    for h in range(24):
        uk_day = datetime.datetime.combine(ny_day, datetime.time(h), NY).astimezone(UK).date()
        shares[uk_day] += ((hourly or {}).get(h, 0.0) / tot) if tot > 0 else 1.0 / 24
    return dict(shares)


def align_to_uk(rows, hourly, fx):
    """rows: [(pid, campaign_id, ny_date, cost_usd, clicks)]; hourly: {(campaign_id, ny_date): {hour: cost}};
    fx: {ny_date: GBP per USD}. Returns (spend_gbp, clicks) as {pid: {uk_date: value}}.
    USD is converted with the rate of the ACCOUNT day the money was spent, then moved onto UK days."""
    spend = collections.defaultdict(lambda: collections.defaultdict(float))
    clicks = collections.defaultdict(lambda: collections.defaultdict(float))
    for pid, cid, day, usd, ck in rows:
        rate = fx[day]
        for uk_day, share in uk_split(day, hourly.get((cid, day))).items():
            spend[pid][uk_day] += usd * rate * share
            clicks[pid][uk_day] += ck * share
    return ({k: dict(v) for k, v in spend.items()}, {k: dict(v) for k, v in clicks.items()})


def pid_of_item(item_id):
    """shopify_ZZ_<product>_<variant> (any case) -> product id; None for other ids."""
    parts = str(item_id).split('_')
    return parts[2] if len(parts) >= 4 and parts[0].lower() == 'shopify' and parts[2].isdigit() else None


def is_excluded_order(o):
    tags = {str(t).lower() for t in (o.get('tags') or [])}
    return bool(o.get('test')) or bool(o.get('cancelledAt')) or bool(tags & {'test-order', 'test_order', 'test'})


def credit_reason(o):
    """Why this order is a store-credit order that must not count (config: CREDIT_* / MIN_REVENUE_SHARE), else None.
    Orders without list-price data (older dicts) are never filtered here."""
    lst, sub = o.get('list_total'), o.get('subtotal')
    if not lst or lst <= 0 or sub is None:
        return None
    share = sub / lst                                             # product revenue after discounts / list price
    codes = [str(c).upper() for c in (o.get('discount_codes') or [])]
    titles = [str(t).lower() for t in (o.get('discount_titles') or [])]
    credit = any(c.startswith(C.CREDIT_CODE_PREFIX) for c in codes) or any(t.startswith(C.CREDIT_TITLE_PREFIX) for t in titles)
    if credit and (1 - share) >= C.CREDIT_MAIN_SHARE:
        return f'store credit covered {1 - share:.0%} of list price'
    if share < C.MIN_REVENUE_SHARE:
        return f'product revenue {share:.0%} of list price (< {C.MIN_REVENUE_SHARE:.0%})'
    return None


def is_old_store_copy(o):
    return any(str(t).lower().startswith(C.OLD_ORDER_TAG) for t in (o.get('tags') or []))


def _ts(s):
    return datetime.datetime.fromisoformat(str(s).replace('Z', '+00:00'))


def sales_by_product(orders, cutover=None):
    """{pid: [UK date of processedAt, one per order]} - NEW-SETUP sales only (owner 2026-11-15 19:16): processed at or
    after `cutover` (default config.CUTOVER) and never a copy of an old-store order (tag old-order / old-order-#...).
    processedAt, not createdAt. Cancelled and test orders never count. A line that brought in GBP 0 (exchange /
    replacement / 100% discount) is not a sale. Store-credit orders (credit_reason) never count.
    cutover=False switches the new-setup filter off (tests only)."""
    cut = None if cutover is False else _ts(cutover or C.CUTOVER)
    out = collections.defaultdict(set)
    for o in orders:
        if is_excluded_order(o):
            continue
        if cut is not None and (is_old_store_copy(o) or _ts(o['processedAt']) < cut):
            continue
        if credit_reason(o):                                      # store-credit orders are not sales (owner 2026-11-16)
            continue
        day = _ts(o['processedAt']).astimezone(UK).date()
        lines = [li for li in o['lineItems'] if li.get('pid')]
        line_sum = sum(li['amount'] for li in lines)
        sub = o.get('subtotal')
        factor = (sub / line_sum) if (sub is not None and line_sum > 0) else 1.0
        for li in lines:
            if li['amount'] * factor > 0.009:
                out[li['pid']].add((day, o['id']))
    return {pid: sorted(d for d, _ in v) for pid, v in out.items()}


# ======================================================================= Google Ads (778)
class AdsBudgetError(RuntimeError):
    pass


class Ads:
    """Counts every Google Ads API operation (search / searchStream request = 1 op) per Pacific day."""
    CALLS_FILE = os.environ.get('NS_CALLS_FILE', 'ns_api_calls.json')

    def __init__(self, cred):
        self.cred = cred
        self.run_ops = 0
        r = requests.post('https://oauth2.googleapis.com/token', data={
            'client_id': cred('GOOGLE_CLIENT_ID'), 'client_secret': cred('GOOGLE_CLIENT_SECRET'),
            'refresh_token': cred('GOOGLE_REFRESH_TOKEN'), 'grant_type': 'refresh_token'}, timeout=30).json()
        if 'access_token' not in r:
            raise RuntimeError(f"Google OAuth refused: {r.get('error', '?')}")
        self._h = {'Authorization': 'Bearer ' + r['access_token'], 'developer-token': cred('GOOGLE_DEVELOPER_TOKEN'),
                   'Content-Type': 'application/json'}
        login = cred('NS_GOOGLE_LOGIN_CUSTOMER_ID')
        if login:
            self._h['login-customer-id'] = login

    def _count(self):
        day = datetime.datetime.now(PT).date().isoformat()
        try:
            n = int(json.load(open(self.CALLS_FILE)).get(day, 0))
        except Exception:
            n = 0
        if n >= C.DAILY_GOOGLE_OPS_BUDGET:
            raise AdsBudgetError(f'GOOGLE_QUOTA_EXHAUSTED: new-store engine budget {n}/{C.DAILY_GOOGLE_OPS_BUDGET} '
                                 f'(Pacific day {day})')
        try:
            json.dump({day: n + 1}, open(self.CALLS_FILE, 'w'))
        except OSError:
            pass
        self.run_ops += 1

    def stream(self, query):
        """ONE searchStream request (= one operation however many rows), retried on 429/5xx/transient 400."""
        self._count()
        url = f'https://googleads.googleapis.com/{ADS_VERSION}/customers/{C.GOOGLE_CUSTOMER_ID}/googleAds:searchStream'
        for a in range(4):
            r = requests.post(url, headers=self._h, json={'query': query}, timeout=300)
            if r.status_code == 200:
                return [x for batch in r.json() for x in batch.get('results', [])]
            if r.status_code in (400, 429, 500, 502, 503, 504) and a < 3:
                time.sleep(5 * (a + 1))
                continue
            raise RuntimeError(f'Google Ads {r.status_code}: {r.text[:300]}')


def check_account(ads):
    c = ads.stream('SELECT customer.currency_code, customer.time_zone FROM customer')[0]['customer']
    if c['currencyCode'] != C.ACCOUNT_CURRENCY or c['timeZone'] != C.ACCOUNT_TIMEZONE:
        raise RuntimeError(f"account {C.GOOGLE_CUSTOMER_ID} is {c['currencyCode']}/{c['timeZone']}, expected "
                           f"{C.ACCOUNT_CURRENCY}/{C.ACCOUNT_TIMEZONE} - stopped (spend would be misread)")
    return c


def fetch_spend(ads, run, fx_raw=None):
    """All spend the rules need in TWO searchStream requests: product x campaign x day, and campaign x day x hour (to
    place each New York day on UK days). Returns (spend_gbp, clicks, info)."""
    ids = ', '.join(C.campaign_ids())
    d0 = ACCOUNT_SPEND_FROM
    d1 = run + datetime.timedelta(days=1)        # the NY day can run past UK midnight - never past tomorrow
    rows = []
    for x in ads.stream(f"SELECT campaign.id, segments.date, segments.product_item_id, metrics.cost_micros, metrics.clicks "
                        f"FROM shopping_performance_view WHERE segments.date BETWEEN '{d0}' AND '{d1}' "
                        f"AND campaign.id IN ({ids}) AND metrics.impressions > 0"):
        pid = pid_of_item(x['segments']['productItemId'])
        if pid:
            rows.append((pid, x['campaign']['id'], datetime.date.fromisoformat(x['segments']['date']),
                         int(x['metrics'].get('costMicros', 0)) / 1e6, int(x['metrics'].get('clicks', 0) or 0)))
    hourly = collections.defaultdict(dict)
    for x in ads.stream(f"SELECT campaign.id, segments.date, segments.hour, metrics.cost_micros FROM campaign "
                        f"WHERE segments.date BETWEEN '{d0}' AND '{d1}' AND campaign.id IN ({ids}) AND metrics.cost_micros > 0"):
        hourly[(x['campaign']['id'], datetime.date.fromisoformat(x['segments']['date']))][int(x['segments']['hour'])] = \
            int(x['metrics']['costMicros']) / 1e6
    if fx_raw is None:
        # store money = account money (Everly: USD/USD) -> 1:1, no ECB call; else ECB USD -> GBP
        fx_raw = {d0: 1.0} if C.STORE_CURRENCY == C.ACCOUNT_CURRENCY else ecb_usd_gbp(d0, d1)
    fx = fill_rates(fx_raw, d0, d1)
    spend, clicks = align_to_uk(rows, hourly, fx)
    return spend, clicks, {'rows': len(rows), 'usd_total': round(sum(r[3] for r in rows), 2),
                           'fx_latest': fx[min(run, d1)], 'fx_days': len(fx)}


def ecb_usd_gbp(d0, d1):
    """ECB USD->GBP by day - the same source and call as the daily P&L (api.frankfurter.dev, base USD). No rate = stop."""
    err = ''
    for a in range(5):
        try:
            r = requests.get(f'https://api.frankfurter.dev/v1/{d0 - datetime.timedelta(days=7)}..{d1}',
                             params={'base': 'USD', 'symbols': 'GBP'}, timeout=30)
            r.raise_for_status()
            raw = {datetime.date.fromisoformat(k): float(v['GBP']) for k, v in r.json()['rates'].items()}
            bad = [v for v in raw.values() if not 0.5 <= v <= 1.0]
            if bad or not raw:
                raise ValueError(f'implausible rates {bad[:3]}')
            return raw
        except Exception as ex:                  # noqa: BLE001
            err = f'{type(ex).__name__}: {str(ex)[:80]}'
            time.sleep(3 * (a + 1))
    raise RuntimeError(f'USD->GBP rates unavailable ({err}) - run stopped, a guessed rate is never used')


# ======================================================================= Shopify (new store)
class Shop:
    def __init__(self, cred):
        r = requests.post(f'https://{C.SHOPIFY_STORE}/admin/oauth/access_token', data={
            'grant_type': 'client_credentials', 'client_id': cred('NS_SHOPIFY_CLIENT_ID'),
            'client_secret': cred('NS_SHOPIFY_CLIENT_SECRET')}, timeout=30).json()
        if 'access_token' not in r:
            raise RuntimeError(f'Shopify token refused for {C.SHOPIFY_STORE}')
        self.tok = r['access_token']

    def gql(self, query, variables=None):
        for a in range(8):
            try:
                r = requests.post(f'https://{C.SHOPIFY_STORE}/admin/api/{C.SHOPIFY_API}/graphql.json',
                                  headers={'X-Shopify-Access-Token': self.tok, 'Content-Type': 'application/json'},
                                  json={'query': query, 'variables': variables or {}}, timeout=90)
                if r.status_code in (429, 500, 502, 503, 504):
                    time.sleep(min(30, 2 * 2 ** a)); continue
                j = r.json()
                if 'THROTTLED' in str(j.get('errors', '')).upper():
                    time.sleep(min(30, 2 * 2 ** a)); continue
                return j
            except requests.RequestException:
                time.sleep(min(30, 2 * 2 ** a))
        raise RuntimeError('Shopify kept failing after 8 tries')

    def read(self, query, variables=None):
        j = self.gql(query, variables)
        if j.get('data') is None:
            raise RuntimeError(f"Shopify read error: {str(j.get('errors'))[:200]}")
        return j['data']


def check_shop(shop):
    """Stop before judging anything if the store is not the one these settings were written for (a missing NS_ variable
    would silently fall back to Lola's values: wrong currency, wrong calendar day)."""
    s = shop.read('{shop{currencyCode ianaTimezone myshopifyDomain}}')['shop']
    if (s['currencyCode'], s['ianaTimezone'], s['myshopifyDomain']) != (C.STORE_CURRENCY, C.STORE_TIMEZONE, C.SHOPIFY_STORE):
        raise RuntimeError(f"store is {s['myshopifyDomain']} {s['currencyCode']}/{s['ianaTimezone']}, settings say "
                           f"{C.SHOPIFY_STORE} {C.STORE_CURRENCY}/{C.STORE_TIMEZONE} - stopped")
    return s


def fetch_products(shop):
    Q = ('query($c:String){products(first:250,after:$c,query:"status:active"){pageInfo{hasNextPage endCursor} '
         'nodes{legacyResourceId handle title status tags priceRangeV2{minVariantPrice{amount}}}}}')
    out, cur = [], None
    while True:
        d = shop.read(Q, {'c': cur})['products']
        for n in d['nodes']:
            out.append(Product(pid=str(n['legacyResourceId']), handle=n['handle'], title=n['title'],
                               price=float(((n.get('priceRangeV2') or {}).get('minVariantPrice') or {}).get('amount') or 0),
                               tags={str(t) for t in n['tags']}, status=n['status']))
        if not d['pageInfo']['hasNextPage']:
            return out
        cur = d['pageInfo']['endCursor']


def fetch_orders(shop, since=None):
    """Orders PROCESSED since `since` (a day before the cutover; sales_by_product applies the exact cutover)."""
    Q = ('query($c:String,$q:String!){orders(first:250,after:$c,query:$q,sortKey:PROCESSED_AT){pageInfo{hasNextPage endCursor} '
         'nodes{id name processedAt createdAt cancelledAt test tags discountCodes subtotalPriceSet{shopMoney{amount}} '
         'discountApplications(first:20){nodes{... on DiscountCodeApplication{code} ... on ManualDiscountApplication{title} '
         '... on AutomaticDiscountApplication{title} ... on ScriptDiscountApplication{title}}} '
         'lineItems(first:100){nodes{product{legacyResourceId} quantity originalUnitPriceSet{shopMoney{amount}} '
         'discountedTotalSet{shopMoney{amount}}}}}}}')
    since = since or C.ORDERS_SINCE
    out, cur = [], None
    while True:
        d = shop.read(Q, {'c': cur, 'q': f'processed_at:>={since}'})['orders']
        for o in d['nodes']:
            sub = (o.get('subtotalPriceSet') or {}).get('shopMoney', {}).get('amount')
            out.append({'id': o['id'], 'name': o['name'], 'processedAt': o['processedAt'], 'createdAt': o['createdAt'],
                        'cancelledAt': o['cancelledAt'], 'test': o['test'], 'tags': o['tags'],
                        'subtotal': float(sub) if sub is not None else None,
                        'discount_codes': o.get('discountCodes') or [],
                        'discount_titles': [a.get('title') or a.get('code') or '' for a in (o.get('discountApplications') or {}).get('nodes', [])],
                        'list_total': sum(float(li['originalUnitPriceSet']['shopMoney']['amount']) * int(li['quantity'] or 0)
                                          for li in o['lineItems']['nodes']),
                        'lineItems': [{'pid': (li.get('product') or {}).get('legacyResourceId') and str(li['product']['legacyResourceId']),
                                       'amount': float(li['discountedTotalSet']['shopMoney']['amount'])}
                                      for li in o['lineItems']['nodes']]})
        if not d['pageInfo']['hasNextPage']:
            return out
        cur = d['pageInfo']['endCursor']
