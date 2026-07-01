#!/usr/bin/env python3
# kill_engine_v4.py  —  PMax Auto-Kill Engine (rules v4)
# ---------------------------------------------------------------------------
# Daily check:
#   1. Pull ACTIVE products live from the Pythago API (7/14/30-day metrics).
#   2. Cross-check each Product ID against LIVE Shopify status; drop anything
#      not currently 'active' (Pythago can be ~15h stale).
#   3. Apply the v4 six-tier rules (revenue = Shopify offline value, NOT count).
#   4. Output the Product IDs to kill + why.  Dry-run by default.
#      --apply  => actually set those products to DRAFT in Shopify (+ kill tags).
#
# Usage:
#   python kill_engine_v4.py                 # live Pythago pull, dry-run
#   python kill_engine_v4.py --apply         # live pull + draft the kills in Shopify
#   python kill_engine_v4.py --csv feed.csv --run-date 2026-06-28   # test on an export
#
# NOTE: the Pythago API blocks datacenter IPs (Cloudflare). Run this from a
#       normal machine/server IP — the same place your other store scripts run.
# ---------------------------------------------------------------------------
import csv, sys, argparse, datetime, requests
from openpyxl import Workbook
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
from zoneinfo import ZoneInfo
from creds import cred
UK = ZoneInfo('Europe/London')                      # store + ad account run on UK time

# ── config ──────────────────────────────────────────────────────────────
# Secrets come from env vars / GitHub Secrets (local: the git-ignored _secrets_local.py).
PYTHAGO_BASE = 'https://api.pythago.io/api/v1'
PYTHAGO_KEY  = cred('PYTHAGO_KEY')
STORE_ID     = 313                                  # Pythago store id
SHOP   = cred('SHOPIFY_STORE')
SHOP_CID = cred('SHOPIFY_CLIENT_ID')
SHOP_CSEC = cred('SHOPIFY_CLIENT_SECRET')
SHOP_API = '2026-01'
KILL_CAP = 25                                       # safety valve: pause if > N kills in one run
RUN_LOG   = 'kill_engine_runs.log'                  # full console output of every run (appended)
KILLS_LOG = 'kills_log.csv'                         # one row per flagged/drafted product (appended)

def norm(v):
    s=str(v).strip()
    if s.endswith('.0'): s=s[:-2]
    return ''.join(c for c in s if c.isdigit()) or None

# ── data sources (both return the same normalized product dict) ──────────
# normalized dict: pid,name,pub,cost30,clicks30,cost7,rev30,rev14,rev7,roas7,offconv30
#   rev* = Shopify offline revenue = GROSS sales. Refunds are NOT subtracted: a refund is
#   usually an order issue (size/shipping), not a bad-product signal — if it sold, it counts.
def _normalize_api(p):
    def m(w,k):
        try: return float((p.get(f'metrics_{w}d') or {}).get(k) or 0)
        except (TypeError,ValueError): return 0.0
    return dict(pid=norm(p['external_id']), name=p.get('name',''), pub=str(p.get('published_at',''))[:10],
        cost30=m(30,'total_cost'), clicks30=int(m(30,'clicks')), cost7=m(7,'total_cost'),
        rev30=m(30,'offline_revenue'), rev14=m(14,'offline_revenue'), rev7=m(7,'offline_revenue'),
        roas7=m(7,'roas_total'), offconv30=m(30,'offline_conversions'))

def _fetch_pythago_curl():
    """Fast path: curl_cffi impersonating Chrome. Returns list, or None if Cloudflare-blocked."""
    try:
        from curl_cffi import requests as creq
    except ImportError:
        return None
    # Mimic a same-origin XHR from the docs page: the WAF wants Referer + Sec-Fetch-*
    hdr={
        'Authorization': f'Bearer {PYTHAGO_KEY}',
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'en-GB,en;q=0.9',
        'Referer': f'{PYTHAGO_BASE}/docs/',
        'Origin': 'https://api.pythago.io',
        'Sec-Fetch-Site': 'same-origin',
        'Sec-Fetch-Mode': 'cors',
        'Sec-Fetch-Dest': 'empty',
        'X-Requested-With': 'XMLHttpRequest',
    }
    out=[]; offset=0
    with creq.Session() as s:                  # explicit close -> avoids WinError 6 at exit
        while True:
            url=f"{PYTHAGO_BASE}/dashboard/products/?store_id={STORE_ID}&status=active&limit=250&offset={offset}"
            r=s.get(url, headers=hdr, impersonate='chrome', timeout=60)
            if r.status_code==403: return None
            r.raise_for_status()
            d=r.json()['data']
            out += [_normalize_api(p) for p in d['products']]
            if not d['pagination']['has_next']: break
            offset+=250
    return out

