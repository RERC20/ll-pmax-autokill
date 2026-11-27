# Hourly PnL feed for the "GUK - Store | Profit Sheet" (Nov Scale Sheet tab, bound Apps Script refreshAll).
#
#   GET /api/hourly?from=YYYY-MM-DD&to=YYYY-MM-DD     (Basic auth, same DASH_USER / DASH_PASS as /feeds)
#
# For every UK date + UK hour (Europe/London) in [from, to]:
#   - Shopify, NEW LolaLooks store (SHOPIFY_NEWLOLA_*): sales GBP = order totalPriceSet (after discounts, incl.
#     shipping + VAT) and order count, bucketed by createdAt in UK time. Excluded: test orders, cancelled orders,
#     copied orders tagged "old-order" / "old-order-#...", and any order whose processedAt is more than 36h before
#     createdAt (imported history).
#   - Google Ads 778-370-4709 (USD, America/New_York; GADS_* variables): account total cost per hour (customer level)
#     plus cost / conversion value / conversions per campaign per hour, from segments.date + segments.hour. Each New
#     York hour is converted to its UK date + hour with zoneinfo (DST-correct).
#   - USD->GBP and GBP->USD (shop_sales_usd): ECB daily reference rates (frankfurter; latest rate on or before the UK date,
#     same source as the daily PnL job), fallback
#     open.er-api.com latest; if neither answers, GBP fields are null and fx.source is null.
#   - split (per hour and per day; top-level split_rules): Testing / Winners / blended / other.
#       SPEND by campaign ID only (GADS_TESTING_CAMPAIGN_IDS, GADS_WINNERS_CAMPAIGN_IDS; comma lists; defaults Testing
#       24425197606, Winners 24467845823). Every other campaign (Brand, Last Chance, Warm-up, ...) is "other", listed by
#       id. Campaign names are never matched.
#       SALES per order line: the line's product had >= WINNER_MIN_PRIOR_SALES earlier sales -> winners, else testing.
#       An earlier sale = an order created before this one (createdAt, then order id) in the new store that the new-store
#       auto-kill (RERC20/ll-pmax-autokill newstore/data.py sales_by_product) also counts as a sale - that is what moves
#       a product to the Winners campaign: processedAt >= HISTORY_CUTOVER (HOURLY_HISTORY_CUTOVER, default = the
#       auto-kill's NS_CUTOVER 2026-11-14T13:00Z), not test / cancelled / test-tagged, not an old-store copy (any tag
#       starting "old-order"), not a store-credit order (CREDIT- code or "Store credit" title covering >= 50% of the
#       list price, or revenue < 20% of list price), and the product's line brought in more than GBP 0.009.
#       Each order's totalPriceSet (pennies) is split over its lines in proportion to line value = originalTotalSet -
#       discountAllocations (so shipping/tax follow the same proportion), largest-remainder rounding: testing + winners
#       sales_gbp == shop_sales_gbp exactly for every hour and day. An order counts in testing / winners if it has >= 1
#       line there (orders_mixed = both).
#
#   GET /api/hourly?from=..&to=..&store=everly     Everly Looks US instead (STORES below; no store / store=lola = the
#   LolaLooks UK feed above, unchanged). Same JSON shape, with these differences:
#   - a first top-level field "store": "everly" (the Lola feed has no store field, so its output is byte-for-byte the same).
#   - Shopify z6hjj1-ae (SHOPIFY_EVERLY_*, USD); Google Ads 123-098-9417 (GADS_EVERLY_CUSTOMER_ID; same OAuth / dev
#     token as Lola; GADS_EVERLY_LOGIN_CUSTOMER_ID if set, else GADS_LOGIN_CUSTOMER_ID).
#   - Dates and hours are New York time (America/New_York; tz in the response). Ads hours are already New York.
#   - No currency conversion: the shop and the ads account are both USD, so usd_gbp = gbp_usd = 1.0 and fx sources are
#     "same currency (USD)". Every *_gbp field then holds the same USD amount as its *_usd twin (names kept so the same
#     Apps Script can read both feeds).
#   - split: Testing GADS_EVERLY_TESTING_CAMPAIGN_IDS (default 24512817508 PMax | Testing | US), Winners
#     GADS_EVERLY_WINNERS_CAMPAIGN_IDS (default none), history cutover HOURLY_EVERLY_HISTORY_CUTOVER (default the Everly
#     auto-kill NS_CUTOVER 2026-11-19T09:45Z). Same sales rules (old-order copies, store credit, ...).
# Results are cached for 5 minutes per (store, from, to, split config); the sales history for 5 minutes per store.
import bisect, datetime as dt, json, os, threading, time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo

import requests

UK = ZoneInfo("Europe/London")
ADS_V = os.environ.get("GADS_API_VERSION", "v24")
CACHE_S = 300
MAX_DAYS = 62
_cache, _lock = {}, threading.Lock()
_S = requests.Session()

# ---- campaign split (owner 2026-11-17) ----
WINNER_MIN_PRIOR_SALES = 1                       # >= this many earlier sales of the product -> the line is a Winners sale
DEFAULT_TESTING_IDS = "24425197606"              # PMax | Testing | UK
DEFAULT_WINNERS_IDS = "24467845823"              # PMax | Winners | UK
DEFAULT_HISTORY_CUTOVER = "2026-11-14T13:00:00+00:00"   # = ll-pmax-autokill newstore config CUTOVER (NS_CUTOVER)
CREDIT_CODE_PREFIX, CREDIT_TITLE_PREFIX = "CREDIT-", "store credit"   # = autokill config (store-credit orders)
CREDIT_MAIN_SHARE, MIN_REVENUE_SHARE = 0.5, 0.2
TEST_TAGS = {"test-order", "test_order", "test"}

