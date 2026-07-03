#!/usr/bin/env python3
# kill_engine_google.py — PMax Auto-Kill, GOOGLE-DIRECT data (hourly-fresh)
# ---------------------------------------------------------------------------
# Same v4 rules + logging + draft/tag as kill_engine_v4.py (imported, so they
# stay identical), but instead of Pythago it builds the feed itself:
#   • Google Ads API  -> per-product cost / clicks / pixel-conv-value (7/14/30d)
#                        from shopping_performance_view (item_id = shopify_zz_<pid>_<vid>)
#   • Shopify orders  -> REAL revenue + order count per product (7/14/30d)  [the truth]
#   • universe        -> live Shopify ACTIVE products only (built-in cross-check, no 15h staleness)
# ROAS = Shopify revenue / Google cost (Shopify-truth, per the v4 reconciliation).
#
# Run:
#   python kill_engine_google.py          # interactive: shows kills, asks yes/no
#   python kill_engine_google.py --auto   # unattended (for the hourly task): drafts + logs, no prompt
# ---------------------------------------------------------------------------
import sys, argparse, datetime, collections, requests
from openpyxl import Workbook
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# reuse the EXACT rules + logging + Shopify write from the Pythago engine
import google_ads_connect as ga                              # Google creds + token + headers
from kill_engine_v4 import evaluate, norm, shopify_token, shopify_draft, _Tee, SHOP, SHOP_API, KILL_CAP
from zoneinfo import ZoneInfo
UK = ZoneInfo('Europe/London')                              # store + Google Ads account run on UK time

PMAX_CAMPAIGN_ID = '23620737018'                             # PMAX | Feed Only | All Products | UK (FYI)
RUN_LOG   = 'kill_engine_google_runs.log'
KILLS_LOG = 'kills_log_google.csv'

def _gql(tok, q, v=None):
    return requests.post(f"https://{SHOP}/admin/api/{SHOP_API}/graphql.json",
        headers={'X-Shopify-Access-Token':tok,'Content-Type':'application/json'},
        json={'query':q,'variables':v or {}}, timeout=60).json()

# ── Google Ads: per-PRODUCT cost/clicks/pixel-value for 7/14/30 day windows ──
def google_product_perf(run_date):
    tok=ga.get_access_token()
    base=ga.ADS_BASE; cid=ga.CUSTOMER_ID
    def search(q):
        r=requests.post(f"{base}/customers/{cid}/googleAds:search", headers=ga._headers(tok), json={'query':q}, timeout=60)
        r.raise_for_status(); return r.json().get('results', [])
    g=collections.defaultdict(lambda: {'cost7':0.0,'cost30':0.0,'clicks30':0})
    # Windows = ROLLING last N days, INCLUDING today (matches the dashboard timing fix). A same-day
    # sale counts immediately, so a cross-sell / low-spend sale isn't mis-read as £0 revenue and
    # false-killed. Rolling (vs the old "ending yesterday") = no abrupt midnight boundary. RULES UNCHANGED.
    end=run_date.isoformat()                                        # TODAY (included)
    for win,days in (('7',7),('30',30)):
        start=(run_date-datetime.timedelta(days=days-1)).isoformat()  # today + (N-1) prior days = N dates
        q=(f"SELECT segments.product_item_id, metrics.cost_micros, metrics.clicks "
           f"FROM shopping_performance_view WHERE segments.date BETWEEN '{start}' AND '{end}'")
        for row in search(q):
            parts=str(row['segments']['productItemId']).split('_')      # shopify_zz_<pid>_<vid>
            pid=norm(parts[2]) if len(parts)>=3 else None
            if not pid: continue
            g[pid][f'cost{win}'] += int(row['metrics'].get('costMicros',0))/1e6
            if win=='30': g[pid]['clicks30'] += int(row['metrics'].get('clicks',0) or 0)
    return g

