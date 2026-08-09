# -*- coding: utf-8 -*-
"""ONE-SHOT campaign structure edits (owner 2026-08-09, scheduled 00:10 UK Aug-10):

    Testing|UK (summer)  24027270949   budget -> £100/day
    Testing|AW           24116871559   budget -> £60/day
    Winners              23620737018   tROAS  -> 2.2 (220%), budget -> £40/day

Idempotent — values already at target are skipped, so any number of re-fires is
harmless. DATE-GUARDED: outside the Aug 10-12 2026 UTC window it exits without
touching anything (protects against a stray scheduled re-fire months later);
--force overrides the guard. Verifies by re-reading every value after the mutate."""
import sys, time, argparse, datetime, requests
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
import google_ads_connect as ga

ap = argparse.ArgumentParser()
ap.add_argument('--apply', action='store_true')
ap.add_argument('--force', action='store_true', help='skip the Aug 10-12 2026 date guard')
A = ap.parse_args()

today = datetime.datetime.now(datetime.timezone.utc).date()
if not A.force and not (datetime.date(2026, 8, 9) <= today <= datetime.date(2026, 8, 12)):
    print(f'date guard: {today} outside 2026-08-09..12 window — nothing done'); sys.exit(0)

CID = ga.CUSTOMER_ID
TOK = ga.get_access_token()
H = {'Authorization': f'Bearer {TOK}', 'developer-token': ga.DEVELOPER_TOKEN,
     'Content-Type': 'application/json'}
if ga.LOGIN_CUSTOMER_ID:
    H['login-customer-id'] = ga.LOGIN_CUSTOMER_ID

TARGETS = {
    '24027270949': {'name': 'Testing|UK summer', 'budget': 100.0},
    '24116871559': {'name': 'Testing|AW',        'budget': 60.0},
    '23620737018': {'name': 'Winners',           'budget': 40.0, 'troas': 2.2},
}

def _req(path, body):
    """POST with retry — Google Ads REST throws transient 400/5xx (~1-in-3 on this account)."""
    for a in range(5):
        r = requests.post(f'{ga.ADS_BASE}/customers/{CID}/{path}', headers=H, json=body, timeout=90)
        if r.status_code == 200:
            return r.json()
        if a < 4 and (r.status_code >= 500 or r.status_code in (400, 429)):
            time.sleep(3 * (a + 1)); continue
        raise RuntimeError(f'{path} {r.status_code}: {r.text[:400]}')
    raise RuntimeError(f'{path}: retries exhausted')

def read_state():
    q = ('SELECT campaign.id, campaign.name, campaign.campaign_budget, '
         'campaign.maximize_conversion_value.target_roas, '
         'campaign_budget.resource_name, campaign_budget.amount_micros, '
         'campaign_budget.explicitly_shared '
         'FROM campaign WHERE campaign.id IN (24027270949, 24116871559, 23620737018)')
    rows = _req('googleAds:search', {'query': q}).get('results', [])
    st = {}
    for r in rows:
        c, b = r['campaign'], r['campaignBudget']
        st[str(c['id'])] = dict(
            name=c.get('name', ''), budget=int(b['amountMicros']) / 1e6,
            budget_rn=b['resourceName'], shared=bool(b.get('explicitlyShared')),
            troas=(c.get('maximizeConversionValue') or {}).get('targetRoas'))
    return st

st = read_state()
if len(st) != 3:
    print(f'!! expected 3 campaigns, got {len(st)}: {list(st)} — aborting'); sys.exit(1)

budget_ops, camp_ops = [], []
for cid, t in TARGETS.items():
    s = st[cid]
    line = f"{t['name']:18} budget £{s['budget']:g} -> £{t['budget']:g}"
    if s['shared']:
        print(f'!! {t["name"]}: budget is SHARED across campaigns — refusing to touch it'); sys.exit(1)
    if abs(s['budget'] - t['budget']) > 0.005:
        budget_ops.append({'updateMask': 'amountMicros',
                           'update': {'resourceName': s['budget_rn'],
                                      'amountMicros': str(int(round(t['budget'] * 1_000_000)))}})
    else:
        line += '  (already set)'
    if 'troas' in t:
        line += f" | tROAS {s['troas']} -> {t['troas']}"
        if s['troas'] != t['troas']:
            camp_ops.append({'updateMask': 'maximizeConversionValue.targetRoas',
                             'update': {'resourceName': f'customers/{CID}/campaigns/{cid}',
                                        'maximizeConversionValue': {'targetRoas': t['troas']}}})
        else:
            line += '  (already set)'
    print(line)

if not A.apply:
    print(f'\nPREVIEW | {len(budget_ops)} budget ops, {len(camp_ops)} campaign ops pending (--apply to run)')
    sys.exit(0)
if budget_ops:
    _req('campaignBudgets:mutate', {'operations': budget_ops})
if camp_ops:
    _req('campaigns:mutate', {'operations': camp_ops})
print(f'\nAPPLIED {len(budget_ops)} budget ops + {len(camp_ops)} campaign ops — verifying…')

ver = read_state()
ok = True
for cid, t in TARGETS.items():
    v = ver[cid]
    good = abs(v['budget'] - t['budget']) < 0.005 and (t.get('troas') is None or v['troas'] == t['troas'])
    ok &= good
    print(f"  {'OK ' if good else 'BAD'} {t['name']:18} budget £{v['budget']:g}"
          + (f" | tROAS {v['troas']}" if 'troas' in t else ''))
print('VERIFIED — all values live' if ok else '!! MISMATCH — check the account')
sys.exit(0 if ok else 1)