# ---- per-store settings. "lola" = the constants / env names above, exactly as before the store parameter existed.
NY = ZoneInfo("America/New_York")
STORES = {
    "lola": {"key": "lola", "tz": UK, "shop_key": "newlola", "shop_env": "NEWLOLA",
             "ads_cid_env": "GADS_CUSTOMER_ID", "ads_cid_default": "7783704709", "ads_login_env": None,
             "testing_env": "GADS_TESTING_CAMPAIGN_IDS", "testing_default": DEFAULT_TESTING_IDS,
             "winners_env": "GADS_WINNERS_CAMPAIGN_IDS", "winners_default": DEFAULT_WINNERS_IDS,
             "cutover_env": "HOURLY_HISTORY_CUTOVER", "cutover_default": DEFAULT_HISTORY_CUTOVER,
             "currency": "GBP", "fx": True,
             "other_example": "Brand 24419559596, Last Chance 24467846096"},
    "everly": {"key": "everly", "tz": NY, "shop_key": "everly", "shop_env": "EVERLY",
               "ads_cid_env": "GADS_EVERLY_CUSTOMER_ID", "ads_cid_default": "1230989417",
               "ads_login_env": "GADS_EVERLY_LOGIN_CUSTOMER_ID",
               "testing_env": "GADS_EVERLY_TESTING_CAMPAIGN_IDS", "testing_default": "24512817508",   # PMax | Testing | US
               "winners_env": "GADS_EVERLY_WINNERS_CAMPAIGN_IDS", "winners_default": "",            # none yet
               "cutover_env": "HOURLY_EVERLY_HISTORY_CUTOVER",
               "cutover_default": "2026-11-19T09:45:00+00:00",     # = ll-pmax-autokill Everly NS_CUTOVER
               "currency": "USD", "fx": False,
               "other_example": "Search | Brand | US 24513422296"},
}
LOLA = STORES["lola"]
SAME_CCY = "same currency (USD)"


def _env(k):
    return (os.environ.get(k) or "").strip()


def _days(a, b):
    d = a
    while d <= b:
        yield d
        d += dt.timedelta(days=1)


def _uk_start(d, tz=UK):
    """Start of local date d in tz (UK for Lola; New York for Everly)."""
    return dt.datetime(d.year, d.month, d.day, tzinfo=tz)


def _iso_utc(x):
    return x.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ts(s):
    return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def _ids(k, default):
    raw = os.environ.get(k)
    raw = default if raw is None or not raw.strip() else raw
    return [x.strip().replace("-", "") for x in raw.split(",") if x.strip()]


def split_cfg(store=None):
    """Read at call time (env on Railway; tests set os.environ). An id in both lists counts as Testing."""
    st = store or LOLA
    t = _ids(st["testing_env"], st["testing_default"])
    w = [x for x in _ids(st["winners_env"], st["winners_default"]) if x not in t]
    cut = os.environ.get(st["cutover_env"])
    cut = st["cutover_default"] if cut is None else cut.strip()       # "" = all time (no cutover)
    return {"testing": tuple(t), "winners": tuple(w), "cutover": cut, "min_prior": WINNER_MIN_PRIOR_SALES}