# ── Shopify: live ACTIVE products (id, title, publishedAt, tags) ──
def shopify_active_products(tok):
    Q=('query($c:String){products(first:100,after:$c,query:"status:active"){'
       'pageInfo{hasNextPage endCursor} edges{node{legacyResourceId title publishedAt tags}}}}')
    prods={}; cur=None
    while True:
        c=_gql(tok,Q,{'c':cur})['data']['products']
        for e in c['edges']:
            n=e['node']; pid=norm(n['legacyResourceId'])
            prods[pid]=dict(name=n.get('title',''), pub=str(n.get('publishedAt') or '')[:10] or '2000-01-01',
                            tags=n.get('tags') or [])
        if c['pageInfo']['hasNextPage']: cur=c['pageInfo']['endCursor']
        else: break
    return prods

# ── Shopify: REAL revenue + order count per product, for 7/14/30 day windows ──
def shopify_revenue(tok, run_date):
    # GROSS sales by design — we do NOT subtract refunds. A refund is usually an order
    # issue (size/shipping/changed mind), not a sign the product is bad; if it sold, it
    # counts. So a refunded sale still proves the product can sell and won't be killed for it.
    # Discounts DO count (2026-07-03): revenue = amount actually paid after ALL discounts (incl order-level).
    since=(run_date-datetime.timedelta(days=31)).isoformat()
    now_uk=datetime.datetime.now(UK)                                 # precise clock for rolling N×24h windows (include today)
    Q=('query($c:String){orders(first:100,after:$c,query:"created_at:>=%s"){'
       'pageInfo{hasNextPage endCursor} edges{node{id createdAt subtotalPriceSet{shopMoney{amount}} '
       'lineItems(first:100){edges{node{product{legacyResourceId} discountedTotalSet{shopMoney{amount}}}}}}}}}' % since)
    rev=collections.defaultdict(lambda:[0.0,0.0,0.0])
    orders=collections.defaultdict(lambda:[set(),set(),set()])
    cur=None
    while True:
        c=_gql(tok,Q,{'c':cur})['data']['orders']
        for e in c['edges']:
            node=e['node']; oid=node['id']
            ca_dt=datetime.datetime.fromisoformat(node['createdAt'].replace('Z','+00:00')).astimezone(UK)
            delta=(now_uk-ca_dt).total_seconds()/86400.0        # rolling age in days -> window = last N×24h INCLUDING today
            # DISCOUNT ALLOCATION: order-level "ACROSS" discounts (Buy-2-10%-off) are NOT pushed into line
            # discountedTotalSet, so summing lines over-states revenue. subtotalPriceSet = revenue after ALL
            # discounts, GROSS of refunds (original field) -> scale each line so rev = what it actually sold for.
            line_sum=sum(float(li['node']['discountedTotalSet']['shopMoney']['amount'])
                         for li in node['lineItems']['edges'] if li['node'].get('product'))
            _sub=(node.get('subtotalPriceSet') or {}).get('shopMoney',{}).get('amount')
            factor=((float(_sub)/line_sum) if (_sub is not None and line_sum>0) else 1.0)
            for li in node['lineItems']['edges']:
                pr=li['node'].get('product')
                if not pr: continue
                pid=norm(pr['legacyResourceId']); amt=float(li['node']['discountedTotalSet']['shopMoney']['amount'])*factor
                for i,d in enumerate((7,14,30)):
                    if 0<=delta<=d: rev[pid][i]+=amt; orders[pid][i].add(oid)   # rolling, INCLUDES today
        if c['pageInfo']['hasNextPage']: cur=c['pageInfo']['endCursor']
        else: break
    cnt={pid:[len(s) for s in sets] for pid,sets in orders.items()}
    return rev, cnt

