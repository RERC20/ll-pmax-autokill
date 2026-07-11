#!/usr/bin/env python3
# backfill_winner_tags.py — ONE-OFF (run 2026-07-11)
# ---------------------------------------------------------------------------
# Tags every currently-ACTIVE product that has >=1 LIFETIME Shopify sale with
# the w_campaign tag (winners). Shopify orders are the GROUND TRUTH — Google's
# conversion reporting is never consulted, so an under-reported sale can't
# cost a product its winner status.
#
# After this backfill, the auto engine's rev30-based tagger covers every NEW
# first sale (a first sale is always inside the 30d window), so this script
# never needs to run again.
#
# Rules (owner-approved):
#   - ACTIVE products only (drafted/killed products stay untagged even if they
#     had sales — they are dead; if the owner ever reactivates one and it sells
#     again, the engine tags it then).
#   - Any paid order counts, refunded included (a refund is an order issue,
#     not proof the product can't sell — same GROSS convention as the engine).
#   - Cancelled orders are excluded.
# ---------------------------------------------------------------------------
import sys, csv, datetime, requests
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
from kill_engine_v4 import shopify_token, SHOP, SHOP_API

WINNER_TAG = 'w_campaign'
SINCE = '2026-01-01'          # store launched March 2026 — January is a safe floor

def gql(tok, q, v=None):
    import time
    for _ in range(6):
        r = requests.post(f"https://{SHOP}/admin/api/{SHOP_API}/graphql.json",
                          headers={'X-Shopify-Access-Token': tok, 'Content-Type': 'application/json'},
                          json={'query': q, 'variables': v or {}}, timeout=90).json()
        if 'errors' in r and any('THROTTLED' in str(e) for e in r['errors']):
            time.sleep(2); continue
        return r
    return r

def main():
    dry = '--dry' in sys.argv
    tok = shopify_token()

    # 1) every product id that appears on any non-cancelled order since SINCE
    print(f"scanning ALL orders since {SINCE} (lifetime sales, Shopify = truth)...")
    Q = ('query($c:String){orders(first:100,after:$c,query:"created_at:>=%s -status:cancelled"){'
         'pageInfo{hasNextPage endCursor} edges{node{id '
         'lineItems(first:100){edges{node{product{legacyResourceId}}}}}}}}' % SINCE)
    sold = {}; cur = None; n_orders = 0
    while True:
        d = gql(tok, Q, {'c': cur})['data']['orders']
        for e in d['edges']:
            n_orders += 1
            for li in e['node']['lineItems']['edges']:
                pr = li['node'].get('product')
                if pr:
                    pid = str(pr['legacyResourceId'])
                    sold[pid] = sold.get(pid, 0) + 1
        if not d['pageInfo']['hasNextPage']: break
        cur = d['pageInfo']['endCursor']
    print(f"  {n_orders} orders -> {len(sold)} distinct products ever sold")

    # 2) currently ACTIVE products + tags
    Q2 = ('query($c:String){products(first:250,after:$c,query:"status:active"){'
          'pageInfo{hasNextPage endCursor} edges{node{legacyResourceId title tags}}}}')
    active = {}; cur = None
    while True:
        d = gql(tok, Q2, {'c': cur})['data']['products']
        for e in d['edges']:
            n = e['node']
            active[str(n['legacyResourceId'])] = dict(title=n['title'], tags=n.get('tags') or [])
        if not d['pageInfo']['hasNextPage']: break
        cur = d['pageInfo']['endCursor']
    print(f"  {len(active)} active products")

    # 3) winners = active AND ever sold AND not yet tagged
    todo = [(pid, active[pid]) for pid in active
            if pid in sold and WINNER_TAG not in active[pid]['tags']]
    already = sum(1 for pid in active if WINNER_TAG in active[pid]['tags'])
    print(f"\nwinners to tag: {len(todo)}  (already tagged: {already})")

    M = 'mutation($id:ID!,$t:[String!]!){tagsAdd(id:$id,tags:$t){userErrors{message}}}'
    rows = []
    for pid, meta in todo:
        if dry:
            res = 'DRY'
        else:
            j = gql(tok, M, {'id': f"gid://shopify/Product/{pid}", 't': [WINNER_TAG]})
            errs = (j.get('data', {}).get('tagsAdd') or {}).get('userErrors') or []
            res = 'ok' if not errs else f"err: {errs[0].get('message','?')[:60]}"
        rows.append((pid, meta['title'], sold[pid], res))
        print(f"  {res:4} {pid}  ({sold[pid]} lifetime orders)  {meta['title'][:52]}")

    out = f"winner_backfill_{datetime.date.today()}.csv"
    with open(out, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f); w.writerow(['product_id', 'title', 'lifetime_orders', 'outcome'])
        w.writerows(rows)
    ok = sum(1 for r in rows if r[3] == 'ok')
    print(f"\nDONE: {ok}/{len(todo)} tagged {WINNER_TAG}{' (DRY)' if dry else ''} | log -> {out}")

if __name__ == '__main__':
    main()