def _fetch_pythago_browser(headless=True):
    """Robust path: drive a real Chrome (undetected-chromedriver) past Cloudflare,
    then fetch the API from inside the api.pythago.io page context."""
    try:
        import undetected_chromedriver as uc
    except ImportError:
        sys.exit("Real-browser fetch needs:  pip install undetected-chromedriver")
    import json as _json, time as _t
    opts=uc.ChromeOptions()
    if headless: opts.add_argument('--headless=new')
    opts.add_argument('--window-size=1280,900')
    driver=uc.Chrome(options=opts)
    try:
        driver.get(f'{PYTHAGO_BASE}/docs/')          # land on api.pythago.io; let Cloudflare clear
        _t.sleep(8)
        driver.set_script_timeout(180)
        js=r"""
        const KEY=arguments[0], STORE=arguments[1], done=arguments[2];
        (async()=>{ try{
          let all=[], offset=0;
          while(true){
            const r=await fetch(`/api/v1/dashboard/products/?store_id=${STORE}&status=active&limit=250&offset=${offset}`,{headers:{'Authorization':'Bearer '+KEY}});
            if(!r.ok){ done('HTTP '+r.status); return; }
            const j=await r.json();
            for(const p of j.data.products){
              const m=(w,k)=>{const o=p['metrics_'+w+'d']||{};const v=parseFloat(o[k]);return isNaN(v)?0:v;};
              all.push({pid:String(p.external_id),name:p.name||'',pub:(p.published_at||'').slice(0,10),
                cost30:m(30,'total_cost'),clicks30:Math.round(m(30,'clicks')),cost7:m(7,'total_cost'),
                rev30:m(30,'offline_revenue'),rev14:m(14,'offline_revenue'),rev7:m(7,'offline_revenue'),
                roas7:m(7,'roas_total'),offconv30:m(30,'offline_conversions')});
            }
            if(!j.data.pagination.has_next) break; offset+=250;
          }
          done(JSON.stringify(all));
        }catch(e){ done('ERR:'+e.message); } })();
        """
        res=driver.execute_async_script(js, PYTHAGO_KEY, STORE_ID)
    finally:
        try: driver.quit()
        except Exception: pass
    if not res or str(res).startswith(('ERR','HTTP')):
        sys.exit(f"Browser fetch failed ({res}). If Cloudflare challenged it, retry with --headed.")
    return [dict(pid=norm(d['pid']),name=d['name'],pub=d['pub'],cost30=d['cost30'],clicks30=int(d['clicks30']),
                 cost7=d['cost7'],rev30=d['rev30'],rev14=d['rev14'],rev7=d['rev7'],roas7=d['roas7'],offconv30=d['offconv30'])
            for d in _json.loads(res)]

def fetch_from_pythago(headless=True):
    res=_fetch_pythago_curl()
    if res is not None:
        print("  (fetched via curl_cffi)"); return res
    print("  curl_cffi blocked/absent -> real-browser fetch (undetected-chromedriver)...")
    return _fetch_pythago_browser(headless=headless)

def load_from_csv(path):
    rows=list(csv.reader(open(path,encoding='utf-8-sig',newline=''))); H=rows[0]
    def ci(n): return H.index(n)
    def f(r,n):
        try: return float(r[ci(n)])
        except (ValueError,IndexError): return 0.0
    out=[]
    for r in rows[1:]:
        out.append(dict(
            pid=norm(r[ci('External ID')]), name=r[ci('Product Name')], pub=str(r[ci('Published At')])[:10],
            cost30=f(r,'Cost (30d) [GBP]'), clicks30=int(f(r,'Clicks (30d)')), cost7=f(r,'Cost (7d) [GBP]'),
            rev30=f(r,'Offline Revenue (30d) [GBP]'), rev14=f(r,'Offline Revenue (14d) [GBP]'),
            rev7=f(r,'Offline Revenue (7d) [GBP]'), roas7=f(r,'ROAS (7d)'), offconv30=f(r,'Offline Quantity (30d)')))
    return out