# ── merge -> normalized product dicts (same shape kill_engine_v4.evaluate expects) ──
def build_feed(run_date):
    g=google_product_perf(run_date)
    stok=shopify_token()
    prods=shopify_active_products(stok)
    rev,cnt=shopify_revenue(stok, run_date)
    feed=[]
    for pid,meta in prods.items():
        gp=g.get(pid, {}); r=rev.get(pid,[0.0,0.0,0.0]); c=cnt.get(pid,[0,0,0])
        cost7=gp.get('cost7',0.0)
        feed.append(dict(pid=pid, name=meta['name'], pub=meta['pub'], tags=meta['tags'],
            cost30=gp.get('cost30',0.0), cost7=cost7, clicks30=gp.get('clicks30',0),
            rev30=r[2], rev14=r[1], rev7=r[0],                       # Shopify = truth
            roas7=(r[0]/cost7) if cost7>0 else 0.0,                  # Shopify rev 7d / Google cost 7d
            offconv30=c[2]))
    return feed

# ── kills log (separate, same columns as v4) ──
def _write_kills_log(kills, outcomes, run_date, action, ts):
    import csv as _csv, os as _os
    new=not _os.path.exists(KILLS_LOG)
    with open(KILLS_LOG,'a',newline='',encoding='utf-8') as f:
        w=_csv.writer(f)
        if new: w.writerow(['timestamp','data_date','mode','outcome','product_id','name','tier','reason',
                            'days_live','cost_7d','cost_30d','clicks_30d','shop_rev_30d','shop_rev_14d','shop_rev_7d','roas_7d'])
        for p,tier,why in sorted(kills,key=lambda k:k[1]):
            dl=(run_date-datetime.date.fromisoformat(p['pub'])).days
            w.writerow([ts, run_date.isoformat(), action, outcomes.get(p['pid'],''), p['pid'], p['name'], tier, why, dl,
                        round(p['cost7'],2), round(p['cost30'],2), p['clicks30'],
                        round(p['rev30'],2), round(p['rev14'],2), round(p['rev7'],2), round(p['roas7'],2)])

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--auto', action='store_true', help='unattended: draft + tag without prompting (for the hourly task)')
    ap.add_argument('--run-date', help='YYYY-MM-DD (default: today)')
    ap.add_argument('--out', default='kill_list_google.xlsx')
    a=ap.parse_args()
    run_date=datetime.date.fromisoformat(a.run_date) if a.run_date else datetime.datetime.now(UK).date()
    is_monday=(run_date.weekday()==0)
    ts=datetime.datetime.now(UK).strftime('%Y-%m-%d %H:%M:%S')

    logf=open(RUN_LOG,'a',encoding='utf-8')
    logf.write(f"\n{'#'*72}\n# RUN {ts}  |  GOOGLE-DIRECT  |  {'AUTO' if a.auto else 'interactive'}  |  data-date {run_date}\n{'#'*72}\n")
    real_stdout=sys.stdout; sys.stdout=_Tee(real_stdout, logf)
    try:
        print(f"== kill_engine GOOGLE == {run_date} ({run_date.strftime('%A')}) | Mon tiers 3&4 {'ON' if is_monday else 'OFF'} | {ts}")
        feed=build_feed(run_date)
        print(f"feed: Google Ads + live Shopify -> {len(feed)} active products (cost from Google, revenue from Shopify orders)")

        # GLITCH GUARD: a live store always has some Shopify revenue over 30 days. If the orders
        # pull came back empty (£0 across EVERY product) it's a data glitch (failed/empty Shopify
        # response), not reality — every spending product would falsely read £0-revenue and look
        # killable. Abort before flagging or drafting anything.
        if sum(p.get('rev30',0) for p in feed) <= 0:
            print(f"\n!! ABORTED — Shopify returned £0 revenue across ALL {len(feed)} active products over 30 days."
                  f"\n   That's an empty/failed orders pull, not real sales. Nothing flagged or drafted — re-run after Shopify recovers.")
            return

        kills=[]; tiers={}
        for p in feed:
            dec,tier,why=evaluate(p, run_date, is_monday)
            if dec=='KILL': kills.append((p,tier,why)); tiers[tier]=tiers.get(tier,0)+1

        wb=Workbook(); ws=wb.active; ws.title='kill list google'
        ws.append(['Product ID','Product Name','Kill Tier','Reason','cost_30','clicks_30','ShopRev_30','ShopRev_14','ShopRev_7','ROAS_7','cost_7'])
        for p,tier,why in sorted(kills,key=lambda k:k[1]):
            ws.append([int(p['pid']),p['name'],tier,why,round(p['cost30'],2),p['clicks30'],
                       round(p['rev30'],2),round(p['rev14'],2),round(p['rev7'],2),round(p['roas7'],2),round(p['cost7'],2)])
        for c in ws['A'][1:]: c.number_format='0'
        wb.save(a.out)

        print(f"\n{'='*72}\nPRODUCTS TO KILL: {len(kills)}   (by tier: {tiers or '-'})   |   kept: {len(feed)-len(kills)}\n{'='*72}")
        for i,(p,tier,why) in enumerate(sorted(kills,key=lambda k:k[1]),1):
            dl=(run_date-datetime.date.fromisoformat(p['pub'])).days
            print(f"\n{i}. Product ID {p['pid']}   [{tier}]   {p['name'][:52]}")
            print(f"   reason : {why}")
            print(f"   metrics: days_live={dl} | spend 7d/30d=£{p['cost7']:.2f}/£{p['cost30']:.2f} | "
                  f"clicks30={p['clicks30']} | Shopify rev 30d/14d/7d=£{p['rev30']:.2f}/£{p['rev14']:.2f}/£{p['rev7']:.2f} | ROAS7={p['roas7']:.2f}")
        print(f"\nsaved -> {a.out}")

        outcomes={p['pid']:'FLAGGED' for p,_,_ in kills}
        action='no kills'
        if not kills:
            print("\nNothing to kill this run.")
        elif len(kills)>KILL_CAP:
            for k in outcomes: outcomes[k]=f'NOT DRAFTED (over cap {KILL_CAP})'
            action='BLOCKED (over cap)'
            print(f"\n** SAFETY STOP: {len(kills)} kills > cap {KILL_CAP}. Not drafting — investigate. (flagged list logged.) **")
        else:
            if a.auto:
                go=True
            else:
                print(f"\n>>> {len(kills)} product(s) flagged. What do you want to do?")
                print("       yes  =  DRAFT them in Shopify + tag 'draft_bad_product', then log")
                print("       no   =  exit WITHOUT drafting (still logs the flagged list)")
                print("     type yes or no: ", end='', flush=True)
                ans=input().strip().lower(); print(f"   (you typed: {ans or '<empty>'})"); go=ans in ('y','yes')
            if not go:
                action='EXITED (no draft)'
                for k in outcomes: outcomes[k]='NOT DRAFTED (you exited)'
                print("\nExiting without drafting. The flagged list has been logged.")
            else:
                action='DRAFTED'
                print(f"\nDrafting {len(kills)} product(s) + tagging 'draft_bad_product' ...")
                wtok=shopify_token()
                for p,tier,why in kills:
                    res=shopify_draft(wtok, p['pid'])
                    outcomes[p['pid']]='DRAFTED' if res=='ok' else f'ERROR: {res}'
                    print(f"  - {p['pid']}  {'DRAFTED + tagged draft_bad_product' if res=='ok' else 'ERROR: '+res}")
                print(f"\nDone: {sum(1 for v in outcomes.values() if v=='DRAFTED')}/{len(kills)} drafted & tagged 'draft_bad_product'.")

        if kills: _write_kills_log(kills, outcomes, run_date, action, ts)
        print(f"\nlogs: run -> {RUN_LOG}" + (f"   |   kills -> {KILLS_LOG} (+{len(kills)} rows)" if kills else ""))
    finally:
        sys.stdout=real_stdout; logf.flush(); logf.close()

if __name__=='__main__':
    main()