def pennies(x):
    return int((Decimal(str(x)) * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def alloc(total, weights):
    """Split integer `total` over `weights` in proportion (largest remainder, ties to the earlier index). The parts are
    integers that sum to `total` exactly. Non-positive weights get 0; all-zero weights -> equal parts."""
    w = [max(0, x) for x in weights]
    if not w:
        return []
    s = sum(w)
    if s <= 0:
        w, s = [1] * len(w), len(w)
    exact = [Decimal(total) * Decimal(x) / Decimal(s) for x in w]
    base = [int(e // 1) for e in exact]
    rest = total - sum(base)
    order = sorted(range(len(w)), key=lambda i: (-(exact[i] - base[i]), i))
    for i in order[:rest]:
        base[i] += 1
    return base


# ---------------------------------------------------------------- Google Ads
class Ads:
    def __init__(self, store=None):
        st = store or LOLA
        self.tok, self.exp, self.login_env = None, 0, st["ads_login_env"]
        self.default_cid = st["ads_cid_default"]
        self.cid = _env(st["ads_cid_env"]).replace("-", "") or self.default_cid

    @property
    def label(self):
        c = self.cid
        return f"{c[:3]}-{c[3:6]}-{c[6:]}" if c == self.default_cid else c

    def login(self):
        """login-customer-id: the store's own variable when it is set (even empty), else GADS_LOGIN_CUSTOMER_ID."""
        if self.login_env and os.environ.get(self.login_env) is not None:
            return _env(self.login_env).replace("-", "")
        return _env("GADS_LOGIN_CUSTOMER_ID").replace("-", "")

    @property
    def ok(self):
        return all(_env(k) for k in ("GADS_DEVELOPER_TOKEN", "GADS_CLIENT_ID", "GADS_CLIENT_SECRET", "GADS_REFRESH_TOKEN"))

    def token(self):
        if self.tok and time.time() < self.exp - 120:
            return self.tok
        r = _S.post("https://oauth2.googleapis.com/token", timeout=30, data=dict(
            client_id=_env("GADS_CLIENT_ID"), client_secret=_env("GADS_CLIENT_SECRET"),
            refresh_token=_env("GADS_REFRESH_TOKEN"), grant_type="refresh_token")).json()
        if "access_token" not in r:
            raise RuntimeError("Google OAuth refused: " + str(r.get("error", "?")))
        self.tok, self.exp = r["access_token"], time.time() + int(r.get("expires_in", 3600))
        return self.tok

    def search(self, q):
        h = {"Authorization": "Bearer " + self.token(), "developer-token": _env("GADS_DEVELOPER_TOKEN")}
        if self.login():
            h["login-customer-id"] = self.login()
        out, page = [], None
        for _ in range(50):
            body = {"query": q}
            if page:
                body["pageToken"] = page
            r = _S.post(f"https://googleads.googleapis.com/{ADS_V}/customers/{self.cid}/googleAds:search",
                        headers=h, json=body, timeout=90)
            j = r.json()
            if r.status_code != 200:
                msg = j.get("error", {}).get("message", "") if isinstance(j, dict) else ""
                raise RuntimeError(f"Google Ads HTTP {r.status_code}: {msg[:200]}")
            out += j.get("results", [])
            page = j.get("nextPageToken")
            if not page:
                break
        return out


ADS = Ads()
ADS_BY_STORE = {"lola": ADS, "everly": Ads(STORES["everly"])}


def ny_to_uk(date_str, hour, tz, to=UK):
    """One account-time-zone (New York) date + hour -> (UK date str, UK hour). Hour 0 may be omitted by the API.
    On the New York autumn change day hour 1 happens twice; Google reports it once and it is placed at its first
    (EDT) occurrence. to = the report time zone (UK for Lola; New York for Everly = identity)."""
    y, m, d = map(int, date_str.split("-"))
    t = dt.datetime(y, m, d, int(hour or 0), tzinfo=tz).astimezone(to)
    return t.date().isoformat(), t.hour


def ads_bucket(acct_rows, camp_rows, tz, lo, hi, to=UK):
    """Pure: Google Ads rows -> {(uk_date_str, uk_hour): {"acct": usd, "acct_micros": int, "camps": {cid: {...}}}}."""
    by = {}

    def slot(seg):
        k = ny_to_uk(seg["date"], seg.get("hour"), tz, to)
        return k if lo <= k[0] <= hi else None

    for r in acct_rows:
        k = slot(r["segments"])
        if k:
            e = by.setdefault(k, {"acct": 0.0, "acct_micros": 0, "camps": {}})
            mic = int(r.get("metrics", {}).get("costMicros", 0))
            e["acct"] += mic / 1e6; e["acct_micros"] += mic
    for r in camp_rows:
        k = slot(r["segments"])
        if not k:
            continue
        c, m = r.get("campaign", {}), r.get("metrics", {})
        e = by.setdefault(k, {"acct": 0.0, "acct_micros": 0, "camps": {}})["camps"].setdefault(str(c.get("id")), {
            "id": str(c.get("id")), "name": c.get("name", ""), "status": c.get("status", ""),
            "cost_usd": 0.0, "conv_value_usd": 0.0, "conversions": 0.0, "_micros": 0})
        mic = int(m.get("costMicros", 0))
        e["cost_usd"] += mic / 1e6; e["_micros"] += mic
        e["conv_value_usd"] += float(m.get("conversionsValue", 0) or 0)
        e["conversions"] += float(m.get("conversions", 0) or 0)
    return by


def ads_hourly(d_from, d_to, store=None):
    """{(uk_date_str, uk_hour): {"acct": usd, "camps": {cid: {...}}}} plus account meta."""
    st = store or LOLA
    ads = ADS_BY_STORE[st["key"]]
    cust = ads.search("SELECT customer.currency_code, customer.time_zone FROM customer")
    meta = (cust[0].get("customer") if cust else {}) or {}
    tz = ZoneInfo(meta.get("timeZone") or "America/New_York")
    # UK day d spans account-tz days d-1 .. d (New York is behind London). (For Everly d-1 is simply filtered out.)
    a, b = (d_from - dt.timedelta(days=1)).isoformat(), d_to.isoformat()
    where = f" WHERE segments.date BETWEEN '{a}' AND '{b}'"
    with ThreadPoolExecutor(2) as ex:
        f1 = ex.submit(ads.search, "SELECT segments.date, segments.hour, metrics.cost_micros FROM customer" + where)
        f2 = ex.submit(ads.search, "SELECT campaign.id, campaign.name, campaign.status, segments.date, segments.hour, "
                                   "metrics.cost_micros, metrics.conversions_value, metrics.conversions FROM campaign" + where)
        acct_rows, camp_rows = f1.result(), f2.result()
    by = ads_bucket(acct_rows, camp_rows, tz, d_from.isoformat(), d_to.isoformat(), st["tz"])
    return by, {"currency": meta.get("currencyCode", "USD"), "account_tz": str(tz)}


# ---------------------------------------------------------------- Shopify
_ORDER_FIELDS = """id createdAt processedAt cancelledAt test tags totalPriceSet{shopMoney{amount currencyCode}}
 subtotalPriceSet{shopMoney{amount}} discountCodes
 discountApplications(first:20){nodes{... on DiscountCodeApplication{code} ... on ManualDiscountApplication{title}
  ... on AutomaticDiscountApplication{title} ... on ScriptDiscountApplication{title}}}
 lineItems(first:100){pageInfo{hasNextPage endCursor} nodes{quantity product{legacyResourceId}
  originalTotalSet{shopMoney{amount}} discountAllocations{allocatedAmountSet{shopMoney{amount}}}}}"""
ORDERS_Q = ("query($c:String,$q:String){orders(first:250,after:$c,query:$q,sortKey:CREATED_AT){"
            "pageInfo{hasNextPage endCursor} nodes{" + _ORDER_FIELDS + "}}}")
LINES_Q = """query($id:ID!,$c:String){order(id:$id){lineItems(first:100,after:$c){pageInfo{hasNextPage endCursor}
 nodes{quantity product{legacyResourceId} originalTotalSet{shopMoney{amount}} discountAllocations{allocatedAmountSet{shopMoney{amount}}}}}}}"""


def _line(li):
    orig = pennies(li["originalTotalSet"]["shopMoney"]["amount"])
    disc = sum(pennies(x["allocatedAmountSet"]["shopMoney"]["amount"]) for x in li.get("discountAllocations") or [])
    p = li.get("product") or {}
    return {"pid": str(p["legacyResourceId"]) if p.get("legacyResourceId") else None,
            "qty": int(li.get("quantity") or 0), "list_p": orig, "value_p": orig - disc}


def parse_order(o, more_lines=()):
    """Shopify order node -> plain dict (no customer data kept: tags are reduced to the flags below)."""
    tags = [str(t).strip().lower() for t in o.get("tags") or []]
    sub = ((o.get("subtotalPriceSet") or {}).get("shopMoney") or {}).get("amount")
    money = o["totalPriceSet"]["shopMoney"]
    lines = [_line(li) for li in (o.get("lineItems") or {}).get("nodes", [])] + [_line(li) for li in more_lines]
    gid = str(o.get("id", ""))
    return {"id": int(gid.rsplit("/", 1)[-1]) if gid.rsplit("/", 1)[-1].isdigit() else 0,
            "created": _ts(o["createdAt"]), "processed": _ts(o["processedAt"]) if o.get("processedAt") else None,
            "cancelled": bool(o.get("cancelledAt")), "test": bool(o.get("test")),
            "old_order": any(t == "old-order" or t.startswith("old-order-") for t in tags),       # totals rule (existing)
            "old_order_prefix": any(t.startswith("old-order") for t in tags),                   # autokill history rule
            "test_tag": bool(set(tags) & TEST_TAGS),
            "total_p": pennies(money["amount"]), "currency": money.get("currencyCode"),
            "subtotal_p": pennies(sub) if sub is not None else None,
            "codes": [str(c) for c in o.get("discountCodes") or []],
            "disc_titles": [a.get("title") or a.get("code") or "" for a in (o.get("discountApplications") or {}).get("nodes", [])],
            "lines": lines}


def total_exclusion(o):
    """Why an order is NOT counted in the hour totals (existing rules, unchanged), else None."""
    imported = o["processed"] and (o["created"] - o["processed"]).total_seconds() > 36 * 3600
    return ("test" if o["test"] else "cancelled" if o["cancelled"] else "old_order" if o["old_order"] else
            "imported" if imported else None)


def credit_reason(o):
    """= ll-pmax-autokill newstore/data.py credit_reason: store-credit orders are never sales for the auto-kill."""
    lst, sub = sum(li["list_p"] for li in o["lines"]), o["subtotal_p"]
    if not lst or lst <= 0 or sub is None:
        return None
    share = sub / lst
    credit = (any(c.upper().startswith(CREDIT_CODE_PREFIX) for c in o["codes"])
              or any(str(t).lower().startswith(CREDIT_TITLE_PREFIX) for t in o["disc_titles"]))
    if credit and (1 - share) >= CREDIT_MAIN_SHARE:
        return "store_credit"
    if share < MIN_REVENUE_SHARE:
        return "low_revenue"
    return None


def history_exclusion(o, cutover):
    """Why an order is NOT an earlier sale for the Winners rule (mirrors the auto-kill's sales_by_product), else None."""
    if o["test"] or o["test_tag"]:
        return "test"
    if o["cancelled"]:
        return "cancelled"
    if cutover:
        if o["old_order_prefix"]:
            return "old_order"
        if o["processed"] is None or o["processed"] < _ts(cutover):
            return "before_cutover"
    if credit_reason(o):
        return "store_credit"
    return None


def okey(o):
    return (o["created"], o["id"])


class History:
    """Earlier-sale index: pid -> sorted [(createdAt, order id)] of history-eligible orders with a revenue line."""

    def __init__(self, orders, cutover):
        self.cutover, self.by_pid, self.seen = cutover, {}, set()
        self.counts = {"orders_scanned": 0, "orders_eligible": 0, "excluded": {}}
        self.add(orders)

    def add(self, orders):
        for o in orders:
            if o["id"] in self.seen:
                continue
            self.seen.add(o["id"]); self.counts["orders_scanned"] += 1
            why = history_exclusion(o, self.cutover)
            if why:
                self.counts["excluded"][why] = self.counts["excluded"].get(why, 0) + 1
                continue
            self.counts["orders_eligible"] += 1
            for pid in {li["pid"] for li in o["lines"] if li["pid"] and li["value_p"] > 0}:
                bisect.insort(self.by_pid.setdefault(pid, []), okey(o))
        return self

    def prior(self, pid, o):
        """Number of eligible orders containing pid created strictly before order o (createdAt, then id)."""
        return bisect.bisect_left(self.by_pid.get(pid, []), okey(o)) if pid else 0


def label_lines(o, hist, min_prior=None):
    mp = WINNER_MIN_PRIOR_SALES if min_prior is None else min_prior
    return ["winners" if hist is not None and li["pid"] and hist.prior(li["pid"], o) >= mp else "testing"
            for li in o["lines"]]


def split_order(o, hist, min_prior=None):
    """-> ({"testing": {"p", "units"}, "winners": {...}}, labels). Parts sum to the order total in pennies exactly."""
    lines = o["lines"]
    labels = label_lines(o, hist, min_prior)
    w = [li["value_p"] for li in lines]
    if sum(max(0, x) for x in w) <= 0:
        w = [li["qty"] for li in lines]
    parts = alloc(o["total_p"], w) if lines else []
    out = {"testing": {"p": 0, "units": 0}, "winners": {"p": 0, "units": 0}}
    if not lines:                       # no line items at all (should not happen): the money stays in testing
        out["testing"]["p"] = o["total_p"]
    for li, lab, p in zip(lines, labels, parts):
        out[lab]["p"] += p; out[lab]["units"] += li["qty"]
    return out, labels


def _empty_hour():
    return {"p": 0, "orders": 0, "mixed": 0,
            "parts": {"testing": {"p": 0, "orders": 0, "units": 0}, "winners": {"p": 0, "orders": 0, "units": 0}}}


def shop_bucket(orders, a, b, hist, min_prior=None, tz=UK, currency="GBP"):
    """Pure: parsed orders -> ({(uk_date, hour): {...}}, meta). a <= createdAt < b. hist None = no split.
    tz: the report time zone the hours are bucketed in; currency: the fallback when no order was counted."""
    by, kept, cur = {}, 0, None
    skipped = {"test": 0, "cancelled": 0, "old_order": 0, "imported": 0}
    lines_no_product = 0
    for o in orders:
        if not (a <= o["created"] < b):
            continue
        why = total_exclusion(o)
        if why:
            skipped[why] += 1
            continue
        cur = cur or o["currency"]
        u = o["created"].astimezone(tz)
        e = by.setdefault((u.date().isoformat(), u.hour), _empty_hour())
        e["p"] += o["total_p"]; e["orders"] += 1; kept += 1
        if hist is not None:
            sp, labels = split_order(o, hist, min_prior)
            for k in ("testing", "winners"):
                e["parts"][k]["p"] += sp[k]["p"]; e["parts"][k]["units"] += sp[k]["units"]
                e["parts"][k]["orders"] += int(k in labels)
            if not o["lines"]:
                e["parts"]["testing"]["orders"] += 1
            e["mixed"] += int("testing" in labels and "winners" in labels)
            lines_no_product += sum(1 for li in o["lines"] if not li["pid"])
    return by, {"currency": cur or currency, "kept": kept, "skipped": skipped, "lines_no_product": lines_no_product}


def _pull(shop, q):
    out, cursor = [], None
    for _ in range(400):
        page = shop.gql(ORDERS_Q, {"c": cursor, "q": q})["orders"]
        for o in page["nodes"]:
            more, li = [], o.get("lineItems") or {}
            c, nxt = (li.get("pageInfo") or {}).get("endCursor"), (li.get("pageInfo") or {}).get("hasNextPage")
            while nxt:                                       # orders with > 100 lines (rare)
                x = shop.gql(LINES_Q, {"id": o["id"], "c": c})["order"]["lineItems"]
                more += x["nodes"]; nxt, c = x["pageInfo"]["hasNextPage"], x["pageInfo"]["endCursor"]
            out.append(parse_order(o, more))
        if not page["pageInfo"]["hasNextPage"]:
            break
        cursor = page["pageInfo"]["endCursor"]
    return out


_hist_cache, _hist_lock = {}, threading.Lock()


def history_orders(shop, cutover):
    """Every order that can be an earlier sale: processedAt >= cutover (all time when cutover is empty). One paginated
    pass, cached CACHE_S per (store, cutover). Returns (orders, fetched_at)."""
    key, now = (getattr(shop, "domain", None), cutover), time.time()
    with _hist_lock:
        hit = _hist_cache.get(key)
        if hit and now - hit[0] < CACHE_S:
            return hit[1], hit[0]
    q = f"processed_at:>='{_iso_utc(_ts(cutover))}'" if cutover else None
    orders = _pull(shop, q)
    with _hist_lock:
        _hist_cache[key] = (now, orders)
    return orders, now


def shop_hourly(shop, d_from, d_to, with_split=True, store=None):
    st = store or LOLA
    a, b = _uk_start(d_from, st["tz"]), _uk_start(d_to + dt.timedelta(days=1), st["tz"])
    orders = _pull(shop, f"created_at:>='{_iso_utc(a)}' created_at:<'{_iso_utc(b)}'")
    hist, hmeta = None, {"ok": False, "error": None}
    if with_split:
        cfg = split_cfg(st)
        try:
            h_orders, at = history_orders(shop, cfg["cutover"])
            # the window pull is fresher than a cached history: its orders are added too (deduped by id)
            hist = History(h_orders, cfg["cutover"]).add(orders)
            hmeta = {"ok": True, "error": None, "fetched_at": dt.datetime.fromtimestamp(at, dt.timezone.utc)
                     .strftime("%Y-%m-%dT%H:%M:%SZ"), **hist.counts, "products_with_sales": len(hist.by_pid)}
        except Exception as e:
            hmeta = {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}
    by, meta = shop_bucket(orders, a, b, hist, cfg["min_prior"] if with_split else None, st["tz"], st["currency"])
    meta["history"] = hmeta
    return by, meta


# ---------------------------------------------------------------- FX
def _ecb(base, sym, d_from, d_to):
    """{uk_date_str: (rate, rate_date)} from the ECB reference rates (frankfurter), latest on or before each UK date -
    the same request the daily PnL job (RERC20/daily-pnl to_usd) makes, so hourly and daily numbers agree."""
    today = dt.datetime.now(UK).date()
    r = _S.get(f"https://api.frankfurter.dev/v1/{(d_from - dt.timedelta(days=7)).isoformat()}..{min(d_to, today).isoformat()}",
               params={"base": base, "symbols": sym}, timeout=20)
    ecb = sorted((k, v[sym]) for k, v in r.json()["rates"].items())
    if not ecb:
        raise RuntimeError("no ECB rates")
    out = {}
    for d in _days(d_from, d_to):
        prior = [x for x in ecb if x[0] <= d.isoformat()] or ecb[:1]
        out[d.isoformat()] = (prior[-1][1], prior[-1][0])
    return out


def fx_rates(d_from, d_to):
    """({uk_date_str: {"usd_gbp": (rate, day), "gbp_usd": (rate, day)}}, {"usd_gbp": source, "gbp_usd": source})"""
    ECB = "ECB reference rate via frankfurter (latest on or before the UK date)"
    out, src = {d.isoformat(): {} for d in _days(d_from, d_to)}, {"usd_gbp": None, "gbp_usd": None}
    for base, sym, key in (("USD", "GBP", "usd_gbp"), ("GBP", "USD", "gbp_usd")):
        try:
            for ds, v in _ecb(base, sym, d_from, d_to).items():
                out[ds][key] = v
            src[key] = ECB + f" {base}->{sym}"
        except Exception:
            pass
    if src["usd_gbp"] is None or src["gbp_usd"] is None:
        try:
            j = _S.get("https://open.er-api.com/v6/latest/USD", timeout=20).json()
            g = float(j["rates"]["GBP"])
            day = dt.datetime.fromtimestamp(int(j["time_last_update_unix"]), dt.timezone.utc).date().isoformat()
            for ds in out:
                if src["usd_gbp"] is None:
                    out[ds]["usd_gbp"] = (g, day)
                if src["gbp_usd"] is None:
                    out[ds]["gbp_usd"] = (round(1 / g, 6), day)
            src = {k: v or "open.er-api.com latest (fallback)" for k, v in src.items()}
        except Exception:
            pass
    return out, src


def same_currency_rates(d_from, d_to):
    """Everly: shop and ads account are both USD - rate 1.0 both ways, no lookup, same shape as fx_rates."""
    return ({d.isoformat(): {"usd_gbp": (1.0, d.isoformat()), "gbp_usd": (1.0, d.isoformat())} for d in _days(d_from, d_to)},
            {"usd_gbp": SAME_CCY, "gbp_usd": SAME_CCY})


# ---------------------------------------------------------------- Google ID token auth (/api/hourly only)
# Accepts "Authorization: Bearer <Google ID token>" (Apps Script ScriptApp.getIdentityToken()). The token is checked by
# Google's tokeninfo endpoint (signature + expiry), then: issuer is Google, email_verified is true, email is listed in
# ALLOWED_GOOGLE_EMAILS (comma separated), exp is in the future, and - when ALLOWED_GOOGLE_AUDS is set - the token's
# audience (the Apps Script project's OAuth client) is listed there. Results are cached for up to 5 minutes.
_tok_cache, _tok_lock, _seen_auds = {}, threading.Lock(), set()


def _csv_env(k):
    return {x.strip().lower() for x in _env(k).split(",") if x.strip()}


def google_ok(auth_header):
    if not auth_header.startswith("Bearer "):
        return False
    tok = auth_header[7:].strip()
    emails = _csv_env("ALLOWED_GOOGLE_EMAILS")
    if not tok or not emails or tok.count(".") != 2:
        return False
    import hashlib
    key, now = hashlib.sha256(tok.encode()).hexdigest(), time.time()
    with _tok_lock:
        hit = _tok_cache.get(key)
    if hit is None or now > hit[0]:
        try:
            r = _S.get("https://oauth2.googleapis.com/tokeninfo", params={"id_token": tok}, timeout=15)
            info = r.json() if r.status_code == 200 else {}
        except Exception:
            info = {}
        exp = int(info.get("exp", 0) or 0)
        hit = (min(now + 300, exp) if exp > now else now + 60, info)
        with _tok_lock:
            for k in [k for k, v in _tok_cache.items() if now > v[0]]:
                _tok_cache.pop(k, None)
            _tok_cache[key] = hit
    info = hit[1]
    auds = _csv_env("ALLOWED_GOOGLE_AUDS")
    aud = str(info.get("aud", "")).lower()
    ok = (info.get("iss") in ("accounts.google.com", "https://accounts.google.com")
          and str(info.get("email_verified", "")).lower() == "true"
          and str(info.get("email", "")).lower() in emails
          and int(info.get("exp", 0) or 0) > now
          and (not auds or aud in auds))
    if info and aud and aud not in _seen_auds:
        _seen_auds.add(aud)
        print(f"[hourly] Google ID token aud={aud} accepted={ok}", flush=True)   # client id only, no email/token
    return ok


# ---------------------------------------------------------------- assembly
def r2(x):
    return None if x is None else round(x + 0.0, 2)


def _usd_parts(tp, wp, g2u):
    """GBP pennies (testing, winners) -> USD cents (testing, winners) that sum to round((tp + wp) / 100 * g2u, 2)."""
    if g2u is None:
        return None, None
    tot = int(round(round((tp + wp) / 100 * g2u, 2) * 100))     # = the hour's / day's shop_sales_usd, in cents
    t, w = alloc(tot, [tp, wp]) if (tp + wp) > 0 else (0, 0)
    return t / 100, w / 100


def _camp_groups(camps, cfg):
    """Campaign rows of one hour/day -> testing / winners / other micros + values. By campaign id only."""
    g = {k: {"micros": 0, "conv_value": 0.0, "conversions": 0.0} for k in ("testing", "winners")}
    other = {}
    for c in camps.values():
        cid = str(c["id"])
        k = "testing" if cid in cfg["testing"] else "winners" if cid in cfg["winners"] else None
        if k:
            g[k]["micros"] += c["_micros"]; g[k]["conv_value"] += c["conv_value_usd"]; g[k]["conversions"] += c["conversions"]
        else:
            o = other.setdefault(cid, {"id": cid, "name": c.get("name", ""), "micros": 0})
            o["micros"] += c["_micros"]
    return g, other


def split_obj(camps, acct_micros, sh, cfg, rate, g2u, sales_ok=True):
    """One hour's (or day's) split object. camps: {cid: campaign row with _micros}; sh: shop bucket (_empty_hour shape)."""
    g, other = _camp_groups(camps, cfg)
    gbp = (lambda usd: None if rate is None else round(usd * rate, 2))
    t_usd, w_usd = _usd_parts(sh["parts"]["testing"]["p"], sh["parts"]["winners"]["p"], g2u) if sales_ok else (None, None)
    roas = (lambda s, c: None if s is None or not c else round(s / c, 2))

    def part(k, sales_usd):
        cost = g[k]["micros"] / 1e6
        p = sh["parts"][k]
        return {"campaign_ids": list(cfg[k]), "cost_usd": r2(cost), "cost_gbp": gbp(cost),
                "conv_value_usd": r2(g[k]["conv_value"]), "conversions": round(g[k]["conversions"], 2),
                "sales_gbp": p["p"] / 100 if sales_ok else None, "sales_usd": sales_usd,
                "orders": p["orders"] if sales_ok else None, "units": p["units"] if sales_ok else None,
                "roas_usd": roas(sales_usd, r2(cost))}

    t, w = part("testing", t_usd), part("winners", w_usd)
    b_cost = (g["testing"]["micros"] + g["winners"]["micros"]) / 1e6
    b_usd = None if t_usd is None else round(t_usd + w_usd, 2)
    blended = {"campaign_ids": list(cfg["testing"]) + list(cfg["winners"]), "cost_usd": r2(b_cost), "cost_gbp": gbp(b_cost),
               "conv_value_usd": r2(g["testing"]["conv_value"] + g["winners"]["conv_value"]),
               "conversions": round(g["testing"]["conversions"] + g["winners"]["conversions"], 2),
               "sales_gbp": (sh["parts"]["testing"]["p"] + sh["parts"]["winners"]["p"]) / 100 if sales_ok else None,
               "sales_usd": b_usd, "orders": sh["orders"] if sales_ok else None,
               "orders_mixed": sh["mixed"] if sales_ok else None,
               "units": sh["parts"]["testing"]["units"] + sh["parts"]["winners"]["units"] if sales_ok else None,
               "roas_usd": roas(b_usd, r2(b_cost))}
    o_micros = sum(o["micros"] for o in other.values())
    camp_micros = g["testing"]["micros"] + g["winners"]["micros"] + o_micros
    oth = {"cost_usd": r2(o_micros / 1e6), "cost_gbp": gbp(o_micros / 1e6),
           "campaigns": [{"id": o["id"], "name": o["name"], "cost_usd": r2(o["micros"] / 1e6)}
                         for o in sorted(other.values(), key=lambda o: (-o["micros"], o["id"]))],
           "account_minus_campaigns_usd": r2((acct_micros - camp_micros) / 1e6)}
    return {"testing": t, "winners": w, "blended": blended, "other": oth}


def _merge_camps(dst, camps):
    for cid, c in camps.items():
        e = dst.setdefault(cid, {"id": c["id"], "name": c.get("name", ""), "_micros": 0, "conv_value_usd": 0.0, "conversions": 0.0})
        e["_micros"] += c["_micros"]; e["conv_value_usd"] += c["conv_value_usd"]; e["conversions"] += c["conversions"]


def _merge_shop(dst, s):
    dst["p"] += s["p"]; dst["orders"] += s["orders"]; dst["mixed"] += s["mixed"]
    for k in ("testing", "winners"):
        for f in ("p", "orders", "units"):
            dst["parts"][k][f] += s["parts"][k][f]


def assemble(d_from, d_to, ads, shp, fx, errors, shop_domain=None, cfg=None, store=None):
    """Pure: fetched data -> response body. ads/shp: (by, meta) or None; fx: (rates, sources)."""
    st = store or LOLA
    cfg = cfg or split_cfg(st)
    ads_by, ads_meta = ads or ({}, {})
    shp_by, shp_meta = shp or ({}, {})
    rates, fx_src = fx
    hist = shp_meta.get("history") or {}
    split_ok = shp is not None and bool(hist.get("ok"))

    days = []
    for d in _days(d_from, d_to):
        ds = d.isoformat(); fxd = rates.get(ds, {})
        rate, rate_day = fxd.get("usd_gbp", (None, None))
        g2u, g2u_day = fxd.get("gbp_usd", (None, None))
        u = (lambda gbp: None if g2u is None else round(gbp * g2u, 2))
        g = (lambda usd: None if rate is None else round(usd * rate, 2))
        hours, tot = [], {"cost": 0.0, "p": 0, "orders": 0}
        day_camps, day_shop, day_acct_micros = {}, _empty_hour(), 0
        for h in range(24):
            a = ads_by.get((ds, h), {"acct": 0.0, "acct_micros": 0, "camps": {}}); s = shp_by.get((ds, h)) or _empty_hour()
            camps = [{k: v for k, v in dict(c, cost_usd=r2(c["cost_usd"]), conv_value_usd=r2(c["conv_value_usd"]),
                                             conversions=round(c["conversions"], 2), cost_gbp=g(c["cost_usd"]),
                                             conv_value_gbp=g(c["conv_value_usd"])).items() if not k.startswith("_")}
                     for c in sorted(a["camps"].values(), key=lambda c: -c["cost_usd"])]
            row = {"hour": h, "ads_cost_usd": r2(a["acct"]), "ads_cost_gbp": g(a["acct"]),
                   "shop_sales_gbp": r2(s["p"] / 100), "shop_sales_usd": u(s["p"] / 100), "orders": s["orders"],
                   "campaigns": camps}
            row["split"] = split_obj(a["camps"], a.get("acct_micros", 0), s, cfg, rate, g2u, split_ok)
            hours.append(row)
            tot["cost"] += a["acct"]; tot["p"] += s["p"]; tot["orders"] += s["orders"]
            _merge_camps(day_camps, a["camps"]); _merge_shop(day_shop, s); day_acct_micros += a.get("acct_micros", 0)
        days.append({"date": ds, "usd_gbp": rate, "usd_gbp_rate_date": rate_day, "gbp_usd": g2u, "gbp_usd_rate_date": g2u_day,
                     "ads_cost_usd": r2(tot["cost"]), "ads_cost_gbp": g(tot["cost"]),
                     "shop_sales_gbp": r2(tot["p"] / 100), "shop_sales_usd": u(tot["p"] / 100), "orders": tot["orders"],
                     "hours": hours,
                     "split": split_obj(day_camps, day_acct_micros, day_shop, cfg, rate, g2u, split_ok)})
    store_tag = {} if st is LOLA else {"store": st["key"]}      # Lola: no extra field (its bytes stay as before)
    return {
        **store_tag,
        "from": d_from.isoformat(), "to": d_to.isoformat(), "tz": str(st["tz"]),
        "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ads": {"customer_id": ADS_BY_STORE[st["key"]].label, "ok": ads is not None,
                "currency": ads_meta.get("currency", "USD"), "account_tz": ads_meta.get("account_tz", "America/New_York"),
                "cost_level": "customer (total account)", "error": errors.get("ads")},
        "shopify": {"store": shop_domain, "ok": shp is not None, "currency": shp_meta.get("currency", st["currency"]),
                    "orders_counted": shp_meta.get("kept"), "orders_skipped": shp_meta.get("skipped"),
                    "error": errors.get("shopify")},
        "fx": {"pair": "USD->GBP", "source": fx_src["usd_gbp"], "gbp_usd_source": fx_src["gbp_usd"], "error": errors.get("fx")},
        "days": days,
        "split_rules": split_rules(cfg, hist, split_ok, shp_meta.get("lines_no_product"), st),
    }


def split_rules(cfg, hist, ok, lines_no_product=None, store=None):
    st = store or LOLA
    return {
        "ok": ok, "error": None if ok else (hist.get("error") or "Shopify sales not available"),
        "spend": {"by": "Google Ads campaign id (never the campaign name)",
                  "testing_campaign_ids": list(cfg["testing"]), "winners_campaign_ids": list(cfg["winners"]),
                  "other": f"every other campaign of the account (e.g. {st['other_example']}), listed by id",
                  "env": [st["testing_env"], st["winners_env"]]},
        "sales": {"unit": "order line item, by product id",
                  "rule": f"winners if the product had >= {cfg['min_prior']} earlier sale(s) (orders created before this "
                          "order: createdAt, then order id), else testing; lines without a product are testing",
                  "winner_min_prior_sales": cfg["min_prior"],
                  "earlier_sale": "an order the new-store auto-kill counts as a sale (what moves a product to Winners): "
                                  + (f"processedAt >= {cfg['cutover']}, not an old-store copy (tag starting old-order), "
                                     if cfg["cutover"] else "any time, old-store copies included (no cutover), ")
                                  + "not test/cancelled/test-tagged, not a store-credit order "
                                  "(CREDIT- code or Store credit title covering >= 50% of list price, or revenue < 20% "
                                  "of list price), line value > 0",
                  "history_cutover": cfg["cutover"] or None,
                  "money": "order totalPriceSet split over its lines by line value (originalTotal - discount "
                           "allocations; shipping and tax follow the same share), largest-remainder pennies: "
                           "testing + winners = shop_sales_gbp exactly",
                  "orders": "an order counts in testing / winners if it has >= 1 line there; blended.orders_mixed = both",
                  "excluded_from_totals": "test, cancelled, old-order copies, imported (unchanged)",
                  "lines_without_product": lines_no_product},
        "history": {k: v for k, v in (hist or {}).items() if k != "error"},
    }


def build(d_from, d_to, shop, store=None):
    st = store or LOLA
    errors = {}

    def safe(name, fn, *a):
        try:
            return fn(*a)
        except Exception as e:
            errors[name] = f"{type(e).__name__}: {e}"[:300]
            return None

    with ThreadPoolExecutor(3) as ex:
        fa = ex.submit(safe, "ads", ads_hourly, d_from, d_to, st) if ADS.ok else None
        fs = ex.submit(safe, "shopify", shop_hourly, shop, d_from, d_to, True, st) if shop and shop.ok else None
        ff = ex.submit(safe, "fx", fx_rates if st["fx"] else same_currency_rates, d_from, d_to)
        ads = fa.result() if fa else None
        shp = fs.result() if fs else None
        fx = ff.result() or ({}, {"usd_gbp": None, "gbp_usd": None})
    if not ADS.ok:
        errors["ads"] = "GADS_* variables not set"
    if not (shop and shop.ok):
        errors["shopify"] = f"SHOPIFY_{st['shop_env']}_* variables not set"
    return assemble(d_from, d_to, ads, shp, fx, errors, getattr(shop, "domain", None), None, st)


def handle(qs, shop, shops=None):
    """Returns (http_code, body). shop = the LolaLooks (newlola) Shop; shops = every Shop by key (engine.SHOPS), used for
    ?store=everly. No store / store=lola: exactly the original LolaLooks feed."""
    sk = (qs.get("store") or [""])[0].strip().lower() or "lola"
    st = STORES.get(sk)
    if st is None:
        return 400, {"error": "store must be one of: " + ", ".join(STORES)}
    if st is not LOLA:
        shop = (shops or {}).get(st["shop_key"])
    today = dt.datetime.now(st["tz"]).date()
    try:
        d_from = dt.date.fromisoformat((qs.get("from") or [(today - dt.timedelta(days=1)).isoformat()])[0])
        d_to = dt.date.fromisoformat((qs.get("to") or [today.isoformat()])[0])
    except ValueError:
        return 400, {"error": "from / to must be YYYY-MM-DD"}
    if d_to < d_from:
        return 400, {"error": "to is before from"}
    if (d_to - d_from).days + 1 > MAX_DAYS:
        return 400, {"error": f"at most {MAX_DAYS} days per request"}
    key = (st["key"], d_from, d_to, tuple(sorted(split_cfg(st).items()))); now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < CACHE_S:
            return 200, dict(hit[1], cached=True, cache_age_s=int(now - hit[0]))
    body = build(d_from, d_to, shop) if st is LOLA else build(d_from, d_to, shop, st)
    if body["ads"]["ok"] and body["shopify"]["ok"] and body["split_rules"]["ok"]:
        with _lock:
            _cache[key] = (now, body)
            for k in [k for k, v in _cache.items() if now - v[0] > CACHE_S]:
                _cache.pop(k, None)
    return 200, dict(body, cached=False, cache_age_s=0)