# ── Shopify ──────────────────────────────────────────────────────────────
def shopify_token():
    return requests.post(f"https://{SHOP}/admin/oauth/access_token",
        data={'grant_type':'client_credentials','client_id':SHOP_CID,'client_secret':SHOP_CSEC},timeout=30).json()['access_token']
def shopify_active_ids(tok):
    Q='query($c:String){products(first:250,after:$c,query:"status:active"){pageInfo{hasNextPage endCursor} edges{node{legacyResourceId}}}}'
    ids=set(); cur=None
    while True:
        j=requests.post(f"https://{SHOP}/admin/api/{SHOP_API}/graphql.json",
            headers={'X-Shopify-Access-Token':tok,'Content-Type':'application/json'},
            json={'query':Q,'variables':{'c':cur}},timeout=60).json()
        c=j['data']['products']
        for e in c['edges']: ids.add(norm(e['node']['legacyResourceId']))
        if c['pageInfo']['hasNextPage']: cur=c['pageInfo']['endCursor']
        else: break
    return ids
def shopify_draft(tok, pid):
    """Set product -> DRAFT, add 'draft_bad_product', and STAMP its publish date as 'pub:YYYY-MM-DD'.
       Shopify wipes publishedAt the moment a product is drafted, so we read it FIRST and preserve it
       in a tag — that keeps the product attributable to its publish batch after the kill.
       All additive; existing tags kept."""
    gid=f"gid://shopify/Product/{pid}"
    # capture publishedAt BEFORE the draft clears it
    pj=requests.post(f"https://{SHOP}/admin/api/{SHOP_API}/graphql.json",
        headers={'X-Shopify-Access-Token':tok,'Content-Type':'application/json'},
        json={'query':'{product(id:"%s"){publishedAt}}'%gid},timeout=30).json()
    pub=((pj.get('data') or {}).get('product') or {}).get('publishedAt')
    tags=['draft_bad_product'] + (['pub:'+str(pub)[:10]] if pub else [])
    M='''mutation($id:ID!,$tags:[String!]!){
      productUpdate(input:{id:$id,status:DRAFT}){product{id status} userErrors{message}}
      tagsAdd(id:$id,tags:$tags){userErrors{message}} }'''
    j=requests.post(f"https://{SHOP}/admin/api/{SHOP_API}/graphql.json",
        headers={'X-Shopify-Access-Token':tok,'Content-Type':'application/json'},
        json={'query':M,'variables':{'id':gid,'tags':tags}},timeout=30).json()
    if j.get('errors'): return str(j['errors'])
    d=j.get('data') or {}
    errs=((d.get('productUpdate') or {}).get('userErrors') or [])+((d.get('tagsAdd') or {}).get('userErrors') or [])
    return 'ok' if not errs else str(errs)

# ── v4 rules ─────────────────────────────────────────────────────────────
def evaluate(p, run_date, is_monday):
    dl=(run_date-datetime.date.fromisoformat(p['pub'])).days
    cost30,cost7,clk = p['cost30'],p['cost7'],p['clicks30']
    rev30,rev14,rev7,roas7,oq = p['rev30'],p['rev14'],p['rev7'],p['roas7'],p['offconv30']
    # v4: gate on revenue VALUE (Shopify), never conversion count
    has_rev  = rev30>0
    recent14 = rev14>0
    zero30   = rev30==0
    zero7    = rev7==0
    cpa      = (cost30/oq) if oq>0 else None
    # ---- proven-product tiers: judged on PERFORMANCE regardless of age ----
    # Tier 6 = ROAS floor. A product that has sold but sits below 2.0 is cut NOW,
    # even at 1 day live (e.g. a re-published product). The <3-day grace does NOT shield it.
    if has_rev and cost7>=5 and roas7<2.0:
        return ('KILL','Tier 6',f'below 2.0 target: ROAS7={roas7:.2f}, £{cost7:.2f}/7d')
    if has_rev and recent14 and zero7 and cpa is not None and cost7>=2*cpa:
        return ('KILL','Tier 5',f'stalled winner: £0 rev 7d, £{cost7:.2f}>=2xCPA(£{cpa:.2f})')
    # ---- NO-SALE testing tiers — SAME-DAY, NO grace period (owner 2026-07-01) ----
    # ONLY change vs the v4 spec: removed the <3-day fresh-import grace AND Tier-2's dl>=4 wait, so a
    # never-sold product is cut the moment it's over £5 (or 40 clicks) with £0 sales — no multi-day
    # bleed. ALL thresholds unchanged (£5 / 40 / 70 clicks). A sale in the last 14d still shields it.
    if not recent14:                              # a sale in 14d shields from Tiers 1-4
        if clk>=70 and zero30:
            return ('KILL','Tier 1',f'{clk} clicks, £0 rev')
        if (clk>=40 or cost30>=5) and zero30:
            return ('KILL','Tier 2',f'no-sale: {clk} clicks/£{cost30:.2f}, £0 rev')
        if is_monday and dl>=7 and cost30>=5 and zero30:
            return ('KILL','Tier 3',f'stale: £{cost30:.2f}, £0 rev')
        if is_monday and dl>=21 and clk<5:
            return ('KILL','Tier 4',f'ghost: {clk} clicks/{dl}d')
    return ('KEEP',None,'no tier triggered')

