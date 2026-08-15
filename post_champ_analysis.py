# -*- coding: utf-8 -*-
"""Per-campaign spend / pixel ROAS / TRUE ROAS since Champions was paused (Aug 7+).
TRUE = Shopify revenue of the campaign's product set / campaign spend.
Sets: Winners = tag w_campaign; Testing|AW = the 530 aw26 pids; Testing|UK = the rest."""
import sys, requests, time, datetime, collections
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from pathlib import Path
import google_ads_connect as ga
from kill_engine_v4 import shopify_token, SHOP, SHOP_API
from zoneinfo import ZoneInfo
UK = ZoneInfo("Europe/London")

tok = ga.get_access_token(); H = ga._headers(tok)
def search(q):
    for a in range(5):
        r = requests.post(f"{ga.ADS_BASE}/customers/{ga.CUSTOMER_ID}/googleAds:search",
                          headers=H, json={"query": q}, timeout=60)
        if r.status_code == 200: return r.json().get("results", [])
        time.sleep(4 * (a + 1))
    r.raise_for_status()

CAMPS = {"24027270949": "Testing | UK", "24116871559": "Testing | AW", "23620737018": "Winners",
         "24047674442": "Champions (paused)"}
START = "2026-08-07"
today = datetime.datetime.now(UK).date().isoformat()

g = {cid: [0.0, 0.0] for cid in CAMPS}   # cost, pixel value
daily = collections.defaultdict(lambda: collections.defaultdict(float))
for r in search(f"""SELECT campaign.id, segments.date, metrics.cost_micros, metrics.conversions_value
  FROM campaign WHERE campaign.id IN ({','.join(CAMPS)}) AND segments.date BETWEEN '{START}' AND '{today}'"""):
    cid = str(r["campaign"]["id"]); m = r["metrics"]
    c = int(m.get("costMicros", 0)) / 1e6; v = float(m.get("conversionsValue", 0) or 0)
    g[cid][0] += c; g[cid][1] += v
    daily[r["segments"]["date"]][cid] += c

# product sets
stok = shopify_token()
def gql(q, v=None):
    return requests.post(f"https://{SHOP}/admin/api/{SHOP_API}/graphql.json",
                         headers={"X-Shopify-Access-Token": stok, "Content-Type": "application/json"},
                         json={"query": q, "variables": v or {}}, timeout=90).json()
winners = set(); cur = None
while True:
    d = gql('query($c:String){products(first:250,after:$c,query:"tag:w_campaign status:active"){pageInfo{hasNextPage endCursor} edges{node{legacyResourceId}}}}', {"c": cur})["data"]["products"]
    for e in d["edges"]: winners.add(str(e["node"]["legacyResourceId"]))
    if not d["pageInfo"]["hasNextPage"]: break
    cur = d["pageInfo"]["endCursor"]
aw = set(Path(r"C:\Users\Rashad\Downloads\autoimp folder\cm2_run\qa_ids.txt").read_text().replace(",", " ").split())

rev = {"Winners": 0.0, "Testing | AW": 0.0, "Testing | UK": 0.0, "orders": 0}
cur = None
Q = ('query($c:String){orders(first:100,after:$c,query:"created_at:>=%s -status:cancelled"){'
     'pageInfo{hasNextPage endCursor} edges{node{createdAt lineItems(first:60){edges{node{'
     'product{legacyResourceId} discountedTotalSet{shopMoney{amount}}}}}}}}}' % START)
while True:
    d = gql(Q, {"c": cur})["data"]["orders"]
    for e in d["edges"]:
        rev["orders"] += 1
        for le in e["node"]["lineItems"]["edges"]:
            p = le["node"].get("product") or {}
            pid = str(p.get("legacyResourceId")); amt = float(le["node"]["discountedTotalSet"]["shopMoney"]["amount"])
            if pid in winners: rev["Winners"] += amt
            elif pid in aw: rev["Testing | AW"] += amt
            else: rev["Testing | UK"] += amt
    if not d["pageInfo"]["hasNextPage"]: break
    cur = d["pageInfo"]["endCursor"]

print(f"window: {START} -> {today} (Champions paused {START}) | winners set {len(winners)} | aw26 set {len(aw)} | orders {rev['orders']}")
print(f"\n{'campaign':22} {'spend':>9} {'pixROAS':>8} {'true rev':>10} {'TRUE ROAS':>9}")
tot_c = tot_r = 0.0
for cid, name in CAMPS.items():
    c, v = g[cid]
    tr = rev.get(name.replace(" (paused)", ""), 0.0) if "Champions" not in name else 0.0
    tot_c += c; tot_r += tr
    print(f"{name:22} £{c:8.2f} {v/c if c else 0:8.2f} £{tr:9.2f} {tr/c if c else 0:9.2f}")
print(f"{'BLENDED (ads-set rev)':22} £{tot_c:8.2f} {'':8} £{tot_r:9.2f} {tot_r/tot_c if tot_c else 0:9.2f}")
print("\ndaily spend:")
for dt in sorted(daily):
    row = "  ".join(f"{CAMPS[c][:12]} £{daily[dt][c]:6.2f}" for c in CAMPS if daily[dt][c] > 0.005)
    print(f"  {dt}  {row}")
