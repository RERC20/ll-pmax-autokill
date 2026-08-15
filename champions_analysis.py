# -*- coding: utf-8 -*-
"""Champions campaign (24047674442) analysis since launch, vs Winners (23620737018):
daily spend / pixel value / clicks, current roster from listing filters, and
TRUE ROAS (Shopify revenue for roster products / Google cost)."""
import sys, requests, time, datetime, collections
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import google_ads_connect as ga
from kill_engine_v4 import shopify_token, SHOP, SHOP_API

tok = ga.get_access_token(); H = ga._headers(tok)
def search(q):
    for a in range(5):
        r = requests.post(f"{ga.ADS_BASE}/customers/{ga.CUSTOMER_ID}/googleAds:search",
                          headers=H, json={"query": q}, timeout=60)
        if r.status_code == 200: return r.json().get("results", [])
        time.sleep(4 * (a + 1))
    r.raise_for_status()

CH, WIN = "24047674442", "23620737018"
today = datetime.date.today().isoformat()

# campaign start dates
for r in search(f"SELECT campaign.id, campaign.name, campaign.start_date FROM campaign WHERE campaign.id IN ({CH},{WIN})"):
    print(f"{r['campaign']['name']:24} start {r['campaign']['startDate']}")

def daily(cid, start):
    rows = search(f"""SELECT segments.date, metrics.cost_micros, metrics.conversions_value,
      metrics.conversions, metrics.clicks FROM campaign
      WHERE campaign.id = {cid} AND segments.date BETWEEN '{start}' AND '{today}'""")
    out = {}
    for r in rows:
        m = r["metrics"]
        out[r["segments"]["date"]] = (int(m.get("costMicros", 0))/1e6, float(m.get("conversionsValue", 0) or 0),
                                      float(m.get("conversions", 0) or 0), int(m.get("clicks", 0) or 0))
    return out

ch = daily(CH, "2026-07-18"); wn = daily(WIN, "2026-07-18")

def tot(d, since=None):
    ks = [k for k in d if not since or k >= since]
    c = sum(d[k][0] for k in ks); v = sum(d[k][1] for k in ks)
    n = sum(d[k][2] for k in ks); cl = sum(d[k][3] for k in ks)
    return c, v, n, cl

print("\n== CHAMPIONS weekly (spend / pixel value / pixROAS / clicks / CPC) ==")
weeks = collections.defaultdict(lambda: [0,0,0,0])
for k,(c,v,n,cl) in sorted(ch.items()):
    wk = datetime.date.fromisoformat(k).isocalendar()[1]
    weeks[wk][0]+=c; weeks[wk][1]+=v; weeks[wk][2]+=n; weeks[wk][3]+=cl
for wk,(c,v,n,cl) in sorted(weeks.items()):
    print(f"  W{wk}: £{c:7.2f} | val £{v:7.2f} | pixROAS {v/c if c else 0:4.2f} | conv {n:4.1f} | clicks {cl:4d} | CPC £{c/cl if cl else 0:.2f}")

# current roster from listing filters (variant item-ids -> product ids)
pids = set()
for r in search("""SELECT asset_group_listing_group_filter.type,
  asset_group_listing_group_filter.case_value.product_item_id.value
  FROM asset_group_listing_group_filter WHERE asset_group.id = 6731971798"""):
    f = r["assetGroupListingGroupFilter"]
    if f["type"] == "UNIT_INCLUDED":
        v = ((f.get("caseValue") or {}).get("productItemId") or {}).get("value", "")
        parts = v.split("_")
        if len(parts) >= 3: pids.add(parts[2])
print(f"\ncurrent roster: {len(pids)} products")

# Shopify TRUE revenue for roster products since Jul 20
stok = shopify_token()
Q = """query($c:String){ orders(first:100, after:$c, query:"created_at:>=2026-07-20"){
  pageInfo{hasNextPage endCursor}
  edges{node{ lineItems(first:60){edges{node{ product{legacyResourceId} discountedTotalSet{shopMoney{amount}} }}} }}}}"""
rev = 0.0; orders_with = 0; cur = None
while True:
    d = requests.post(f"https://{SHOP}/admin/api/{SHOP_API}/graphql.json",
                      headers={"X-Shopify-Access-Token": stok, "Content-Type": "application/json"},
                      json={"query": Q, "variables": {"c": cur}}, timeout=60).json()["data"]["orders"]
    for e in d["edges"]:
        hit = 0.0
        for le in e["node"]["lineItems"]["edges"]:
            p = le["node"].get("product") or {}
            if str(p.get("legacyResourceId")) in pids:
                hit += float(le["node"]["discountedTotalSet"]["shopMoney"]["amount"])
        if hit: rev += hit; orders_with += 1
    if not d["pageInfo"]["hasNextPage"]: break
    cur = d["pageInfo"]["endCursor"]

c_all = tot(ch); w_all = tot(wn)
c_14 = tot(ch, (datetime.date.today()-datetime.timedelta(days=13)).isoformat())
w_14 = tot(wn, (datetime.date.today()-datetime.timedelta(days=13)).isoformat())
print(f"\n== TOTALS since Jul 18 ==")
print(f"CHAMPIONS: spend £{c_all[0]:.2f} | pixel val £{c_all[1]:.2f} | pixROAS {c_all[1]/c_all[0] if c_all[0] else 0:.2f} | CPC £{c_all[0]/c_all[3] if c_all[3] else 0:.2f}")
print(f"WINNERS  : spend £{w_all[0]:.2f} | pixel val £{w_all[1]:.2f} | pixROAS {w_all[1]/w_all[0] if w_all[0] else 0:.2f} | CPC £{w_all[0]/w_all[3] if w_all[3] else 0:.2f}")
print(f"\n== last 14 days ==")
print(f"CHAMPIONS: spend £{c_14[0]:.2f} | pixROAS {c_14[1]/c_14[0] if c_14[0] else 0:.2f}")
print(f"WINNERS  : spend £{w_14[0]:.2f} | pixROAS {w_14[1]/w_14[0] if w_14[0] else 0:.2f}")
print(f"\nTRUE (Shopify) revenue of CURRENT {len(pids)}-product roster since Jul 20: £{rev:.2f} in {orders_with} orders")
print(f"TRUE ROAS vs champions spend since Jul 18: {rev/c_all[0] if c_all[0] else 0:.2f}")
print("(caveat: roster changed over time — demoted products' revenue not counted; Shopify rev includes non-ad orders)")