# ── logging ──────────────────────────────────────────────────────────────
class _Tee:
    """Mirror everything printed to BOTH the console and the run-log file."""
    def __init__(self, *streams): self.streams=streams
    def write(self, s):
        for st in self.streams:
            try: st.write(s); st.flush()
            except Exception: pass
    def flush(self):
        for st in self.streams:
            try: st.flush()
            except Exception: pass

def _write_kills_log(kills, outcomes, run_date, mode, ts):
    """Append one row per flagged/drafted product to KILLS_LOG (the separate kills history)."""
    import csv as _csv, os as _os
    new = not _os.path.exists(KILLS_LOG)
    with open(KILLS_LOG,'a',newline='',encoding='utf-8') as f:
        w=_csv.writer(f)
        if new: w.writerow(['timestamp','data_date','mode','outcome','product_id','name','tier','reason',
                            'days_live','cost_7d','cost_30d','clicks_30d','shop_rev_30d','shop_rev_14d','shop_rev_7d','roas_7d'])
        for p,tier,why in sorted(kills,key=lambda k:k[1]):
            dl=(run_date-datetime.date.fromisoformat(p['pub'])).days
            w.writerow([ts, run_date.isoformat(), mode, outcomes.get(p['pid'],''), p['pid'], p['name'], tier, why, dl,
                        round(p['cost7'],2), round(p['cost30'],2), p['clicks30'],
                        round(p['rev30'],2), round(p['rev14'],2), round(p['rev7'],2), round(p['roas7'],2)])

# ── main ─────────────────────────────────────────────────────────────────
def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--csv', help='read feed from an exported CSV instead of the Pythago API (testing)')
    ap.add_argument('--run-date', help='YYYY-MM-DD (default: today)')
    ap.add_argument('--out', default='kill_list_v4.xlsx')
    ap.add_argument('--headed', action='store_true', help='show the browser if the Pythago fetch needs it')
    a=ap.parse_args()
    run_date=datetime.date.fromisoformat(a.run_date) if a.run_date else datetime.datetime.now(UK).date()
    is_monday=(run_date.weekday()==0)
    ts=datetime.datetime.now(UK).strftime('%Y-%m-%d %H:%M:%S')

    # tee ALL output to the run log (append), so each run is fully recorded
    logf=open(RUN_LOG,'a',encoding='utf-8')
    logf.write(f"\n{'#'*72}\n# RUN {ts}  |  data-date {run_date} ({run_date.strftime('%A')})\n{'#'*72}\n")
    real_stdout=sys.stdout; sys.stdout=_Tee(real_stdout, logf)
    try:
        print(f"== kill_engine v4 == {run_date} ({run_date.strftime('%A')}) | Mon tiers 3&4 {'ON' if is_monday else 'OFF'} | {ts}")
        feed = load_from_csv(a.csv) if a.csv else fetch_from_pythago(headless=not a.headed)
        print(f"feed source: {'CSV '+a.csv if a.csv else 'Pythago API'} -> {len(feed)} active products")

        tok=shopify_token(); live=shopify_active_ids(tok)
        analyzed=[p for p in feed if p['pid'] in live]
        dropped=[p['pid'] for p in feed if p['pid'] not in live]
        print(f"live Shopify active: {len(live)} | analyzed: {len(analyzed)} | dropped (stale/not active): {len(dropped)}")

        kills=[]; tiers={}
        for p in analyzed:
            dec,tier,why=evaluate(p, run_date, is_monday)
            if dec=='KILL':
                kills.append((p,tier,why)); tiers[tier]=tiers.get(tier,0)+1

        # kill-list xlsx
        wb=Workbook(); ws=wb.active; ws.title='kill list v4'
        ws.append(['Product ID','Product Name','Kill Tier','Reason','cost_30','clicks_30','ShopRev_30','ShopRev_14','ShopRev_7','ROAS_7','cost_7'])
        for p,tier,why in sorted(kills,key=lambda k:k[1]):
            ws.append([int(p['pid']),p['name'],tier,why,round(p['cost30'],2),p['clicks30'],
                       round(p['rev30'],2),round(p['rev14'],2),round(p['rev7'],2),round(p['roas7'],2),round(p['cost7'],2)])
        for c in ws['A'][1:]: c.number_format='0'
        wb.save(a.out)

        # detailed kill report: reason + the metrics behind each (review BEFORE any action)
        print(f"\n{'='*72}")
        print(f"PRODUCTS TO KILL: {len(kills)}   (by tier: {tiers or '-'})   |   kept: {len(analyzed)-len(kills)}")
        print('='*72)
        for i,(p,tier,why) in enumerate(sorted(kills,key=lambda k:k[1]),1):
            dl=(run_date-datetime.date.fromisoformat(p['pub'])).days
            print(f"\n{i}. Product ID {p['pid']}   [{tier}]   {p['name'][:52]}")
            print(f"   reason : {why}")
            print(f"   metrics: days_live={dl} | spend 7d/30d=£{p['cost7']:.2f}/£{p['cost30']:.2f} | "
                  f"clicks30={p['clicks30']} | Shopify rev 30d/14d/7d=£{p['rev30']:.2f}/£{p['rev14']:.2f}/£{p['rev7']:.2f} | ROAS7={p['roas7']:.2f}")
        print(f"\nsaved -> {a.out}")

        # ---- ask the user what to do, then act on the answer ----
        outcomes={p['pid']:'FLAGGED' for p,_,_ in kills}
        action='no kills'
        if not kills:
            print("\nNothing to kill today.")
        elif len(kills)>KILL_CAP:
            for k in outcomes: outcomes[k]=f'NOT DRAFTED (over cap {KILL_CAP})'
            action='BLOCKED (over cap)'
            print(f"\n** SAFETY STOP: {len(kills)} kills > cap {KILL_CAP}. Not drafting — investigate first. (flagged list is logged.) **")
        else:
            print(f"\n>>> {len(kills)} product(s) flagged. What do you want to do?")
            print("       yes  =  DRAFT them in Shopify + tag 'draft_bad_product', then log")
            print("       no   =  exit WITHOUT drafting (still logs the flagged list)")
            print("     type yes or no: ", end='', flush=True)
            ans=input().strip().lower()
            print(f"   (you typed: {ans or '<empty>'})")
            if ans in ('y','yes'):
                action='DRAFTED'
                print(f"\nDrafting {len(kills)} product(s) + tagging 'draft_bad_product' ...")
                live2=shopify_active_ids(tok)              # fresh re-check right before writing
                for p,tier,why in kills:
                    if p['pid'] not in live2:
                        outcomes[p['pid']]='SKIPPED (not active)'; print(f"  - {p['pid']}  SKIP (no longer active in Shopify)"); continue
                    res=shopify_draft(tok,p['pid'])
                    outcomes[p['pid']]='DRAFTED' if res=='ok' else f'ERROR: {res}'
                    print(f"  - {p['pid']}  {'DRAFTED + tagged draft_bad_product' if res=='ok' else 'ERROR: '+res}")
                print(f"\nDone: {sum(1 for v in outcomes.values() if v=='DRAFTED')}/{len(kills)} drafted & tagged 'draft_bad_product'.")
            else:
                action='EXITED (no draft)'
                for k in outcomes: outcomes[k]='NOT DRAFTED (you exited)'
                print("\nExiting without drafting. The flagged list has been logged.")

        # separate kills history: one row per flagged/drafted product
        if kills: _write_kills_log(kills, outcomes, run_date, action, ts)
        print(f"\nlogs: run -> {RUN_LOG}" + (f"   |   kills -> {KILLS_LOG} (+{len(kills)} rows)" if kills else ""))
    finally:
        sys.stdout=real_stdout
        logf.flush(); logf.close()

if __name__=='__main__':
    main()
