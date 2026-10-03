#!/usr/bin/env python3
# kill_engine_google_auto.py
# ---------------------------------------------------------------------------
# UNATTENDED Google-direct auto-kill — for running on a schedule (e.g. Claude
# schedule / GitHub repo, hourly). Runs once when CALLED, no user input:
#   1. Pull the live feed (Google Ads cost/clicks + Shopify revenue, UK tz).
#   2. Tag new winners (any Shopify sale) + fast-path them into the Winners campaign.
#   3. WINNER PACE RULE (v11): kill winners whose Winners-campaign spend since
#      their last sale exceeds max(sale rev, price) / 2.0 (see block below).
#   4. Apply the SAME v4 rules (evaluate) to the TESTING pool (winners exempt).
#   5. DRAFT every flagged product in Shopify (no yes/no prompt).
#   6. NOTIFY:
#        - TELEGRAM every run  -> run stats + the .xlsx (instant push, no daily cap)
#        - RESEND email twice/day (SUMMARY_HOURS) -> a TEXT digest of the last 12h kills + the .xlsx
#
# Separate from kill_engine_google.py (which asks for confirmation) — it changes
# nothing in the other files; it only REUSES their tested functions.
#
# Usage:
#   python kill_engine_google_auto.py          # live: drafts the kills + notifies
#   python kill_engine_google_auto.py --dry     # safe test: computes + notifies, DRAFTS NOTHING
#   python kill_engine_google_auto.py --test    # like the run but also FORCES the 12h email now (testing)
#
# Setup (env vars, or the git-ignored _secrets_local.py):
#   TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID  (from @BotFather; drives the every-run push)
#   RESEND_API_KEY  (free key from resend.com; twice-daily digest; signup inbox == EMAIL_TO)
# Any unset channel is simply skipped (the run still drafts + logs).
# ---------------------------------------------------------------------------
import sys, os, csv, base64, html, datetime, collections, requests
from openpyxl import Workbook
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
from zoneinfo import ZoneInfo

# reuse the EXACT feed + rules + Shopify write + logging from the existing engines
from kill_engine_google import build_feed, PMAX_CAMPAIGN_ID as WINNERS_CAMPAIGN_ID
from kill_engine_v4 import evaluate, shopify_token, shopify_draft, _Tee, SHOP, SHOP_API
from creds import cred                          # env -> _secrets_local.py -> '' (so local testing works too)

UK = ZoneInfo('Europe/London')                 # store + Google Ads account run on UK time

# ---- notifications ----
EMAIL_TO       = cred('EMAIL_TO')               # digest inbox — from Secrets/_secrets_local, never in public code
RESEND_API_KEY = cred('RESEND_API_KEY')
RESEND_FROM    = cred('RESEND_FROM') or 'onboarding@resend.dev'
TELEGRAM_TOKEN = cred('TELEGRAM_BOT_TOKEN')     # from @BotFather — every-run push
TELEGRAM_CHAT  = cred('TELEGRAM_CHAT_ID')       # your numeric chat id (@userinfobot)
SUMMARY_HOURS  = (9, 21)                        # UK hours for the twice-a-day (every 12h) email digest
RUN_LOG        = 'kill_engine_auto_runs.log'
KILLS_LOG      = 'kills_log_auto.csv'

# ── Telegram kill formatting: plain-English tier + the rule's reason + core metrics ──
TIER_LABEL = {
    'Tier 1': 'Tier 1 — no sale (70+ clicks)', 'Tier 2': 'Tier 2 — no sale (£5+ or 40+ clicks)',
    'Tier 3': 'Tier 3 — Mon stale no-sale',    'Tier 4': 'Tier 4 — Mon ghost (<5 clicks)',
    'Tier 5': 'Tier 5 — stalled winner',       'Tier 6': 'Tier 6 — below 2.0 ROAS (7d)',
    'Tier 7': 'Tier 7 — slow dribbler (30d)'}
def _fmt_kill(p, tier, why, run_date):
    dl = (run_date - datetime.date.fromisoformat(p['pub'])).days
    return (f"• <b>{html.escape(p['name'][:46])}</b>\n"
            f"  <code>{p['pid']}</code> · <b>{TIER_LABEL.get(tier, tier)}</b> · {dl}d live\n"
            f"  ↳ why: {html.escape(str(why))}\n"
            f"  ↳ spend 7d £{p['cost7']:.2f} / 30d £{p['cost30']:.2f} · ROAS7 {p['roas7']:.2f} · "
            f"rev 30d £{p['rev30']:.2f} · {p['clicks30']} clk")

def _days_live(p, run_date):
    return (run_date - datetime.date.fromisoformat(p['pub'])).days

# ── WINNERS (w_campaign): tag + exempt (owner-approved 2026-07-11) ──────────
# A product with >=1 Shopify sale (GROUND TRUTH — never depends on Google's
# under-reporting) that is still ACTIVE is a "winner". It gets the w_campaign
# tag, which does two things:
#   (a) Simprosys rule maps tag -> custom_label_1=w_campaign -> the product hops
#       from the Testing PMax campaign to the Winners campaign on the next
#       feed sync (near-real-time, tag changes fire Shopify webhooks);
#   (b) EXEMPTS it from this engine's kill rules — winners will get their OWN
#       rules later; until then NO product with a sale is ever drafted here.
#       Only never-sold products (Tiers 1-4 territory) keep dying.
# First sales always appear in rev30 (live Shopify orders pull) within one
# 8-min run. Sales older than 30d were tagged by the one-off backfill
# (backfill_winner_tags.py, run 2026-07-11) — so rev30>0 is a complete signal
# for every NEW first sale going forward.
WINNER_TAG = 'w_campaign'
LC_TAG = 'lc_campaign'          # LAST CHANCE (owner 2026-08-13): pace-killed winners stay ACTIVE
LC_CAMPAIGN_ID = '24127184079'  # 'PMax | Last Chance | UK' — £20/day, tROAS 2.2, UK presence-only
LC_KILL_CAP = 15                # glitch guard for the lc exit rule
LC_LOOKBACK_D = 200             # ladder v7: LC spend window (no time rule, so a product can stay for months)
LOST_TAG   = 'l_camp'      # "lost" — WAS a winner, killed by the pace rule (v11). Set on winner
                           # kills; stripped again if the product is ever resurrected and re-sells.

def _shopify_read(tok, query, variables, timeout=60):
    """One Shopify Admin GraphQL READ with retries (ladder v7.2, 2026-10-02): a network error, HTTP 429/5xx, a non-JSON
    reply or a reply without 'data' (e.g. THROTTLED) is retried twice (2 s, 4 s) before raising - a single transient
    reset used to skip a whole judging section for the run. Returns the parsed JSON reply."""
    import time as _time
    for attempt in range(3):                  # v7.3: 3 tries, at most 45 s each, 2 s / 4 s pauses
        if attempt:
            _time.sleep(2 * attempt)
        try:
            resp = requests.post(f"https://{SHOP}/admin/api/{SHOP_API}/graphql.json",
                                 headers={'X-Shopify-Access-Token': tok, 'Content-Type': 'application/json'},
                                 json={'query': query, 'variables': variables}, timeout=min(timeout, 45))
            if resp.status_code == 429 or resp.status_code >= 500:
                raise RuntimeError(f'Shopify HTTP {resp.status_code}')
            j = resp.json()
            if j.get('data') is None:
                raise RuntimeError(f"Shopify reply without data: {str(j.get('errors'))[:120]}")
            return j
        except Exception:
            if attempt == 2:
                raise

def _product_tags(tok, pid):
    """A product's current tags, read straight from Shopify (v7.4): decides whether a write that 'failed' (timeouts / 5xx
    on every try) actually landed, BEFORE anything is rolled back. None when the read itself fails - then nothing is rolled
    back and the next run repairs the move (every write here can be repeated safely)."""
    import time as _time
    _time.sleep(3)                     # a write that timed out may still be landing - give Shopify a moment first
    try:
        j = _shopify_read(tok, '{product(id:"gid://shopify/Product/%s"){tags}}' % pid, {}, timeout=20)
        return {str(t) for t in (((j.get('data') or {}).get('product') or {}).get('tags') or [])}
    except Exception:
        return None

def _shopify_write(tok, query, variables, field):
    """Run one Shopify Admin GraphQL mutation. Returns 'ok' only when the reply carries `field` and no userErrors.
    Ladder v7.1 (2026-10-02): a THROTTLED reply has no 'data' and used to read as 'ok' (nothing written, engine moved
    on). Now THROTTLED / HTTP 429 / 5xx / network errors are retried with backoff; anything else is reported as 'err'."""
    import time as _time
    last = 'err: no reply'
    for attempt in range(3):                  # v7.3: 3 tries, 20 s each, 1 s / 2 s pauses, none after the last try
        if attempt:
            _time.sleep(attempt)
        try:
            resp = requests.post(f"https://{SHOP}/admin/api/{SHOP_API}/graphql.json",
                                 headers={'X-Shopify-Access-Token': tok, 'Content-Type': 'application/json'},
                                 json={'query': query, 'variables': variables}, timeout=20)
            if resp.status_code == 429 or resp.status_code >= 500:
                last = f'err: http {resp.status_code}'
            else:
                j = resp.json()
                errs = j.get('errors') or []
                node = (j.get('data') or {}).get(field)
                if node is not None and not errs:
                    ue = node.get('userErrors') or []
                    return 'ok' if not ue else f"err: {ue[0].get('message', '?')[:60]}"
                throttled = isinstance(errs, list) and any(
                    ((e or {}).get('extensions') or {}).get('code') == 'THROTTLED' for e in errs if isinstance(e, dict))
                if not throttled:
                    return f"err: {str(errs)[:80] if errs else 'no data in reply'}"
                last = 'err: throttled'
        except Exception as ex:
            last = f"err: {str(ex)[:80]}"
    return last

def shopify_add_tag(tok, pid, tag):
    m = 'mutation($id:ID!,$t:[String!]!){tagsAdd(id:$id,tags:$t){userErrors{message}}}'
    return _shopify_write(tok, m, {'id': f"gid://shopify/Product/{pid}", 't': [tag]}, 'tagsAdd')

def shopify_remove_tag(tok, pid, tag):
    m = 'mutation($id:ID!,$t:[String!]!){tagsRemove(id:$id,tags:$t){userErrors{message}}}'
    return _shopify_write(tok, m, {'id': f"gid://shopify/Product/{pid}", 't': [tag]}, 'tagsRemove')

def shopify_set_label_metafield(tok, pid, value=WINNER_TAG):
    """Also write Simprosys's own attribute metafield (mm-google-shopping.custom_label_1).
    The app's bulk-edit stores labels app-side, but it READS this metafield on its syncs —
    so future winners pick up the feed label without a human touching the app. The Ads-API
    item-ID mover (added after the campaigns exist) is the guaranteed instant path.
    value: WINNER_TAG (winners) or CHAMPION_TAG (champions tier, 2026-07-20)."""
    m = ('mutation($mf:[MetafieldsSetInput!]!){metafieldsSet(metafields:$mf){userErrors{message}}}')
    v = {'mf': [{'ownerId': f"gid://shopify/Product/{pid}", 'namespace': 'mm-google-shopping',
                 'key': 'custom_label_1', 'type': 'single_line_text_field', 'value': value}]}
    return _shopify_write(tok, m, v, 'metafieldsSet')

WINNER_ENTRY_ORDERS = 1   # owner 2026-08-24: 2 -> 1 — first sale graduates to Winners (tROAS raised to 2.2 as the guard; pace-kill's 1-sale branch is the stop-loss).
                          # Safe because tROAS 2.2 is the real quality filter: a 2-sale
                          # entrant that cannot clear 2.2 simply never serves. The pace
                          # rule bounds the downside at max(last-2-rev, price)/2.0.

def tag_new_winners(feed, dry, life=None):
    """Tag ACTIVE products that reached WINNER_ENTRY_ORDERS lifetime orders.
    rev30>0 is only the cheap pre-filter; the real gate is the full lifetime
    order count (owner 2026-08-20: gate is 2 — a single sale stays in Testing)."""
    cand = [p for p in feed if p['rev30'] > 0 and WINNER_TAG not in p['tags']
            and LC_TAG not in p['tags']]   # lc graduation is lc_run's job (post-stamp sales only)
    if not cand:
        return []
    if life is None:
        life, _n = _lifetime_sales(shopify_token())
    new = [p for p in cand if len(life.get(str(p['pid']), [])) >= WINNER_ENTRY_ORDERS]
    below = len(cand) - len(new)
    if below:
        print(f"  entry gate: {below} seller(s) under {WINNER_ENTRY_ORDERS} lifetime orders stay in Testing")
    if not new:
        return []
    tok = None if dry else shopify_token()
    for p in new:
        res = 'DRY (not tagged)' if dry else shopify_add_tag(tok, p['pid'], WINNER_TAG)
        mres = 'DRY' if dry else shopify_set_label_metafield(tok, p['pid'])
        if LOST_TAG in p['tags'] and not dry:
            shopify_remove_tag(tok, p['pid'], LOST_TAG)   # resurrected + sold again: no longer "lost"
        print(f"  {'would tag' if dry else 'tag'} winner {p['pid']} -> {res} (label metafield: {mres}) | {p['name'][:42]}")
        if not dry and len(life.get(str(p['pid']), [])) >= 2:
            _add_to_best_sellers(tok, p['pid'])   # owner 2026-08-24: Best Sellers stays gated at 2+ sales (promotion != merchandising)
        p['tags'].append(WINNER_TAG)     # exempt from kill rules in THIS same run too
    if not dry and new:
        resort_best_sellers(tok, life)   # keep AW-first-by-sales order after adds
    return new


BEST_SELLERS_COLLECTION = 'gid://shopify/Collection/690375426428'   # manual 'best-sellers' (AW best sellers)


def _add_to_best_sellers(tok, pid):
    """Add a freshly-promoted winner to the Best Sellers manual collection.
    WARN-only: a failure here must never break the promotion run."""
    try:
        q = ('mutation($id:ID!,$p:[ID!]!){collectionAddProductsV2(id:$id,productIds:$p)'
             '{userErrors{field message}}}')
        r = requests.post(f"https://{SHOP}/admin/api/{SHOP_API}/graphql.json",
                          headers={'X-Shopify-Access-Token': tok, 'Content-Type': 'application/json'},
                          json={'query': q, 'variables': {'id': BEST_SELLERS_COLLECTION,
                                'p': [f"gid://shopify/Product/{pid}"]}}, timeout=30).json()
        errs = (r.get('data') or {}).get('collectionAddProductsV2', {}).get('userErrors')
        print(f"    best-sellers add: {'WARN ' + str(errs)[:60] if errs else 'ok'}")
    except Exception as e:                       # noqa: BLE001
        print(f"    best-sellers add WARN: {type(e).__name__}: {str(e)[:60]}")


def resort_best_sellers(tok, life=None):
    """Owner 2026-08-24: Best Sellers order = AW items by sales first, then summer
    by sales. MANUAL sort; re-applied nightly after promotions. WARN-only."""
    try:
        import datetime as _dt
        if life is None:
            life, _ = _lifetime_sales(tok)
        H = {'X-Shopify-Access-Token': tok, 'Content-Type': 'application/json'}
        def gql(q, v):
            return requests.post(f"https://{SHOP}/admin/api/{SHOP_API}/graphql.json",
                                 headers=H, json={'query': q, 'variables': v}, timeout=90).json()
        cur = None; mem = []
        Q = ('query($id:ID!,$c:String){collection(id:$id){products(first:250,after:$c)'
             '{pageInfo{hasNextPage endCursor} edges{node{id legacyResourceId tags '
             'l2:metafield(namespace:"mm-google-shopping",key:"custom_label_2"){value}}}}}}')
        while True:
            d = gql(Q, {'id': BEST_SELLERS_COLLECTION, 'c': cur})['data']['collection']['products']
            for e in d['edges']:
                n = e['node']
                aw = (n['l2'] or {}).get('value') in ('aw26', 'aw26b') or 'aw26' in {t.lower() for t in (n['tags'] or [])}   # aw26b = Testing|AW-B (2026-09-08)
                mem.append((n['id'], n['legacyResourceId'], aw))
            if d['pageInfo']['hasNextPage']: cur = d['pageInfo']['endCursor']
            else: break
        cut = (_dt.date.today() - _dt.timedelta(days=30)).isoformat()
        def rank(pid):
            s = life.get(pid, [])
            return (sum(1 for x in s if x['date'] >= cut), len(s), sum(x['rev'] for x in s))
        aw = sorted([m for m in mem if m[2]], key=lambda m: rank(m[1]), reverse=True)
        sm = sorted([m for m in mem if not m[2]], key=lambda m: rank(m[1]), reverse=True)
        moves = [{'id': g, 'newPosition': str(i)} for i, (g, _pid, _a) in enumerate(aw + sm)]
        MV = ('mutation($id:ID!,$moves:[MoveInput!]!){collectionReorderProducts(id:$id,moves:$moves)'
              '{job{id} userErrors{field message}}}')
        for i in range(0, len(moves), 200):
            gql(MV, {'id': BEST_SELLERS_COLLECTION, 'moves': moves[i:i+200]})
        print(f"  best-sellers resorted: {len(aw)} AW first, {len(sm)} summer after")
    except Exception as e:                       # noqa: BLE001
        print(f"  best-sellers resort WARN: {type(e).__name__}: {str(e)[:60]}")

# ── ADS FAST-PATH: move new winners between the two PMax campaigns INSTANTLY ─
# The Testing/Winners split (2026-07-11) filters on custom_label_1=w_campaign,
# but that label rides Simprosys's feed sync (minutes-to-hours). This path skips
# the wait: the moment a product wins, its variant item-ids are written straight
# into the campaigns' listing trees via the Ads API —
#   Winners  (asset group 6684080392): item-ids UNIT_INCLUDED  (serves NOW)
#   Testing  (asset group 6729681029): item-ids UNIT_EXCLUDED  (stops NOW)
# Both trees' "everything else" branches were pre-converted to item-id
# subdivisions on 2026-07-11, so this is a plain node-add. Idempotent (checks
# existing nodes first). If it ever fails, the label path still moves the
# product on the next feed sync — so failures WARN, never break the kill run.
WINNERS_AG_ID = '6684080392'
TESTING_AG_ID = '6729681029'
AW_TESTING_AG_ID = '6738045970'   # Testing|AW: aw26 subdivision added 2026-08-16 (node 15260771050)

def _ads_search(ga, gt, query):
    """GoogleAdsService.search, every page. Ladder v7.1: a network error, a non-JSON reply or HTTP 429/5xx is retried twice
    (3 s, 6 s) - a network error / timeout / non-JSON reply only once since v7.3 - instead of skipping a whole section for
    the run; a Google API error still raises."""
    import time as _time
    out = []; tok = None
    while True:
        body = {'query': query}
        if tok: body['pageToken'] = tok
        r = None
        for attempt in range(3):
            try:
                resp = requests.post(f"{ga.ADS_BASE}/customers/{ga.CUSTOMER_ID}/googleAds:search",
                                     headers=ga._headers(gt), json=body, timeout=60)
                if resp.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                    _time.sleep(3 * (attempt + 1)); continue
                if resp.status_code == 400 and attempt == 0 and not any(k in (resp.text or '') for k in (
                        'queryError', 'fieldError', 'requestError', 'authenticationError', 'authorizationError')):
                    print(f"  (Google HTTP 400 - retrying once: {(resp.text or '')[:200]})")   # v7.4: the documented flake
                    _time.sleep(4); continue
                r = resp.json()
                break
            except Exception:
                if attempt >= 1: raise              # v7.3: one retry on a network error / timeout (keeps runs short)
                _time.sleep(3)
        if 'error' in r: raise RuntimeError(str(r)[:300])
        out += r.get('results', []); tok = r.get('nextPageToken')
        if not tok: return out

def _variant_item_ids(stok, pid):
    q = 'query($id:ID!){product(id:$id){variants(first:100){edges{node{legacyResourceId}}}}}'
    j = requests.post(f"https://{SHOP}/admin/api/{SHOP_API}/graphql.json",
                      headers={'X-Shopify-Access-Token': stok, 'Content-Type': 'application/json'},
                      json={'query': q, 'variables': {'id': f"gid://shopify/Product/{pid}"}}, timeout=30).json()
    vs = [e['node']['legacyResourceId'] for e in j['data']['product']['variants']['edges']]
    return [f"shopify_zz_{pid}_{v}".lower() for v in vs]

def ads_fast_path(new_winners, stok):
    try:
        import google_ads_connect as ga
        gt = ga.get_access_token()
        rows = _ads_search(ga, gt,
            "SELECT asset_group.id, asset_group_listing_group_filter.resource_name, "
            "asset_group_listing_group_filter.type, asset_group_listing_group_filter.parent_listing_group_filter, "
            "asset_group_listing_group_filter.case_value.product_custom_attribute.index, "
            "asset_group_listing_group_filter.case_value.product_custom_attribute.value, "
            "asset_group_listing_group_filter.case_value.product_item_id.value "
            "FROM asset_group_listing_group_filter WHERE asset_group.id IN (6684080392,6729681029,6738045970)")
        info = {}
        for agid in (WINNERS_AG_ID, TESTING_AG_ID, AW_TESTING_AG_ID):
            ag = [x for x in rows if str(x['assetGroup']['id']) == agid]
            subdiv = None; have = set()
            for x in ag:
                # item-id subdivision: in Winners/Testing it is the SUBDIVISION whose
                # case is attr1-with-no-value; in Testing-AW it is the SUBDIVISION
                # whose case is attr2='aw26' (tree converted 2026-08-16).
                f = x['assetGroupListingGroupFilter']; cv = f.get('caseValue', {})
                pca = cv.get('productCustomAttribute')
                if f['type'] != 'SUBDIVISION' or pca is None:
                    continue
                if agid == AW_TESTING_AG_ID:
                    if pca.get('value') == 'aw26':
                        subdiv = f['resourceName']
                elif 'value' not in pca:
                    subdiv = f['resourceName']
            for x in ag:
                f = x['assetGroupListingGroupFilter']; cv = f.get('caseValue', {})
                if f.get('parentListingGroupFilter') == subdiv and cv.get('productItemId', {}).get('value'):
                    have.add(cv['productItemId']['value'].lower())
            if not subdiv: raise RuntimeError(f"item-id subdivision not found in AG {agid}")
            info[agid] = (subdiv, have, len(ag))          # len(ag) = nodes already in this tree (Google caps at 1,000)
        global _TREE_NODES_SEEN                            # cheapest possible trigger for the prune: we just read the trees
        _TREE_NODES_SEEN = max(v[2] for v in info.values())
        made = 0
        for agid, node_type in ((WINNERS_AG_ID, 'UNIT_INCLUDED'), (TESTING_AG_ID, 'UNIT_EXCLUDED'),
                                (AW_TESTING_AG_ID, 'UNIT_EXCLUDED')):
            subdiv, have, n_nodes = info[agid]
            ops = []
            for p in new_winners:
                for iid in _variant_item_ids(stok, p['pid']):
                    if iid not in have:
                        ops.append({"create": {"assetGroup": f"customers/{ga.CUSTOMER_ID}/assetGroups/{agid}",
                                               "type": node_type, "listingSource": "SHOPPING",
                                               "parentListingGroupFilter": subdiv,
                                               "caseValue": {"productItemId": {"value": iid}}}})
            if ops and n_nodes + len(ops) > FAST_PATH_CAP:   # near Google's 1,000-node cap: skip this tree, the label path moves it
                print(f"  fast-path: AG {agid} has {n_nodes} nodes, +{len(ops)} would pass the {FAST_PATH_CAP} cap - skipped (label path covers it; nightly prune frees space)")
                continue
            if ops:
                r = requests.post(f"{ga.ADS_BASE}/customers/{ga.CUSTOMER_ID}/assetGroupListingGroupFilters:mutate",
                                  headers=ga._headers(gt), json={"operations": ops}, timeout=60).json()
                if 'error' in r: raise RuntimeError(str(r)[:300])
                made += len(ops)
        print(f"  fast-path: {made} item-id nodes written (Winners include / Testing+AW exclude)")
        return made, None
    except Exception as ex:
        print(f"  !! fast-path FAILED (harmless - label path still moves it on next feed sync): {ex}")
        return 0, str(ex)[:150]

# ── BEST SELLERS COLLECTION AUTO-ADD (owner request 2026-07-13) ─────────────
# Every ACTIVE winner (w_campaign) is ensured a member of the manual "Best
# Sellers" collection (handle best-sellers, shown on the storefront/product
# pages). ADD-ONLY by design: a winner later drafted/killed STAYS a member
# (owner call — drafts don't render on the storefront anyway, and a
# reactivated product is already in place). Collection sortOrder is
# BEST_SELLING, so Shopify auto-ranks the page by real sales. Idempotent
# every 8-min run; failures WARN, never break the run.
BESTSELLER_COLLECTION_ID = '690375426428'    # "Best Sellers" (manual collection)

def sync_bestseller_collection(tok, dry):
    """Add any active winner missing from the Best Sellers collection. Returns (added, err)."""
    try:
        cgid = f"gid://shopify/Collection/{BESTSELLER_COLLECTION_ID}"
        Q = ('query($id:ID!,$c:String){collection(id:$id){products(first:250,after:$c){'
             'pageInfo{hasNextPage endCursor} edges{node{legacyResourceId}}}}}')
        have = set(); cur = None
        while True:
            j = requests.post(f"https://{SHOP}/admin/api/{SHOP_API}/graphql.json",
                              headers={'X-Shopify-Access-Token': tok, 'Content-Type': 'application/json'},
                              json={'query': Q, 'variables': {'id': cgid, 'c': cur}}, timeout=60).json()
            co = (j.get('data') or {}).get('collection')
            if co is None: return 0, 'Best Sellers collection not found'
            pp = co['products']
            for e in pp['edges']: have.add(str(e['node']['legacyResourceId']))
            if not pp['pageInfo']['hasNextPage']: break
            cur = pp['pageInfo']['endCursor']
        missing = [p for p in _winner_products(tok) if p not in have]
        if not missing: return 0, None
        if dry:
            print(f"  would add {len(missing)} winner(s) to Best Sellers collection (DRY)")
            return 0, None
        M = ('mutation($id:ID!,$pids:[ID!]!){collectionAddProductsV2(id:$id,productIds:$pids){'
             'userErrors{field message}}}')
        added = 0
        for i in range(0, len(missing), 250):
            batch = [f"gid://shopify/Product/{p}" for p in missing[i:i + 250]]
            j = requests.post(f"https://{SHOP}/admin/api/{SHOP_API}/graphql.json",
                              headers={'X-Shopify-Access-Token': tok, 'Content-Type': 'application/json'},
                              json={'query': M, 'variables': {'id': cgid, 'pids': batch}}, timeout=60).json()
            errs = ((j.get('data') or {}).get('collectionAddProductsV2') or {}).get('userErrors') or []
            if errs: return added, f"collection add: {str(errs)[:100]}"
            added += len(batch)
        print(f"  Best Sellers collection: +{added} winner(s) added")
        return added, None
    except Exception as ex:
        return 0, f'Best Sellers sync error: {str(ex)[:120]}'

# ── WINNER PACE RULE (v12 — owner 2026-08-16: two-sale window at ROAS 2.0) ──
# The Winners campaign has ONE kill rule:
#
#     KILL when Winners-campaign spend SINCE THE 3rd-LAST SALE
#          > max(revenue of the last TWO sales, product price) / 2.0
#     (2 lifetime sales: spend since the older one vs their summed revenue;
#      1 sale: spend since it vs max(its revenue, price); none: price, whole window)
#     price floor kept (owner 2026-08-14): a discounted bundle sale must not
#     shorten a winner's runway below what its normal price would grant.
#
# Why 2.8 (owner 2026-08-10, raised from 2.3 alongside Winners tROAS 2.0 -> 2.2):
# a kill line at 2.8-pace makes ~2.9 the survival standard, holding the Winners
# campaign at the owner's TRUE-2.5+ doctrine (pixel 2.2 ≈ TRUE ~2.75). Every sale
# opens a fresh cycle and prepays EXACTLY its own revenue/2.8 of runway. Allowances
# never stack — past glory never pays for the present.
#
# Owner-approved design decisions (2026-07-12 session):
#   • Sales truth = Shopify, ANY channel (ads / organic / social / cross-sell).
#     Google conversions & attribution are NEVER consulted. Revenue is GROSS
#     with the same order-level discount scaling as the feed; refunds ignored.
#   • Spend = Winners campaign ONLY, per-product per-DAY (Google's finest grain).
#     The sale's own day is credited to the product — its new cycle is charged
#     from the NEXT day. Kills can only fire late (~1 day of that product's
#     spend), never early.
#   • ANCHORED cycles: judged from the REAL last sale — "if it's a real winner
#     it will hold". No fresh start at activation, no grace, no shields.
#   • Kill = DRAFT + tags draft_bad_product + l_camp + pub: stamp, and the
#     w_campaign tag is REMOVED. PERMANENT — no demotion back to Testing (a
#     demoted ex-winner's conversion history would out-compete cold imports for
#     Testing's Max-Conversions budget). Manual reactivation stays possible;
#     if it then sells, tag_new_winners re-promotes it and strips l_camp.
#   • Zero-spend products can never die (no spend, no crime): cross-sell and
#     organic sellers are immortal and only lift the blended.
#   • DORMANT until WINNER_KILL_START — before that date every run computes and
#     reports would-kills (Telegram preview) but drafts nothing.
#   • FAIL-SAFE: any error in this section skips winner kills for the run and
#     warns — it can never block or distort the testing kills. A glitchy run
#     that flags more than WINNER_KILL_CAP winners aborts (data glitch guard,
#     same philosophy as the testing KILL_CAP).
WINNER_PACE_ROAS  = 2.0                            # 2.4 -> 2.0 owner 2026-08-14: max spend absorption; survival ~2.1 sits under the tROAS-2.2 gate, so the bidder is the real control and this is a deep backstop; LC catches exits
WINNER_KILL_START = datetime.date(2026, 7, 13)     # LIVE (owner 2026-07-13: "if a winner hits the rules kill it, don't wait for Jul 25")
WINNER_LOOKBACK_D = 60                             # last-sale + spend lookback window
WINNER_KILL_CAP   = 10                             # >N winner kills in one run = glitch -> abort + alert
WINNER_POOL_ALERT = 25                             # warn when the winner pool drops below this
WINNER_KILLS_LOG  = 'winner_kills_log.csv'

def _winner_products(tok):
    """Live winners (ACTIVE + w_campaign) -> {pid: {name, price}} (min variant price)."""
    Q = ('query($c:String){products(first:250,after:$c,query:"tag:w_campaign status:active"){'
         'pageInfo{hasNextPage endCursor} edges{node{legacyResourceId title tags '
         'priceRangeV2{minVariantPrice{amount}}}}}}')
    out = {}; cur = None
    while True:
        j = _shopify_read(tok, Q, {'c': cur}, timeout=60)   # v7.2: retried read
        c = j['data']['products']
        for e in c['edges']:
            n = e['node']
            out[str(n['legacyResourceId'])] = dict(
                name=n.get('title', ''),
                price=float((n.get('priceRangeV2') or {}).get('minVariantPrice', {}).get('amount') or 0),
                tags=[str(t) for t in (n.get('tags') or [])])                  # v7.5: for the Last Chance restart stamp
        if c['pageInfo']['hasNextPage']: cur = c['pageInfo']['endCursor']
        else: break
    return out

def _winner_last_sales(tok, run_date):
    """pid -> {ts, date (UK), rev} of the LATEST order containing the product.
    Cancelled orders excluded; revenue = this product's lines in that order,
    scaled by the order-level discount factor (same GROSS convention as the feed)."""
    since = (run_date - datetime.timedelta(days=WINNER_LOOKBACK_D)).isoformat()
    Q = ('query($c:String){orders(first:100,after:$c,query:"created_at:>=%s -status:cancelled"){'
         'pageInfo{hasNextPage endCursor} edges{node{createdAt subtotalPriceSet{shopMoney{amount}} '
         'lineItems(first:100){edges{node{product{legacyResourceId} '
         'discountedTotalSet{shopMoney{amount}}}}}}}}}' % since)
    last = {}; all_dates = {}; cur = None; n_orders = 0
    while True:
        j = requests.post(f"https://{SHOP}/admin/api/{SHOP_API}/graphql.json",
                          headers={'X-Shopify-Access-Token': tok, 'Content-Type': 'application/json'},
                          json={'query': Q, 'variables': {'c': cur}}, timeout=90).json()
        c = j['data']['orders']
        for e in c['edges']:
            node = e['node']; n_orders += 1; ts = node['createdAt']
            d = datetime.datetime.fromisoformat(ts.replace('Z', '+00:00')).astimezone(UK).date().isoformat()
            lines = [(str(li['node']['product']['legacyResourceId']),
                      float(li['node']['discountedTotalSet']['shopMoney']['amount']))
                     for li in node['lineItems']['edges'] if li['node'].get('product')]
            line_sum = sum(a for _, a in lines)
            _sub = (node.get('subtotalPriceSet') or {}).get('shopMoney', {}).get('amount')
            factor = (float(_sub) / line_sum) if (_sub is not None and line_sum > 0) else 1.0
            per = collections.defaultdict(float)
            for pid, amt in lines: per[pid] += amt * factor
            for pid, rev in per.items():
                if pid not in last or ts > last[pid]['ts']:
                    last[pid] = dict(ts=ts, date=d, rev=rev)
                all_dates.setdefault(pid, []).append(d)
        if c['pageInfo']['hasNextPage']: cur = c['pageInfo']['endCursor']
        else: break
    return last, all_dates, n_orders

def _campaign_daily_spend(run_date, pids, campaign_id):
    """pid -> [(date_iso, GBP), ...] spend rows in ONE campaign, lookback window.
    Campaign-scoped by design: each tier's rule judges only that tier's spend."""
    import google_ads_connect as ga
    gt = ga.get_access_token()
    start = (run_date - datetime.timedelta(days=WINNER_LOOKBACK_D)).isoformat()
    q = (f"SELECT campaign.id, segments.date, segments.product_item_id, metrics.cost_micros "
         f"FROM shopping_performance_view "
         f"WHERE segments.date BETWEEN '{start}' AND '{run_date.isoformat()}' "
         f"AND campaign.id = {campaign_id} AND metrics.cost_micros > 0")
    out = collections.defaultdict(list)
    for row in _ads_search(ga, gt, q):
        # Google omits productItemId on non-shopping PMax rows (e.g. a £0.003
        # Champions row 2026-07-21) — a bare ['productItemId'] KeyError'd here and
        # silently skipped the champion demotion check for every run after it.
        item = row.get('segments', {}).get('productItemId')
        if not item:
            continue
        parts = str(item).lower().split('_')
        pid = parts[2] if len(parts) >= 3 and parts[0] == 'shopify' else None
        if pid and pid in pids:
            out[pid].append((row['segments']['date'], int(row['metrics'].get('costMicros', 0)) / 1e6))
    return out

def _campaign_daily_spend_lb(run_date, pids, campaign_id, lookback_d):
    """_campaign_daily_spend with its own lookback (Last Chance has no time rule, so its window is long)."""
    import google_ads_connect as ga
    gt = ga.get_access_token()
    start = (run_date - datetime.timedelta(days=lookback_d)).isoformat()
    q = (f"SELECT campaign.id, segments.date, segments.product_item_id, metrics.cost_micros "
         f"FROM shopping_performance_view "
         f"WHERE segments.date BETWEEN '{start}' AND '{run_date.isoformat()}' "
         f"AND campaign.id = {campaign_id} AND metrics.cost_micros > 0")
    out = collections.defaultdict(list)
    for row in _ads_search(ga, gt, q):
        item = row.get('segments', {}).get('productItemId')
        if not item:
            continue
        parts = str(item).lower().split('_')
        pid = parts[2] if len(parts) >= 3 and parts[0] == 'shopify' else None
        if pid and pid in pids:
            out[pid].append((row['segments']['date'], int(row['metrics'].get('costMicros', 0)) / 1e6))
    return out

def _winners_daily_spend(run_date, pids):
    return _campaign_daily_spend(run_date, pids, WINNERS_CAMPAIGN_ID)

def _tier_daily_spend(run_date, pids, lookback_d=None):
    """pid -> [(date_iso, GBP), ...] spend in EVERY campaign a sold product can serve in: Rising (Winners),
    Proven (Champions) and Last Chance. Ladder v7 (owner 2026-10-02): a product that moves between tiers is judged
    on all of its spend, so a move never hands it fresh room for the same dry spell. Testing spend before the
    first sale stays out: it belongs to Testing's own judgement (and the window always starts after a sale)."""
    import google_ads_connect as ga
    gt = ga.get_access_token()
    start = (run_date - datetime.timedelta(days=lookback_d or WINNER_LOOKBACK_D)).isoformat()
    q = (f"SELECT campaign.id, segments.date, segments.product_item_id, metrics.cost_micros "
         f"FROM shopping_performance_view "
         f"WHERE segments.date BETWEEN '{start}' AND '{run_date.isoformat()}' "
         f"AND campaign.id IN ({WINNERS_CAMPAIGN_ID}, {CHAMPIONS_CAMPAIGN_ID}, {LC_CAMPAIGN_ID}) AND metrics.cost_micros > 0")
    out = collections.defaultdict(list)
    for row in _ads_search(ga, gt, q):
        item = row.get('segments', {}).get('productItemId')
        if not item:
            continue
        parts = str(item).lower().split('_')
        pid = parts[2] if len(parts) >= 3 and parts[0] == 'shopify' else None
        if pid and pid in pids:
            out[pid].append((row['segments']['date'], int(row['metrics'].get('costMicros', 0)) / 1e6))
    return out

def shopify_winner_kill(tok, pid, run_date=None):
    """LAST CHANCE routing (owner 2026-08-13, replaces permanent DRAFT): the product
    STAYS ACTIVE — tags l_camp + lc_campaign + lc:DATE stamp, w_campaign removed,
    feed label -> lc_campaign so it serves ONLY in 'PMax | Last Chance | UK'.
    The lc exit rule (lc_run) drafts it permanently if the last chance fails."""
    gid = f"gid://shopify/Product/{pid}"
    # audit 2026-08-16: stamp in UK time (runner is UTC, laptop AST — never naive local),
    # and strip any stale lc:* stamps from a previous LC cycle: lc_run must only ever
    # see the CURRENT stamp, or pre-stamp sales graduate the product (gate leak class).
    stamp = 'lc:' + (run_date or datetime.datetime.now(UK).date()).isoformat()
    lab = shopify_set_label_metafield(tok, pid, value=LC_TAG)     # v7.3: label first - routing follows the label
    if lab != 'ok':
        return f'err: label {lab}'                                 # nothing else changed; the next run retries the move
    old_stamps = []
    try:
        tj = requests.post(f"https://{SHOP}/admin/api/{SHOP_API}/graphql.json",
                           headers={'X-Shopify-Access-Token': tok, 'Content-Type': 'application/json'},
                           json={'query': '{product(id:"%s"){tags}}' % gid}, timeout=30).json()
        old_stamps = [str(t) for t in (((tj.get('data') or {}).get('product') or {}).get('tags') or [])
                      if str(t).startswith('lc:') and str(t) != stamp]
    except Exception:
        pass
    tags = [LOST_TAG, LC_TAG, stamp]
    M = ('mutation($id:ID!,$tags:[String!]!,$rm:[String!]!){'
         'tagsAdd(id:$id,tags:$tags){userErrors{message}} '
         'tagsRemove(id:$id,tags:$rm){userErrors{message}} }')
    try:
        j = _shopify_read(tok, M, {'id': gid, 'tags': tags, 'rm': [WINNER_TAG] + old_stamps}, timeout=30)   # retried
    except Exception as ex:
        now = _product_tags(tok, pid)                                  # v7.4: did the tag change land after all?
        if now is not None and LC_TAG in now and WINNER_TAG not in now:
            return 'ok -> last chance (confirmed by read-back)'
        if now is not None:                                            # definitely not moved: roll the label back
            shopify_set_label_metafield(tok, pid, value=WINNER_TAG)
            return f'err: tags {str(ex)[:100]} (label rolled back)'
        return f'err: tags {str(ex)[:100]} (tags unreadable - label kept, next run repairs)'
    if j.get('errors'): return str(j['errors'])[:120]
    d = j.get('data') or {}
    errs = sum([((d.get(k) or {}).get('userErrors') or []) for k in ('tagsAdd', 'tagsRemove')], [])
    return 'ok -> last chance' if not errs else str(errs)[:120]


def shopify_lc_draft(tok, pid):
    """The last chance FAILED: permanent DRAFT (the old winner-kill behaviour) +
    draft_bad_product; lc_campaign tag removed (l_camp + lc: stamp kept as history)."""
    gid = f"gid://shopify/Product/{pid}"
    pj = requests.post(f"https://{SHOP}/admin/api/{SHOP_API}/graphql.json",
                       headers={'X-Shopify-Access-Token': tok, 'Content-Type': 'application/json'},
                       json={'query': '{product(id:"%s"){publishedAt}}' % gid, 'variables': {}}, timeout=30).json()
    pub = ((pj.get('data') or {}).get('product') or {}).get('publishedAt')
    tags = ['draft_bad_product'] + (['pub:' + str(pub)[:10]] if pub else [])
    M = ('mutation($id:ID!,$tags:[String!]!,$rm:[String!]!){'
         'productUpdate(input:{id:$id,status:DRAFT}){userErrors{message}} '
         'tagsAdd(id:$id,tags:$tags){userErrors{message}} '
         'tagsRemove(id:$id,tags:$rm){userErrors{message}} }')
    j = requests.post(f"https://{SHOP}/admin/api/{SHOP_API}/graphql.json",
                      headers={'X-Shopify-Access-Token': tok, 'Content-Type': 'application/json'},
                      json={'query': M, 'variables': {'id': gid, 'tags': tags, 'rm': [LC_TAG]}},
                      timeout=30).json()
    if j.get('errors'): return str(j['errors'])[:120]
    d = j.get('data') or {}
    errs = sum([((d.get(k) or {}).get('userErrors') or []) for k in ('productUpdate', 'tagsAdd', 'tagsRemove')], [])
    return 'ok' if not errs else str(errs)[:120]


def ads_remove_winner_item_nodes(pids):
    """Remove UNIT_INCLUDED item-id nodes of these pids from the WINNERS asset group.
    Item-id includes serve regardless of feed label, so a pace-killed product must
    have its nodes swept or it keeps serving in Winners. WARN-only on failure —
    the exclusion is belt-and-braces (label alone routes GMC within hours)."""
    if not pids: return
    try:
        import google_ads_connect as ga
        gt = ga.get_access_token()
        rows = _ads_search(ga, gt,
            "SELECT asset_group_listing_group_filter.id, asset_group_listing_group_filter.type, "
            "asset_group_listing_group_filter.case_value.product_item_id.value "
            f"FROM asset_group_listing_group_filter WHERE asset_group.id = {WINNERS_AG_ID}")
        tgt = []
        for r in rows:
            f = r['assetGroupListingGroupFilter']
            if f['type'] != 'UNIT_INCLUDED': continue
            val = str(((f.get('caseValue') or {}).get('productItemId') or {}).get('value', ''))
            if any(f"_{pid}_" in val or val.endswith(f"_{pid}") for pid in pids):
                tgt.append(str(f['id']))
        if not tgt: return
        fp = f"customers/{ga.CUSTOMER_ID}/assetGroupListingGroupFilters"
        r = requests.post(f"{ga.ADS_BASE}/customers/{ga.CUSTOMER_ID}/assetGroupListingGroupFilters:mutate",
                          headers=ga._headers(gt),
                          json={'operations': [{'remove': f"{fp}/{WINNERS_AG_ID}~{i}"} for i in tgt]},
                          timeout=90).json()
        if 'error' in r: print(f"  (winners node sweep warn: {str(r)[:120]})")
        else: print(f"  winners AG: removed {len(tgt)} item-id node(s) of product(s) that left Rising")
    except Exception as ex:
        print(f"  (winners node sweep warn: {str(ex)[:100]})")


def reconcile_serving_state(dry, extra_roster=None):
    """Every-run janitor (audit 2026-08-16). The demote-time node sweep is one-shot and
    warn-only, so a single failure used to strand a product serving in Winners forever;
    and nothing ever cleaned w_campaign off DRAFT products (a republish would bypass the
    entry gate). Both are reconciled here, idempotently. Warn-only: never breaks the run."""
    out = dict(nodes_swept=0, drafts_stripped=0, err=None)
    try:
        tok = shopify_token()
        # -- current legit roster: ACTIVE + w_campaign tag --
        roster = set()
        Q = ('query($c:String){products(first:250,after:$c,query:"tag:%s status:active"){'
             'pageInfo{hasNextPage endCursor} edges{node{legacyResourceId tags}}}}' % WINNER_TAG)
        cur = None
        while True:
            j = requests.post(f"https://{SHOP}/admin/api/{SHOP_API}/graphql.json",
                              headers={'X-Shopify-Access-Token': tok, 'Content-Type': 'application/json'},
                              json={'query': Q, 'variables': {'c': cur}}, timeout=60).json()
            c = j['data']['products']
            for e in c['edges']:
                tg_ = [str(t) for t in e['node']['tags']]
                if WINNER_TAG in tg_ and CHAMPION_TAG not in tg_:   # ladder v7.1: Proven products serve in Champions by
                    roster.add(str(e['node']['legacyResourceId']))   # label, so a Winners item-id node of theirs is stray
            if c['pageInfo']['hasNextPage']: cur = c['pageInfo']['endCursor']
            else: break
        roster |= {str(x) for x in (extra_roster or ())}   # v7.4: this run's new winners (their fresh fast-path nodes)
        # -- Winners AG: sweep item-id nodes whose pid is not in the roster --
        import google_ads_connect as ga
        gt = ga.get_access_token()
        rows = _ads_search(ga, gt,
            "SELECT asset_group_listing_group_filter.id, asset_group_listing_group_filter.type, "
            "asset_group_listing_group_filter.case_value.product_item_id.value "
            f"FROM asset_group_listing_group_filter WHERE asset_group.id = {WINNERS_AG_ID}")
        stray = []
        for r in rows:
            f = r['assetGroupListingGroupFilter']
            if f['type'] != 'UNIT_INCLUDED': continue
            val = str(((f.get('caseValue') or {}).get('productItemId') or {}).get('value', ''))
            parts = val.split('_')          # shopify_GB_<pid>_<variant>
            pid = parts[2] if len(parts) >= 3 else ''
            if pid and pid not in roster:
                stray.append(str(f['id']))
        if stray and not dry:
            fp = f"customers/{ga.CUSTOMER_ID}/assetGroupListingGroupFilters"
            rj = requests.post(f"{ga.ADS_BASE}/customers/{ga.CUSTOMER_ID}/assetGroupListingGroupFilters:mutate",
                               headers=ga._headers(gt),
                               json={'operations': [{'remove': f"{fp}/{WINNERS_AG_ID}~{i}"} for i in stray]},
                               timeout=90).json()
            if 'error' in rj: out['err'] = f"node sweep: {str(rj)[:100]}"
            else: out['nodes_swept'] = len(stray)
        elif stray:
            out['nodes_swept'] = len(stray)   # dry: report what would go
        # -- DRAFT products still tagged w_campaign: strip tag + label --
        QD = ('query($c:String){products(first:250,after:$c,query:"tag:%s status:draft"){'
              'pageInfo{hasNextPage endCursor} edges{node{id legacyResourceId tags}}}}' % WINNER_TAG)
        cur = None; drafts = []; champ_drafts = set()
        while True:
            j = requests.post(f"https://{SHOP}/admin/api/{SHOP_API}/graphql.json",
                              headers={'X-Shopify-Access-Token': tok, 'Content-Type': 'application/json'},
                              json={'query': QD, 'variables': {'c': cur}}, timeout=60).json()
            c = j['data']['products']
            for e in c['edges']:
                tg_ = [str(t) for t in e['node']['tags']]
                if WINNER_TAG in tg_:
                    drafts.append(str(e['node']['legacyResourceId']))
                    if CHAMPION_TAG in tg_: champ_drafts.add(str(e['node']['legacyResourceId']))
            if c['pageInfo']['hasNextPage']: cur = c['pageInfo']['endCursor']
            else: break
        for pid in drafts:
            if dry: continue
            shopify_remove_tag(tok, pid, WINNER_TAG)
            if pid in champ_drafts: shopify_remove_tag(tok, pid, CHAMPION_TAG)   # ladder v7.1
            requests.post(f"https://{SHOP}/admin/api/{SHOP_API}/graphql.json",
                headers={'X-Shopify-Access-Token': tok, 'Content-Type': 'application/json'},
                json={'query': 'mutation($m:[MetafieldIdentifierInput!]!){ metafieldsDelete(metafields:$m){ userErrors{message} } }',
                      'variables': {'m': [{"ownerId": f"gid://shopify/Product/{pid}",
                                           "namespace": "mm-google-shopping", "key": "custom_label_1"}]}},
                timeout=30)
        out['drafts_stripped'] = len(drafts)
    except Exception as ex:
        out['err'] = str(ex)[:120]
    return out


# ── FAST-PATH NODE HYGIENE (owner 2026-09-09) ────────────────────────────────
# Every fast-path item-id node is a TEMPORARY side door: it moves a product the moment it
# wins, until custom_label_1=w_campaign reaches Google's feed (1.5-3 h) and the label RULE
# takes over. Nothing ever closed those doors, so by 8 Sep all three trees sat at Google's
# 1,000-node cap and every fast-path write failed (RESOURCE_LIMIT) - graduations still moved
# via the label, just not instantly. Two fixes:
#   * prune_settled_nodes(): once a day, delete every item-id node whose product's FEED labels
#     already do what the node does (Winners include -> all its offers carry w_campaign;
#     Testing exclude -> all offers carry w_campaign/lc_campaign, or its custom_label_2 already
#     routes it out of that tree; or the product is not in the feed at all). A node is only
#     removed when the product keeps serving exactly where it does today - proven 9 Sep on all
#     2,980 nodes: Winners' eligible offers identical before and after.
#   * FAST_PATH_CAP: the writer skips a tree that is near the cap (label path covers it)
#     instead of raising and aborting the other trees' writes.
FAST_PATH_CAP = 990
PRUNE_HOUR_UK = 4          # baseline: prune in the 04:xx UK runs (idempotent, ~12 API pages)
PRUNE_TRIGGER_NODES = 600  # ...and any run where a tree has grown past this, so a heavy graduation day cannot refill the
                           # cap before the next 04:00. A graduate writes ~24 variant nodes per tree and 20-30 graduate on a
                           # good day (~500-700 nodes), so daily-only pruning would sail close to the 1,000 cap. Zero extra
                           # API cost: ads_fast_path already read the trees and leaves the count in _TREE_NODES_SEEN.
_TREE_NODES_SEEN = 0

def prune_settled_nodes(dry):
    out = dict(checked=0, removed=0, kept=0, err=None)
    try:
        import google_ads_connect as ga
        gt = ga.get_access_token()
        ags = {WINNERS_AG_ID: 'winners', TESTING_AG_ID: 'testing_uk', AW_TESTING_AG_ID: 'testing_aw'}
        rows = _ads_search(ga, gt,
            "SELECT asset_group.id, asset_group_listing_group_filter.id, asset_group_listing_group_filter.type, "
            "asset_group_listing_group_filter.case_value.product_item_id.value "
            f"FROM asset_group_listing_group_filter WHERE asset_group.id IN ({','.join(ags)})")
        items = []   # (agid, node_id, type, pid)
        for r in rows:
            f = r['assetGroupListingGroupFilter']; v = ((f.get('caseValue') or {}).get('productItemId') or {}).get('value')
            if v and f['type'] in ('UNIT_INCLUDED', 'UNIT_EXCLUDED'):
                parts = str(v).split('_'); pid = parts[2] if len(parts) >= 3 else None
                if pid: items.append((str(r['assetGroup']['id']), str(f['id']), f['type'], pid))
        out['checked'] = len(items)
        if not items:
            return out
        # feed labels per product, ALL offers (a node stays until every variant carries the label)
        feed = {}
        for r in _ads_search(ga, gt, "SELECT shopping_product.item_id, shopping_product.custom_attribute1, shopping_product.custom_attribute2 FROM shopping_product"):
            s = r['shoppingProduct']; parts = str(s['itemId']).split('_'); pid = parts[2] if len(parts) >= 3 else None
            if not pid: continue
            f = feed.setdefault(pid, {'l1': set(), 'l2': set()})
            f['l1'].add((s.get('customAttribute1') or '').lower()); f['l2'].add((s.get('customAttribute2') or '').lower())
        def redundant(agid, ntype, pid):
            f = feed.get(pid)
            if not f: return True                                   # not in the feed: dead node
            l1, l2 = f['l1'], f['l2']
            if agid == WINNERS_AG_ID:
                # ladder v7: a Proven product (label c_champion) must not be held in Rising by an item-id node either
                return ntype == 'UNIT_INCLUDED' and (l1 == {'w_campaign'} or l1 == {'c_champion'})
            if ntype != 'UNIT_EXCLUDED': return False
            if l1 and l1 <= {'w_campaign', 'lc_campaign', 'c_champion'}: return True   # the l1 rule already excludes it (c_champion: ladder v7 tree rule)
            if agid == AW_TESTING_AG_ID: return len(l2) == 1 and 'aw26' not in l2   # not admitted by the aw26 rule anyway
            if agid == TESTING_AG_ID:    return l2 == {'aw26'}                       # Testing|UK excludes aw26 anyway
            return False
        todo = collections.defaultdict(list)
        for agid, nid, ntype, pid in items:
            if redundant(agid, ntype, pid):
                todo[agid].append(nid)
        out['kept'] = len(items) - sum(len(v) for v in todo.values())
        if dry:
            out['removed'] = sum(len(v) for v in todo.values()); return out
        fp = f"customers/{ga.CUSTOMER_ID}/assetGroupListingGroupFilters"
        for agid, ids in todo.items():
            for i in range(0, len(ids), 200):
                chunk = ids[i:i+200]
                rj = requests.post(f"{ga.ADS_BASE}/customers/{ga.CUSTOMER_ID}/assetGroupListingGroupFilters:mutate",
                                   headers=ga._headers(gt), json={'operations': [{'remove': f"{fp}/{agid}~{n}"} for n in chunk]}, timeout=120).json()
                if 'error' in rj and len(chunk) > 1:   # a subdivision may refuse to lose its last specific child: keep one
                    rj = requests.post(f"{ga.ADS_BASE}/customers/{ga.CUSTOMER_ID}/assetGroupListingGroupFilters:mutate",
                                       headers=ga._headers(gt), json={'operations': [{'remove': f"{fp}/{agid}~{n}"} for n in chunk[:-1]]}, timeout=120).json()
                    if 'error' not in rj: out['removed'] += len(chunk) - 1; out['kept'] += 1; continue
                if 'error' in rj: out['err'] = f"{ags[agid]}: {str(rj)[:100]}"; break
                out['removed'] += len(chunk)
    except Exception as ex:
        out['err'] = str(ex)[:120]
    return out



# ── FEED LABEL SYNC GUARD (owner 2026-10-03, ladder v7.6) ─────────────────────────────────────────────────────────────
# A product's campaign is decided by custom_label_1 as GOOGLE holds it: the engine writes the label in Shopify and the feed
# app copies it to Google, normally within 1-1.5 h. On 3 Oct two products had never been copied (26 h and 78 h) - one served in
# Rising while the engine judged it as Proven. Once an hour this compares every ACTIVE product's tier (from its tags, the
# engine's own record) with the label on EVERY offer Google holds. Still wrong LABEL_STUCK_H after the label was written ->
# a neutral tag is added and removed: the product update makes the feed app send the product again. Never writes a label or
# a routing tag; a Shopify label that disagrees with the tags is reported, not guessed at.
LABEL_SYNC_MINUTE_MAX = 8      # the first run of every hour at the 8-minute cadence
LABEL_STUCK_H         = 2.0    # label written longer ago than this and Google still disagrees = stuck
LABEL_RESEND_GAP_H    = 2.0    # at most one re-send per product every 2 h (judged on the product's own updatedAt)
LABEL_ALERT_H         = 6.0    # still stuck after this long = shout in Telegram (the re-send is not working for it)
LABEL_RESEND_CAP      = 25     # more stuck products than this = a feed outage, not a product problem: alert, touch nothing
LABEL_RESEND_TAG      = 'feed_resync'


def _tier_of_tags(tags):
    tg = {str(t) for t in tags}
    return 'proven' if CHAMPION_TAG in tg else 'lc' if LC_TAG in tg else 'rising' if WINNER_TAG in tg else 'testing'


def _tier_of_label(v):
    return {CHAMPION_TAG: 'proven', LC_TAG: 'lc', WINNER_TAG: 'rising'}.get((v or '').strip().lower(), 'testing')


def _google_offer_tiers():
    """pid -> Counter(tier of every offer Google holds for it), from shopping_product (custom_attribute1 = custom_label_1)."""
    import google_ads_connect as ga
    gt = ga.get_access_token()
    out = collections.defaultdict(collections.Counter)
    for r in _ads_search(ga, gt, "SELECT shopping_product.item_id, shopping_product.custom_attribute1 FROM shopping_product"):
        sp = r['shoppingProduct']; parts = str(sp.get('itemId', '')).split('_'); pid = parts[2] if len(parts) >= 3 else None
        if pid:
            out[pid][_tier_of_label(sp.get('customAttribute1'))] += 1
    return out


def _label_state(tok, pids):
    """pid -> (Shopify custom_label_1, when that label was written, when the product was last updated) for a few products."""
    out, pids = {}, list(pids)
    for i in range(0, len(pids), 50):
        ids = [f"gid://shopify/Product/{p}" for p in pids[i:i + 50]]
        j = _shopify_read(tok, 'query($ids:[ID!]!){nodes(ids:$ids){... on Product{legacyResourceId updatedAt '
                               'l1:metafield(namespace:"mm-google-shopping",key:"custom_label_1"){value updatedAt}}}}', {'ids': ids}, timeout=60)
        for n in ((j.get('data') or {}).get('nodes') or []):
            if n:
                m = n.get('l1') or {}
                out[str(n['legacyResourceId'])] = (m.get('value'), m.get('updatedAt'), n.get('updatedAt'))
    return out


def label_sync_check(feed, dry, now=None, force=False):
    """Hourly feed label guard (see the block above). None when it is not this run's turn, else
    dict(checked, syncing, waiting, resent, stuck_long, label_wrong, cleaned, err). Never raises."""
    now = now or datetime.datetime.now(UK)
    if not (force or now.minute < LABEL_SYNC_MINUTE_MAX):
        return None
    res = dict(checked=0, syncing=[], waiting=[], resent=[], stuck_long=[], label_wrong=[], cleaned=0, err=None)
    try:
        tok = shopify_token()
        active = {str(p['pid']): p for p in feed}
        if not dry:                     # a re-send whose second write failed leaves the neutral tag behind: take it off
            for pid, p in active.items():
                if LABEL_RESEND_TAG in [str(t) for t in p['tags']] and shopify_remove_tag(tok, pid, LABEL_RESEND_TAG) == 'ok':
                    res['cleaned'] += 1
        offers = _google_offer_tiers()
        if not offers:
            res['err'] = 'Google returned no offers - glitch; label check skipped'
            return res
        cand = {}
        for pid, p in active.items():
            c = offers.get(pid)
            if not c:
                continue
            res['checked'] += 1
            want = _tier_of_tags(p['tags'])
            wrong = sum(v for t, v in c.items() if t != want)
            if wrong:
                cand[pid] = (p, want, wrong, sum(c.values()))
        if not cand:
            return res
        state = _label_state(tok, cand)
        utc_now = now.astimezone(datetime.timezone.utc)

        def hours(ts):
            return (utc_now - datetime.datetime.fromisoformat(ts.replace('Z', '+00:00'))).total_seconds() / 3600 if ts else None
        stuck = []
        for pid, (p, want, wrong, tot) in cand.items():
            lab, lab_ts, prod_ts = state.get(pid, (None, None, None))
            row = dict(pid=pid, name=p['name'], want=want, wrong=wrong, offers=tot, label_age=hours(lab_ts), product_age=hours(prod_ts))
            if _tier_of_label(lab) != want:
                res['label_wrong'].append(row)          # Shopify's own label disagrees with the tags: report, never guess
                continue
            if row['label_age'] is None or row['label_age'] < LABEL_STUCK_H:
                res['syncing'].append(row)
                continue
            if row['label_age'] >= LABEL_ALERT_H:
                res['stuck_long'].append(row)
            if row['product_age'] is not None and row['product_age'] < LABEL_RESEND_GAP_H:
                res['waiting'].append(row)             # re-sent (or edited) recently: give the feed app its time
                continue
            stuck.append(row)
        if len(stuck) > LABEL_RESEND_CAP:
            res['err'] = f'SAFETY STOP: {len(stuck)} stuck labels > cap {LABEL_RESEND_CAP} - looks like a feed outage; nothing re-sent'
            return res
        for row in stuck:
            if dry:
                row['outcome'] = 'DRY'
            else:
                r1 = shopify_add_tag(tok, row['pid'], LABEL_RESEND_TAG)
                r2 = shopify_remove_tag(tok, row['pid'], LABEL_RESEND_TAG) if r1 == 'ok' else 'skipped'
                row['outcome'] = 'ok' if (r1 == 'ok' and r2 == 'ok') else f'add {r1} / remove {r2}'
            res['resent'].append(row)
    except Exception as ex:
        res['err'] = f'label check error (skipped; nothing else affected): {type(ex).__name__}: {str(ex)[:120]}'
    return res

def lc_run(run_date, dry, life=None):
    """LAST CHANCE exit + graduation rule (owner 2026-08-13).
      * GRADUATE: a sale dated AFTER the lc: stamp -> back to Winners (tag + label +
        ads fast-path). Pre-lc sales NEVER graduate (no ping-pong). v7.5: stamped lc_grad:<rescue sale date> FIRST
        (no stamp, no graduation this run); from then on only sales on/after that date count - a new graduate again.
      * EXIT: lc-campaign spend since stamp > min(price/2, £20) with no post-stamp sale -> permanent DRAFT
        (owner 2026-10-02, ladder v7; the old 90-days-saleless time rule is REMOVED - no time rules).
    Fail-safe: any error skips this section for the run."""
    res = dict(pool=0, graduated=[], drafted=[], err=None)
    try:
        tok = shopify_token()
        Q = ('query($c:String){products(first:250,after:$c,query:"tag:%s status:active"){'
             'pageInfo{hasNextPage endCursor} edges{node{legacyResourceId title tags '
             'priceRangeV2{minVariantPrice{amount}}}}}}' % LC_TAG)
        pool = {}; cur = None
        while True:
            j = _shopify_read(tok, Q, {'c': cur}, timeout=60)   # v7.2: retried read
            c = j['data']['products']
            for e in c['edges']:
                n = e['node']; tl = [str(t) for t in n['tags']]
                if LC_TAG not in tl: continue          # tag-search tokenises; exact check
                # audit 2026-08-16: NEWEST stamp wins (stale stamps from an earlier LC
                # cycle made pre-stamp sales count as post-stamp); keep the raw list so
                # graduation can consume every stamp tag.
                stamps = sorted(t[3:13] for t in tl if t.startswith('lc:'))
                pool[str(n['legacyResourceId'])] = dict(
                    name=n['title'], price=float(n['priceRangeV2']['minVariantPrice']['amount']),
                    stamp=(stamps[-1] if stamps else run_date.isoformat()),
                    stamp_tags=['lc:' + s for s in stamps],
                    grad_tags=[t for t in tl if t.startswith(LC_GRAD_PREFIX)])   # v7.5: restart stamps of earlier cycles
            if c['pageInfo']['hasNextPage']: cur = c['pageInfo']['endCursor']
            else: break
        res['pool'] = len(pool)
        if not pool: return res
        # audit 2026-08-16: LC lives up to 90d but the old 60d pull silently dropped
        # older post-stamp sales (false 'saleless' drafts + lost 1-sale protection).
        # Lifetime pull covers the whole horizon; main() passes it in to avoid a re-pull.
        if life is None:
            life, _n = _lifetime_sales(tok)
        sale_dates = {p_: [x['date'] for x in lst] for p_, lst in life.items()}
        if not sale_dates:
            res['err'] = 'orders pull returned 0 — glitch; lc rule skipped'; return res
        spend = _campaign_daily_spend_lb(run_date, set(pool), LC_CAMPAIGN_ID, LC_LOOKBACK_D)   # ladder v7: no time rule, so a long spend window
        grads, exits = [], []
        for pid, m in pool.items():
            post = [x for x in sale_dates.get(pid, []) if x > m['stamp']]
            # owner 2026-08-24: graduation = ONE post-stamp sale (was two, 2026-08-16) —
            # matches the entry gate lowered to 1 the same day; the Winners pace rule's
            # 1-sale branch bounds a bad re-entry, so ping-pong risk is now priced in.
            if len(post) >= 1:
                grads.append((pid, m)); continue
            spent = sum(v for d, v in spend.get(pid, ()) if d > m['stamp'])
            # owner 2026-08-16: allowance aligned to the LC tROAS 2.1 — a product may
            # spend what ONE sale at 2.1 ROAS would justify (price/2.1, £20 cap)
            # before drafting. Was min(price/7, £5) — too strict for LC's purpose.
            # owner 2026-10-02 (ladder v7): price / 2 (max £20) - "ROAS 2 of the price" with no new sale. No time rule.
            allow = min(m['price'] / 2.0, 20.0) if m['price'] > 0 else 5.0
            if spent > allow:
                exits.append((pid, m, f'lc spend £{spent:.2f} > £{allow:.2f}, no sale since {m["stamp"]}'))
        if len(exits) > LC_KILL_CAP:
            res['err'] = f'SAFETY STOP: {len(exits)} lc exits > cap {LC_KILL_CAP} — nothing drafted'
            return res
        for pid, m in grads:
            # v7.5 (owner 2026-10-03): the rescue sale = its new sale #1; everything before it stops counting
            rescue = min(x for x in sale_dates.get(pid, []) if x > m['stamp'])
            gstamp = f"{LC_GRAD_PREFIX}{rescue}"
            if dry:
                print(f"  lc would-GRADUATE {pid} -> Winners (history restarts {rescue}) | {m['name'][:40]}"); continue
            r0 = shopify_add_tag(tok, pid, gstamp)                             # the restart stamp FIRST
            if r0 != 'ok':
                now = _product_tags(tok, pid)                                  # the write may have landed anyway
                if not (now is not None and gstamp in now):
                    print(f"  lc GRADUATE {pid} postponed: restart stamp not written ({r0}) - next run retries | {m['name'][:40]}")
                    continue
            shopify_add_tag(tok, pid, WINNER_TAG)
            shopify_set_label_metafield(tok, pid, value=WINNER_TAG)
            shopify_remove_tag(tok, pid, LC_TAG)
            shopify_remove_tag(tok, pid, LOST_TAG)
            for _st in m.get('stamp_tags', []):   # consume stamps — stale ones re-graduate on pre-stamp sales
                shopify_remove_tag(tok, pid, _st)
            for _st in m.get('grad_tags', []):    # v7.5: an earlier cycle's restart stamp is replaced by this one
                if _st != gstamp:
                    shopify_remove_tag(tok, pid, _st)
            print(f"  lc GRADUATE {pid} -> Winners (history restarts {rescue}) | {m['name'][:40]}")
            res['graduated'].append({'pid': pid, 'name': m['name']})
        if res['graduated'] and not dry:
            try:
                ads_fast_path([{'pid': g['pid'], 'name': g['name']} for g in res['graduated']], tok)
            except Exception as ex:
                print(f"  (lc fast-path warn: {str(ex)[:100]})")
        for pid, m, why in exits:
            out = 'DRY' if dry else shopify_lc_draft(tok, pid)
            print(f"  {'would draft' if dry else 'draft'} lc-exit {pid} -> {out} | {why} | {m['name'][:40]}")
            res['drafted'].append({'pid': pid, 'name': m['name'], 'why': why})
        return res
    except Exception as ex:
        res['err'] = f'lc rule error (skipped): {str(ex)[:150]}'
        return res


def _write_winner_kills_log(rows, run_date):
    import csv as _csv, os as _os
    new = not _os.path.exists(WINNER_KILLS_LOG)
    with open(WINNER_KILLS_LOG, 'a', newline='', encoding='utf-8') as f:
        w = _csv.writer(f)
        if new: w.writerow(['timestamp', 'data_date', 'product_id', 'name', 'cycle_opened_by',
                            'allowance', 'spent', 'outcome'])
        ts = datetime.datetime.now(UK).strftime('%Y-%m-%d %H:%M:%S')
        for r in rows:
            w.writerow([ts, run_date.isoformat(), r['pid'], r['name'], r['opened'],
                        round(r['allow'], 2), round(r['spent'], 2), r.get('outcome', '')])

def winner_pace_run(run_date, dry, life=None, shared=None, skip=None, exclude=None):
    """Evaluate every winner against the pace rule. Kills only when live (>= start
    date and not --dry). Returns dict(live, evaluated, flagged, killed, pool, err, closest)."""
    live = (run_date >= WINNER_KILL_START) and not dry
    res = dict(live=live, evaluated=0, flagged=[], killed=0, pool=0, err=None, closest=[])
    try:
        tok = shopify_token()
        winners = _winner_products(tok)
        # CHAMPIONS EXEMPT (2026-07-20): champions keep w_campaign but serve in the Champions
        # campaign — their winners-campaign spend is residual, and their last sale can be stale,
        # so judging them here would false-kill. They have their OWN trailing-2.0 demotion rule.
        champs = exclude if exclude is not None else _champion_pids(tok)   # v7.3: exact Proven set from champion_run
        # ladder v7.1: `skip` = products promoted to Proven THIS run (Shopify's tag search can lag behind the write)
        winners = {pid: m for pid, m in winners.items() if pid not in champs and pid not in (skip or set())}
        res['evaluated'] = len(winners); res['pool'] = len(winners)
        if not winners: return res
        # v12: judged on lifetime chronological sales — the two-sale allowance
        # needs per-sale revenue, which the last-sale-only pull did not keep.
        if life is None:
            life, _n = _lifetime_sales(tok)
        if not life:   # glitch guard: a live store ALWAYS has orders
            res['err'] = 'orders pull returned 0 orders — glitch; winner kills skipped'
            return res
        # ladder v7 (owner 2026-10-02): Rising + Proven + Last Chance spend - no fresh room after a move. v7.1: reuse the
        # Proven section's pull when it covered every product judged here, cut to this rule's own 60-day window.
        if shared and shared[1] is not None and set(winners) <= shared[0]:
            cut = (run_date - datetime.timedelta(days=WINNER_LOOKBACK_D)).isoformat()
            spend = {pid: [(d, v) for d, v in rows if d >= cut] for pid, rows in shared[1].items() if pid in winners}
        else:
            spend = _tier_daily_spend(run_date, set(winners))
        rows = []
        for pid, m in winners.items():
            slist = _since_reset(life.get(pid, []), _lc_reset_date(m.get('tags') or ()))   # v7.5: new sales only after Last Chance
            if len(slist) >= 2:
                # v12: the last two sales pool their revenue; the cycle opens at the
                # sale BEFORE them (3rd-last), or the older of the two if only two
                # exist. Spend counts from the day AFTER the anchor. If the anchor
                # predates the Ads spend window, spend truncates to the window —
                # undercounting spend only ever DELAYS a kill (safe direction).
                anchor = slist[-3]['date'] if len(slist) >= 3 else slist[-2]['date']
                rev2 = slist[-1]['rev'] + slist[-2]['rev']
                allow = max(rev2, m['price']) / WINNER_PACE_ROAS
                spent = sum(v for d, v in spend.get(pid, ()) if d > anchor)
                opened = f"last-2 £{rev2:.2f}, window since {anchor}"
            elif len(slist) == 1:
                allow = max(slist[0]['rev'], m['price']) / WINNER_PACE_ROAS
                spent = sum(v for d, v in spend.get(pid, ()) if d > slist[0]['date'])
                opened = f"sale £{slist[0]['rev']:.2f} on {slist[0]['date']}"
            else:   # no lifetime sale on record — allowance from price, spend from whole window
                allow = m['price'] / WINNER_PACE_ROAS
                spent = sum(v for _, v in spend.get(pid, ()))
                opened = f"no sale on record (price £{m['price']:.2f})"
            rows.append(dict(pid=pid, name=m['name'], allow=allow, spent=spent, opened=opened,
                             pct=(spent / allow * 100) if allow > 0 else 0.0))
        rows.sort(key=lambda x: -x['pct'])
        res['closest'] = [r for r in rows if 60 <= r['pct'] <= 100][:5]
        flagged = [r for r in rows if r['spent'] > r['allow']]
        res['flagged'] = flagged
        if not flagged: return res
        if len(flagged) > WINNER_KILL_CAP:
            res['err'] = (f'SAFETY STOP: {len(flagged)} winner kills > cap {WINNER_KILL_CAP} — '
                          f'looks like a data glitch; NOTHING drafted, investigate')
            res['flagged'] = []          # do not act, do not spam details
            return res
        for r in flagged:
            if live:
                r['outcome'] = shopify_winner_kill(tok, r['pid'], run_date)
                print(f"  winner KILL {r['pid']} -> {r['outcome']} | spent £{r['spent']:.2f} > "
                      f"allowance £{r['allow']:.2f} ({r['opened']}) | {r['name'][:40]}")
            else:
                r['outcome'] = 'DRY' if dry else 'PREVIEW (dormant)'
                print(f"  winner would-KILL {r['pid']} | spent £{r['spent']:.2f} > "
                      f"allowance £{r['allow']:.2f} ({r['opened']}) | {r['name'][:40]}")
        if live:
            ads_remove_winner_item_nodes([r['pid'] for r in flagged
                                          if str(r.get('outcome', '')).startswith('ok')])
            res['killed'] = sum(1 for r in flagged if str(r.get('outcome', '')).startswith('ok'))
            res['pool'] = len(winners) - res['killed']
            _write_winner_kills_log(flagged, run_date)
        return res
    except Exception as ex:
        res['err'] = f'winner rule error (skipped this run; testing kills unaffected): {str(ex)[:150]}'
        return res

# ── CHAMPIONS TIER (owner-approved 2026-07-20) ──────────────────────────────
# Third campaign for PROVEN repeat-sellers: PMax | Champions | UK (MCV, tROAS 2.0, £50/day).
# Only repeat-sellers ever absorbed extra spend productively (Jul-16 analysis) — this tier
# gives them a looser target so Google buys volume, watched by a per-product trailing floor.
#
#   ENTRY      2 lifetime orders (order count, not units; cancelled excluded; 3->2 owner 2026-08-20; was 4->3 2026-08-16, 3->4 2026-07-31)
#              -> tag c_champion (w_campaign KEPT), feed label -> c_champion,
#                 item-ids: Champions AG include + Winners AG include-nodes removed.
#   DEMOTE     Champions-campaign spend since the 3rd-last sale
#                > (revenue of the last TWO sales) / 2.0
#              = the trailing-ROAS-2.0 floor smoothed over two sale-gaps (owner-corrected
#              2026-07-20: counting 3 sales' revenue would double-count the anchor sale and
#              only enforce ~1.33). Spend charged from the day AFTER the anchor sale — late,
#              never early. Demotion is a SOFT landing: back to Winners, NOT drafted; the
#              winners pace clock restarts anchored on its own last sale.
#   RE-ENTER   2 NEW sales (dates strictly after the champ_demoted: stamp) while in Winners.
#   2nd FAIL   nothing special here — a demoted champion is a normal winner again; if it
#              breaches the winners 2.8 pace, the existing winner rule drafts it (permanent).
#   GUARDS     promotions capped (glitch), demotions capped (glitch), zero-orders pull skips
#              the whole section; failures WARN and never touch the testing/winner kills.
# ── CHAMPIONS DISABLED (owner 2026-08-07) ──────────────────────────────────
# 20-day audit: Champions spent £953 at pixel 1.60 (W32: 0.75) with CPC £0.46 vs
# Winners' 1.99 at £0.35 over the same period — a 7-product roster can't feed a
# tROAS bidder. Campaign paused, roster folded back into Winners (c_champion tag
# removed, feed label restored to w_campaign). Flip to True only when the roster
# can be rebuilt at 25-30+ products.
# ── LADDER v7 (owner 2026-10-01/02): PROVEN = PMax | Champions | UK, MCV tROAS 2.8, £300/day ─────────────
# Entry 4th sale + last-4 ROAS >= max(2.5, own break-even); exit when the last 4 sales fall under that line;
# re-entry 2 sales after the demotion + ROAS >= 2.5 since; routing by LABEL only (Testing trees exclude
# c_champion); every spend window counts Rising + Proven + Last Chance spend. Evidence: plan v7 on the Desktop.
CHAMPIONS_ENABLED       = True
CHAMPION_TAG            = 'c_champion'
CHAMPION_DEMOTED_PREFIX = 'champ_demoted:'          # champ_demoted:YYYY-MM-DD, set on demotion
LC_GRAD_PREFIX          = 'lc_grad:'                # v7.5 (owner 2026-10-03): lc_grad:YYYY-MM-DD = the date of the sale that took the product
                                                    # OUT of Last Chance. From then on it is a new Testing graduate again: only sales on/after the
                                                    # stamp count in every Rising / Proven rule - Proven needs 4 NEW sales and the last-4 ROAS on the
                                                    # spend since the first of them. No credit for the sales made before Last Chance.
CHAMPIONS_CAMPAIGN_ID   = '24047674442'             # PMax | Champions | UK  (created 2026-07-20)
CHAMPIONS_AG_ID         = '6731971798'              # its asset group (listing tree mirrors Winners)
CHAMPION_ENTRY_ORDERS   = 4   # ladder v7 (owner 2026-10-01): 4th sale, checked vs the 5th-7th. Older history: 4->3 (owner 2026-08-16): LC tier now caps false-positive cost (demote after ~1 order's rev, recovery path), and the continuation curve is flat past 3 (P(4|3)=60% vs P(5|4)=62%). Was: raised 3->4 (owner 2026-07-31): cohort audit — of 23 products that
                              # ever entered at 3 sales, the 10 that never resold burned 24% of all
                              # champion spend for £0 return (ROAS 0.00), while 4-5-sale entrants ran
                              # 3.14 and 6+ ran 4.20. Sale #4 filters the whole dud class at minimal
                              # star-delay; 5+ would only delay the profitable 4-5 cohort.
CHAMPION_LINE           = 2.5     # ladder v7: entry + exit ROAS line on the last 4 sales (own break-even if higher)
CHAMPION_REENTRY_ROAS   = 2.5     # ladder v7: re-entry needs ROAS >= 2.5 on all spend since the demotion
PROVEN_LOOKBACK_D       = 200     # ladder v7: LONGEST spend window for the last-4 check (each run sizes it to the oldest anchor, min 60)
BE_SNAPSHOT             = 'breakeven_snapshot.json'   # LOCAL runs only (gitignored); Actions reads the PROVEN_BE_JSON secret
CHAMPION_REPROMOTE_SALES = 2
CHAMPION_PROMOTE_CAP    = 25                        # >N promotions in one run = glitch -> abort section
CHAMPION_DEMOTE_CAP     = 10                        # >N demotions in one run = glitch -> abort section
CHAMPION_LOG            = 'champion_moves_log.csv'
LIFETIME_SINCE          = '2026-01-01'              # predates the store — lifetime = complete

def _champion_pids(tok):
    """ACTIVE products tagged c_champion -> set of pids."""
    Q = ('query($c:String){products(first:250,after:$c,query:"tag:c_champion status:active"){'
         'pageInfo{hasNextPage endCursor} edges{node{legacyResourceId}}}}')
    out = set(); cur = None
    while True:
        j = _shopify_read(tok, Q, {'c': cur}, timeout=60)   # v7.2: retried read
        c = j['data']['products']
        for e in c['edges']: out.add(str(e['node']['legacyResourceId']))
        if c['pageInfo']['hasNextPage']: cur = c['pageInfo']['endCursor']
        else: break
    return out

def _lifetime_sales(tok):
    """pid -> chronological [{ts, date(UK), rev}, ...] — ONE entry per ORDER containing the
    product, all orders lifetime. Cancelled excluded; per-order product revenue with the same
    order-level discount scaling as the feed (GROSS, refunds ignored). ~10 pages / run."""
    Q = ('query($c:String){orders(first:100,after:$c,query:"created_at:>=%s -status:cancelled"){'
         'pageInfo{hasNextPage endCursor} edges{node{createdAt subtotalPriceSet{shopMoney{amount}} '
         'lineItems(first:100){edges{node{product{legacyResourceId} '
         'discountedTotalSet{shopMoney{amount}}}}}}}}}' % LIFETIME_SINCE)
    sales = collections.defaultdict(list); cur = None; n_orders = 0
    while True:
        j = _shopify_read(tok, Q, {'c': cur}, timeout=90)   # v7.2: retried read
        c = j['data']['orders']
        for e in c['edges']:
            node = e['node']; n_orders += 1; ts = node['createdAt']
            d = datetime.datetime.fromisoformat(ts.replace('Z', '+00:00')).astimezone(UK).date().isoformat()
            lines = [(str(li['node']['product']['legacyResourceId']),
                      float(li['node']['discountedTotalSet']['shopMoney']['amount']))
                     for li in node['lineItems']['edges'] if li['node'].get('product')]
            line_sum = sum(a for _, a in lines)
            _sub = (node.get('subtotalPriceSet') or {}).get('shopMoney', {}).get('amount')
            factor = (float(_sub) / line_sum) if (_sub is not None and line_sum > 0) else 1.0
            per = collections.defaultdict(float)
            for pid, amt in lines: per[pid] += amt * factor
            for pid, rev in per.items():
                sales[pid].append(dict(ts=ts, date=d, rev=rev))
        if c['pageInfo']['hasNextPage']: cur = c['pageInfo']['endCursor']
        else: break
    for pid in sales: sales[pid].sort(key=lambda s: s['ts'])
    return sales, n_orders

def _stamp_date(tags, prefix):
    """Latest valid <prefix>YYYY-MM-DD stamp, or None. v7.3: a malformed stamp (typed by hand) is ignored - it used
    to make the date parsing raise and skip the whole Proven section every run."""
    ds = []
    for t in tags:
        t = str(t)
        if t.startswith(prefix):
            v = t[len(prefix):]
            try:
                if len(v) != 10 or v[4] != '-' or v[7] != '-':
                    raise ValueError(v)
                datetime.date.fromisoformat(v)
                ds.append(v)
            except ValueError:
                pass
    return max(ds) if ds else None


def _demoted_date(tags):
    """Latest valid champ_demoted:YYYY-MM-DD stamp, or None."""
    return _stamp_date(tags, CHAMPION_DEMOTED_PREFIX)


def _lc_reset_date(tags):
    """v7.5: latest valid lc_grad:YYYY-MM-DD stamp (the sale that took the product out of Last Chance), or None."""
    return _stamp_date(tags, LC_GRAD_PREFIX)


def _since_reset(slist, reset):
    """v7.5: a product that sold its way out of Last Chance starts again like a new Testing graduate - only the sales from
    that sale on count. Never mutates the shared lifetime-sales list."""
    return [x for x in slist if x['date'] >= reset] if reset else slist

def _ag_listing_state(ga, gt, ag_ids):
    """agid -> (item_subdiv_resource_name, {item_id_lower: node_resource_name}).
    Same discovery as ads_fast_path: the item-id subdivision is the SUBDIVISION whose
    case is attr1-with-no-value."""
    rows = _ads_search(ga, gt,
        "SELECT asset_group.id, asset_group_listing_group_filter.resource_name, "
        "asset_group_listing_group_filter.type, asset_group_listing_group_filter.parent_listing_group_filter, "
        "asset_group_listing_group_filter.case_value.product_custom_attribute.index, "
        "asset_group_listing_group_filter.case_value.product_custom_attribute.value, "
        "asset_group_listing_group_filter.case_value.product_item_id.value "
        f"FROM asset_group_listing_group_filter WHERE asset_group.id IN ({','.join(ag_ids)})")
    info = {}
    for agid in ag_ids:
        ag = [x for x in rows if str(x['assetGroup']['id']) == agid]
        subdiv = None; have = {}
        for x in ag:
            f = x['assetGroupListingGroupFilter']; cv = f.get('caseValue', {})
            pca = cv.get('productCustomAttribute')
            if f['type'] == 'SUBDIVISION' and pca is not None and 'value' not in pca:
                subdiv = f['resourceName']
        for x in ag:
            f = x['assetGroupListingGroupFilter']; cv = f.get('caseValue', {})
            if f.get('parentListingGroupFilter') == subdiv and cv.get('productItemId', {}).get('value'):
                have[cv['productItemId']['value'].lower()] = f['resourceName']
        if not subdiv: raise RuntimeError(f"item-id subdivision not found in AG {agid}")
        info[agid] = (subdiv, have)
    return info

def _iid_pid(iid):
    """'shopify_zz_{pid}_{vid}' -> pid ('' if the value has an unexpected shape)."""
    parts = iid.split('_')
    return parts[2] if len(parts) >= 4 else ''

def champion_ads_move(roster_pids, demote_pids, stok, winner_pids=None):
    """Listing-tree RECONCILIATION, every run (label path = backup, as with winners):
       - every roster champion's item-ids: Champions AG include + Winners include-nodes removed
         + TESTING AG item-BLOCK enforced (label-era winners were only blocked from Testing by
           custom_label_1=w_campaign; the c_champion label ends that block, so without an
           item-id exclude a champion becomes Testing-eligible again — found by the
           segmentation proof 2026-07-20, 11/14 champions leaked)
       - freshly demoted: Winners AG include restored
       - STRAY champions nodes (item-ids of products no longer in the roster): removed
    Fully idempotent + self-healing: an interrupted run (e.g. the 8-min cron cancelling a
    manual run mid-promotion) leaves tags without listing moves — the next run repairs it.
    Any failure WARNs — the c_champion/w_campaign label still moves the product on the
    next feed sync, and the rules stay campaign-scoped either way."""
    if not (roster_pids or demote_pids): return 0, None
    try:
        import google_ads_connect as ga
        gt = ga.get_access_token()
        info = _ag_listing_state(ga, gt, [CHAMPIONS_AG_ID, WINNERS_AG_ID, TESTING_AG_ID])
        ch_sub, ch_have = info[CHAMPIONS_AG_ID]
        wi_sub, wi_have = info[WINNERS_AG_ID]
        te_sub, te_have = info[TESTING_AG_ID]
        creates, removes = [], set()
        def _node(agid, subdiv, have, iid, node_type):
            if iid not in have:
                creates.append({'create': {'assetGroup': f"customers/{ga.CUSTOMER_ID}/assetGroups/{agid}",
                                           'type': node_type, 'listingSource': 'SHOPPING',
                                           'parentListingGroupFilter': subdiv,
                                           'caseValue': {'productItemId': {'value': iid}}}})
        def _include(agid, subdiv, have, iid):
            _node(agid, subdiv, have, iid, 'UNIT_INCLUDED')
        roster_iids = set()
        roster_set = {str(p) for p in roster_pids}
        for pid in roster_pids:
            for iid in _variant_item_ids(stok, pid):
                roster_iids.add(iid)
                _include(CHAMPIONS_AG_ID, ch_sub, ch_have, iid)
                _node(TESTING_AG_ID, te_sub, te_have, iid, 'UNIT_EXCLUDED')   # never back into Testing
                if iid in wi_have: removes.add(wi_have[iid])
        for pid in demote_pids:
            for iid in _variant_item_ids(stok, pid):
                _include(WINNERS_AG_ID, wi_sub, wi_have, iid)
        for iid, rn in ch_have.items():                     # stray cleanup (covers demotions too)
            if iid not in roster_iids: removes.add(rn)
        # STALE PRUNE (2026-07-29): dead winners' item-id nodes were never removed, so the
        # Winners/Testing trees crept to the 1,000-node cap and every create bounced with
        # RESOURCE_LIMIT. Each run now drops nodes whose product is no longer an active
        # winner/champion. Sanity-gated: an implausibly small winner set skips the prune.
        if winner_pids and len(winner_pids) >= 20:
            keep_wi = winner_pids - roster_set              # champions serve from Champions AG
            keep_te = winner_pids | roster_set
            for iid, rn in wi_have.items():
                if _iid_pid(iid) not in keep_wi: removes.add(rn)
            for iid, rn in te_have.items():
                if _iid_pid(iid) not in keep_te: removes.add(rn)
        # removes FIRST (frees node slots), then creates — both chunked; never one giant
        # atomic mutate again (that is what wedged at the cap).
        rm_ops = [{'remove': rn} for rn in sorted(removes)]
        for i in range(0, len(rm_ops), 500):
            r = requests.post(f"{ga.ADS_BASE}/customers/{ga.CUSTOMER_ID}/assetGroupListingGroupFilters:mutate",
                              headers=ga._headers(gt), json={'operations': rm_ops[i:i + 500]}, timeout=120).json()
            if 'error' in r: raise RuntimeError(str(r)[:300])
        for i in range(0, len(creates), 500):
            r = requests.post(f"{ga.ADS_BASE}/customers/{ga.CUSTOMER_ID}/assetGroupListingGroupFilters:mutate",
                              headers=ga._headers(gt), json={'operations': creates[i:i + 500]}, timeout=120).json()
            if 'error' in r: raise RuntimeError(str(r)[:300])
        n_ops = len(creates) + len(removes)
        if n_ops:
            print(f"  champion fast-path: {len(creates)} creates / {len(removes)} removes "
                  f"(roster {len(roster_pids)} / demoted {len(demote_pids)})")
        return n_ops, None
    except Exception as ex:
        print(f"  !! champion fast-path FAILED (label path moves them on next feed sync): {ex}")
        return 0, str(ex)[:150]

def _write_champion_log(rows):
    import csv as _csv, os as _os
    new = not _os.path.exists(CHAMPION_LOG)
    with open(CHAMPION_LOG, 'a', newline='', encoding='utf-8') as f:
        w = _csv.writer(f)
        if new: w.writerow(['timestamp', 'run_date', 'action', 'product_id', 'name',
                            'lifetime_orders', 'allowance', 'spent', 'outcome'])
        for r in rows: w.writerow(r)

def _load_breakeven():
    """pid -> own break-even ROAS. Source: the PROVEN_BE_JSON environment variable (an encrypted repo secret - this repo is
    PUBLIC, so product costs never go into a committed file), else a local BE_SNAPSHOT file for runs on the laptop. Only
    products whose break-even is above 2.5 need to be listed (the line is max(2.5, own BE)). Missing -> {} (line 2.5)."""
    import json as _json
    try:
        raw = os.environ.get('PROVEN_BE_JSON')
        if raw:
            data = _json.loads(raw)
        else:
            with open(BE_SNAPSHOT, encoding='utf-8') as f:
                data = _json.load(f)
        if isinstance(data, dict) and isinstance(data.get('be'), dict):
            data = data['be']
        if not isinstance(data, dict):
            raise ValueError('not a JSON object')
    except FileNotFoundError:
        return {}
    except Exception as ex:
        print(f"  !! WARNING: break-even list unreadable ({type(ex).__name__}) - Proven line = {CHAMPION_LINE} for every product")
        return {}
    out, bad = {}, 0
    for k, v in data.items():               # one bad value skips that product only, never the whole list
        try:
            f = float(v)
            if not (0 < f < float('inf')): raise ValueError
            out[str(k)] = f
        except Exception:
            bad += 1
    if bad:
        print(f"  !! WARNING: {bad} unreadable break-even value(s) skipped - those products use the {CHAMPION_LINE} line")
    return out

def champion_run(feed, run_date, dry, life=None):
    """PROVEN tier, ladder v7 (owner 2026-10-01/02) - PMax | Champions | UK, tROAS 2.8, £300/day.
      RESTART   v7.5: a product that sold its way out of Last Chance (lc_grad: stamp) is judged on the sales from that sale on
                only, in every rule below - like a new Testing graduate.
      PROMOTE   4+ lifetime orders AND last-4 ROAS >= max(2.5, own break-even): revenue of the last 4 sales over the
                spend (Rising + Proven + Last Chance) since the sale before them (no 5th-last sale: since the first sale).
                After a demotion: 2+ sales dated after the champ_demoted: stamp AND ROAS >= 2.5 on the spend since it,
                AND the last-4 test above (so a re-entry is not demoted again on the very next run).
      DEMOTE    that same last-4 spend > last-4 revenue / max(2.5, own break-even) -> back to Rising (label w_campaign),
                stamped champ_demoted:DATE. Judged for every Proven product, whatever its order count. A product moved
                down is judged by the Rising pace rule IN THE SAME RUN (no fresh room), so it can reach Last Chance at once.
      ROUTING   label only (custom_label_1 = c_champion; Champions tree includes it, Testing trees exclude it). Writes go
                LABEL FIRST, then the tag, then the stamp - a half-failed write is retried by the next run instead of
                leaving a tagged product serving under its old label. A promotion also removes any Winners item-id node.
      GUARDS    promotion cap (skips promotions only) and demotion cap (skips demotions only); an empty orders pull skips
                the section; any error warns and skips it - the Testing kills, pace rule and Last Chance still run.
      PRIVACY   the repo and its Actions logs are public: nothing printed to stdout lets anyone work out a product's own
                break-even (figures go to the private Telegram chat only).
    Returns res with: roster, promoted, demoted, flagged, watch, err, warn, spend (pid -> rows, shared with the pace
    rule), skip_pace (promoted this run) and pace_exclude (the exact Proven set after this run's moves - the pace rule uses
    it instead of a tag search; None when the section did not finish, so the pace rule falls back to its own search)."""
    res = dict(roster=0, promoted=[], demoted=[], flagged=[], watch=[], err=None, warn=None, spend=None, skip_pace=set(),
               pace_exclude=None)
    try:
        tok = shopify_token()
        active = {str(p['pid']): p for p in feed}
        champs = {pid: p for pid, p in active.items() if CHAMPION_TAG in p['tags']}
        winners = {pid: p for pid, p in active.items() if WINNER_TAG in p['tags'] and CHAMPION_TAG not in p['tags']}
        res['roster'] = len(champs)
        if life:                                    # main() already pulled lifetime sales this run - reuse it
            sales, n_orders = life, len(life)
        else:
            sales, n_orders = _lifetime_sales(tok)
        if n_orders == 0:
            res['err'] = 'lifetime orders pull returned 0 — glitch; champion moves skipped'
            return res
        be = _load_breakeven()
        if not be:
            res['warn'] = 'break-even list missing (PROVEN_BE_JSON) — every Proven line is 2.5 this run'
        line = lambda pid: max(CHAMPION_LINE, be.get(pid, 0.0))   # noqa: E731

        # spend window: back to the oldest date any judged product needs (5th-last sale, first sale, or stamp), 60..200 days
        need = []
        # v7.5: the sales history each rule judges - since the restart stamp for products that sold their way out of Last Chance
        hist = {pid: _since_reset(sales.get(pid, []), _lc_reset_date(active[pid]['tags'])) for pid in list(champs) + list(winners)}
        for pid in list(champs) + [q for q in winners if len(hist[q]) >= CHAMPION_ENTRY_ORDERS]:
            sl = hist[pid]
            if sl:
                need.append(sl[-5]['date'] if len(sl) >= 5 else sl[0]['date'])
            dem0 = _demoted_date(active[pid]['tags'])
            if dem0:
                need.append(dem0)
        days = 60
        if need:
            days = (run_date - datetime.date.fromisoformat(min(need))).days + 1
        days = max(WINNER_LOOKBACK_D, min(PROVEN_LOOKBACK_D, days))
        spend = _tier_daily_spend(run_date, set(winners) | set(champs), days)
        res['spend'] = spend
        res['spend_pids'] = set(winners) | set(champs)

        def last4(pid, slist):
            rows = spend.get(pid, ())
            if len(slist) >= 5:
                anchor = slist[-5]['date']
                spent = sum(v for d, v in rows if d > anchor)                  # the day AFTER the 5th-last sale
            elif slist:
                anchor = None                                                  # v7.3: 1-4 sales -> since the first sale
                spent = sum(v for d, v in rows if d >= slist[0]['date'])       # (never older spend from the shared window)
            else:
                anchor = None
                spent = sum(v for d, v in rows)                                # no sale on record: the whole window
            return spent, sum(x['rev'] for x in slist[-4:]), anchor

        def passes_last4(pid, slist):
            spent, rev, _a = last4(pid, slist)
            return spent <= 0 or rev / spent >= line(pid)

        # ---- PROMOTIONS ----
        cands = []
        for pid, p in winners.items():
            slist = hist[pid]                                                  # v7.5: new sales only after Last Chance
            if len(slist) < CHAMPION_ENTRY_ORDERS: continue
            dem = _demoted_date(p['tags'])
            if dem:
                post = [x for x in slist if x['date'] > dem]
                if len(post) < CHAMPION_REPROMOTE_SALES: continue
                spent = sum(v for d, v in spend.get(pid, ()) if d > dem)
                if spent > 0 and sum(x['rev'] for x in post) / spent < CHAMPION_REENTRY_ROAS: continue
            if not passes_last4(pid, slist): continue
            cands.append((pid, p, len(slist), dem))
        log_rows = []; ts = datetime.datetime.now(UK).strftime('%Y-%m-%d %H:%M:%S')
        if len(cands) > CHAMPION_PROMOTE_CAP:
            res['err'] = (f'SAFETY STOP: {len(cands)} promotions > cap {CHAMPION_PROMOTE_CAP} — '
                          f'looks like a data glitch; NO promotions this run (demotions still checked)')
            cands = []
        for pid, p, n_life, dem in sorted(cands, key=lambda c: -c[2]):
            if dry:
                out = 'DRY'
            else:
                r1 = shopify_set_label_metafield(tok, pid, CHAMPION_TAG)          # label first: routing follows it
                r2 = shopify_add_tag(tok, pid, CHAMPION_TAG) if r1 == 'ok' else 'skipped'
                if r1 == 'ok' and r2 != 'ok':          # v7.4: read the tags before any rollback - the write may have landed
                    now = _product_tags(tok, pid)
                    if now is not None and CHAMPION_TAG in now:
                        r2 = 'ok'                                              # it landed after all: the move is complete
                    elif now is not None:                                      # definitely not tagged: roll the label back
                        r2 = f"{r2} (label rolled back: {shopify_set_label_metafield(tok, pid, WINNER_TAG)})"
                    else:
                        r2 = f"{r2} (tags unreadable - label kept, next run repairs)"
                if r1 == 'ok' and r2 == 'ok':
                    for t in [t for t in p['tags'] if str(t).startswith(CHAMPION_DEMOTED_PREFIX)]:
                        shopify_remove_tag(tok, pid, t)      # clean re-entry (only once the move itself is written)
                    p['tags'].append(CHAMPION_TAG)
                out = 'ok' if (r1 == 'ok' and r2 == 'ok') else f'label {r1} / tag {r2}'
            res['promoted'].append(dict(pid=pid, name=p['name'], orders=n_life, re=bool(dem), outcome=out))
            if out in ('ok', 'DRY') or 'tags unreadable' in out:    # unreadable: it may already be Proven - keep it out
                res['skip_pace'].add(pid)                         # of the pace rule this run (the next run settles it)
            log_rows.append([ts, run_date.isoformat(), 'RE-PROMOTE' if dem else 'PROMOTE', pid, p['name'], n_life, '', '', out])
            print(f"  {'would promote' if dry else 'promote'} {'(re) ' if dem else ''}to Proven {pid} ({n_life} orders"
                  f"{' since Last Chance' if _lc_reset_date(p['tags']) else ''}) -> {out} | {p['name'][:40]}")
        if not dry:
            ok = [x['pid'] for x in res['promoted'] if x['outcome'] == 'ok']
            if ok: ads_remove_winner_item_nodes(ok)   # label routes it; an item-id node would keep it in Rising too

        # ---- DEMOTIONS: last 4 sales under max(2.5, own BE) on all-tier spend ----
        flagged = []
        for pid, p in champs.items():
            slist = hist[pid]                                                  # v7.5: same history as at entry
            spent, rev, anchor = last4(pid, slist)
            allow = rev / line(pid)
            pct = (spent / allow * 100) if allow > 0 else (100.0 if spent > 0 else 0.0)
            row = dict(pid=pid, name=p['name'], allow=allow, spent=spent, pct=pct,
                       opened=f"last-4 £{rev:.2f}, spend since {anchor or 'the first sale'}")   # Telegram only (private)
            if spent > allow: flagged.append(row)
            elif pct >= 60: res['watch'].append(row)
        res['watch'].sort(key=lambda x: -x['pct']); res['watch'] = res['watch'][:5]
        if len(flagged) > CHAMPION_DEMOTE_CAP:
            msg = (f'SAFETY STOP: {len(flagged)} demotions > cap {CHAMPION_DEMOTE_CAP} — '
                   f'looks like a data glitch; NO demotions this run')
            res['err'] = (res['err'] + ' | ' + msg) if res['err'] else msg
            flagged = []
        res['flagged'] = flagged
        for r in flagged:
            pid = r['pid']; p = champs[pid]
            if dry:
                r['outcome'] = 'DRY'
            else:
                r1 = shopify_set_label_metafield(tok, pid, WINNER_TAG)            # label first: routing follows it
                r2 = shopify_remove_tag(tok, pid, CHAMPION_TAG) if r1 == 'ok' else 'skipped'
                if r1 == 'ok' and r2 != 'ok':          # v7.4: read the tags before any rollback - the write may have landed
                    now = _product_tags(tok, pid)
                    if now is not None and CHAMPION_TAG not in now:
                        r2 = 'ok'                                              # it landed after all: the move is complete
                    elif now is not None:                                      # definitely still tagged: roll the label back
                        r2 = f"{r2} (label rolled back: {shopify_set_label_metafield(tok, pid, CHAMPION_TAG)})"
                    else:
                        r2 = f"{r2} (tags unreadable - label kept, next run repairs)"
                r3 = shopify_add_tag(tok, pid, f"{CHAMPION_DEMOTED_PREFIX}{run_date.isoformat()}") if r2 == 'ok' else 'skipped'
                r['outcome'] = 'ok' if (r1 == r2 == r3 == 'ok') else f'label {r1} / tag {r2} / stamp {r3}'
                r['moved'] = (r1 == 'ok' and r2 == 'ok')   # out of Proven even if only the stamp failed
                if r2 == 'ok' and CHAMPION_TAG in p['tags']: p['tags'].remove(CHAMPION_TAG)
            res['demoted'].append(r)
            log_rows.append([ts, run_date.isoformat(), 'DEMOTE', pid, p['name'], len(hist[pid]),
                             round(r['allow'], 2), round(r['spent'], 2), r['outcome']])
            print(f"  {'would demote' if dry else 'demote'} Proven {pid} (last 4 sales under its line) -> {r['outcome']} | {p['name'][:40]}")
        if not dry and log_rows: _write_champion_log(log_rows)
        # v7.3: the EXACT Proven set for the pace rule - feed tags + this run's moves (Shopify's tag search can lag)
        moved_in = {x['pid'] for x in res['promoted'] if x['outcome'] in ('ok', 'DRY') or 'tags unreadable' in str(x['outcome'])}
        moved_out = {x['pid'] for x in res['demoted'] if x.get('outcome') == 'DRY' or x.get('moved')}
        res['pace_exclude'] = (set(champs) | moved_in) - moved_out
        return res
    except Exception as ex:
        res['err'] = f'champion rule error (section skipped; other kills unaffected): {type(ex).__name__}: {str(ex)[:120]}'
        return res

def build_report(rows, outcomes, run_date, ts, n_active, n_kills, n_drafted, dry):
    wb = Workbook()
    s = wb.active; s.title = 'Summary'
    s.append(['PMax — Google Auto-Kill Run Report'])
    s.append(['Run (UK time)', ts])
    s.append(['Data date', str(run_date), run_date.strftime('%A')])
    s.append(['Mode', 'DRY-RUN (nothing drafted)' if dry else 'LIVE (products drafted)'])
    s.append(['Active products analyzed', n_active])
    s.append(['Kills found by rules', n_kills])
    s.append(['Products drafted', n_drafted])
    tiers = collections.Counter(t for (_, t, _) in rows)
    s.append(['By tier'] + [f'{k}: {v}' for k, v in sorted(tiers.items())])
    s.append([])
    s.append(['Revenue is GROSS (refunds never deducted). Cost/clicks from Google Ads; revenue from Shopify; windows UK-aligned.'])

    d = wb.create_sheet('Drafted')
    d.append(['Product ID', 'Product Name', 'Kill Tier', 'Reason', 'days_live',
              'cost_7d', 'cost_30d', 'clicks_30d', 'rev_30d', 'rev_14d', 'rev_7d', 'roas_7d', 'outcome'])
    for p, tier, why in rows:
        d.append([int(p['pid']), p['name'], tier, why, _days_live(p, run_date),
                  round(p['cost7'], 2), round(p['cost30'], 2), p['clicks30'],
                  round(p['rev30'], 2), round(p['rev14'], 2), round(p['rev7'], 2),
                  round(p['roas7'], 2), outcomes.get(p['pid'], '')])
    for c in d['A'][1:]: c.number_format = '0'

    fname = f"auto_kill_report_{run_date}_{datetime.datetime.now(UK).strftime('%H%M')}.xlsx"
    wb.save(fname)
    return fname

def send_report(subject, body, xlsx_path=None):
    if not (RESEND_API_KEY and EMAIL_TO):
        print("!! EMAIL NOT SENT — set the RESEND_API_KEY + EMAIL_TO secrets.")
        if xlsx_path: print(f"   report saved locally: {xlsx_path}")
        return False
    try:
        payload = {'from': RESEND_FROM, 'to': [EMAIL_TO], 'subject': subject, 'text': body}
        if xlsx_path:
            with open(xlsx_path, 'rb') as f:
                payload['attachments'] = [{'filename': os.path.basename(xlsx_path),
                                           'content': base64.b64encode(f.read()).decode()}]
        r = requests.post('https://api.resend.com/emails',
                          headers={'Authorization': f'Bearer {RESEND_API_KEY}'}, json=payload, timeout=30)
        if r.status_code in (200, 201):
            print(f'Email sent to {EMAIL_TO} via Resend: "{subject}"')
            return True
        print(f"!! EMAIL FAILED (Resend {r.status_code}): {r.text[:200]}")
        return False
    except Exception as ex:
        print(f"!! EMAIL FAILED: {ex}   (report saved: {xlsx_path})")
        return False

def send_telegram(text, xlsx_path=None):
    """Every-run push: a text summary + the .xlsx as a document. No daily cap on Telegram."""
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT):
        print("!! TELEGRAM NOT SENT — set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID (env or _secrets_local.py).")
        return False
    base = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"; ok = False
    try:
        r = requests.post(f"{base}/sendMessage",
                          data={'chat_id': TELEGRAM_CHAT, 'text': text, 'parse_mode': 'HTML'}, timeout=30)
        ok = (r.status_code == 200)
        if not ok: print(f"!! Telegram message failed ({r.status_code}): {r.text[:200]}")
        if xlsx_path and os.path.exists(xlsx_path):
            with open(xlsx_path, 'rb') as f:
                rd = requests.post(f"{base}/sendDocument",
                                   data={'chat_id': TELEGRAM_CHAT}, files={'document': f}, timeout=60)
            if rd.status_code != 200: print(f"!! Telegram document failed ({rd.status_code}): {rd.text[:200]}")
        if ok: print(f"Telegram sent to chat {TELEGRAM_CHAT}.")
    except Exception as ex:
        print(f"!! TELEGRAM FAILED: {ex}")
    return ok

def _kills_last_12h():
    """Rows actually DRAFTED (outcome 'ok') in the last 12h, from kills_log_auto.csv."""
    if not os.path.exists(KILLS_LOG): return []
    cutoff = datetime.datetime.now(UK) - datetime.timedelta(hours=12); out = []
    with open(KILLS_LOG, encoding='utf-8') as f:
        for row in csv.DictReader(f):
            try: t = datetime.datetime.fromisoformat(row['timestamp']).replace(tzinfo=UK)
            except Exception: continue
            if t >= cutoff and row.get('outcome') == 'ok': out.append(row)
    return out

def build_12h_report(rows, ts):
    wb = Workbook(); s = wb.active; s.title = '12h drafted'
    s.append(['Auto-Kill — 12h digest', ts]); s.append(['Products drafted (12h)', len(rows)])
    s.append(['By tier'] + [f'{k}: {v}' for k, v in sorted(collections.Counter(r['tier'] for r in rows).items())])
    s.append([]); s.append(['timestamp', 'product_id', 'name', 'tier', 'reason', 'cost_30d', 'clicks_30d', 'roas_7d'])
    for r in rows:
        s.append([r['timestamp'], int(r['product_id']), r['name'], r['tier'], r['reason'],
                  r['cost_30d'], r['clicks_30d'], r['roas_7d']])
    fname = f"auto_kill_12h_{datetime.datetime.now(UK).strftime('%Y-%m-%d_%H%M')}.xlsx"; wb.save(fname); return fname

def maybe_send_12h_email(ts, force=False):
    """Twice a day (SUMMARY_HOURS) email a TEXT digest of the last 12h kills + the .xlsx."""
    now = datetime.datetime.now(UK)
    if not (force or (now.hour in SUMMARY_HOURS and now.minute < 8)): return
    rows = _kills_last_12h()
    tiers = collections.Counter(r['tier'] for r in rows)
    lines = [f"- {r['product_id']} [{r['tier']}] {r['name'][:44]} | £{r['cost_30d']}/30d, {r['clicks_30d']} clk"
             for r in rows[:50]]
    body = (f"Auto-Kill — 12-hour digest\n{ts} (UK)\n\n"
            f"PRODUCTS DRAFTED (last 12h): {len(rows)}\n"
            f"By tier: {dict(tiers) or '-'}\n\n"
            + ("\n".join(lines) if lines else "(nothing drafted in the last 12 hours)")
            + (f"\n...and {len(rows)-50} more" if len(rows) > 50 else "")
            + "\n\n(Full list also in the attached Excel.)")
    send_report(f"12h Auto-Kill — {len(rows)} drafted", body, build_12h_report(rows, ts) if rows else None)

def _write_kills_log(rows, outcomes, run_date, ts, dry):
    new = not os.path.exists(KILLS_LOG)
    with open(KILLS_LOG, 'a', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        if new:
            w.writerow(['timestamp', 'data_date', 'mode', 'product_id', 'name', 'tier', 'reason', 'days_live',
                        'cost_7d', 'cost_30d', 'clicks_30d', 'rev_30d', 'rev_14d', 'rev_7d', 'roas_7d', 'outcome'])
        mode = 'DRY' if dry else 'LIVE'
        for p, tier, why in rows:
            w.writerow([ts, run_date, mode, p['pid'], p['name'], tier, why, _days_live(p, run_date),
                        round(p['cost7'], 2), round(p['cost30'], 2), p['clicks30'],
                        round(p['rev30'], 2), round(p['rev14'], 2), round(p['rev7'], 2),
                        round(p['roas7'], 2), outcomes.get(p['pid'], '')])

def main():
    dry = '--dry' in sys.argv or '--dry-run' in sys.argv
    run_date = datetime.datetime.now(UK).date()
    is_monday = (run_date.weekday() == 0)
    ts = datetime.datetime.now(UK).strftime('%Y-%m-%d %H:%M:%S')

    logf = open(RUN_LOG, 'a', encoding='utf-8')
    logf.write(f"\n{'#'*72}\n# AUTO RUN {ts} (UK) | GOOGLE-DIRECT | {'DRY' if dry else 'LIVE'} | data-date {run_date}\n{'#'*72}\n")
    real = sys.stdout; sys.stdout = _Tee(real, logf)
    try:
        print(f"== kill_engine GOOGLE AUTO == {run_date} ({run_date.strftime('%A')}) | "
              f"Mon tiers 3&4 {'ON' if is_monday else 'OFF'} | {'DRY-RUN' if dry else 'LIVE'} | {ts}")
        feed = build_feed(run_date)
        print(f"feed: Google Ads + live Shopify -> {len(feed)} active products")

        # GLITCH GUARD: a live store ALWAYS has some Shopify revenue over 30 days. If the orders
        # pull comes back empty (£0 across EVERY product) it's a data glitch (failed/empty Shopify
        # response), not reality — every spending product would falsely read £0-revenue and get
        # killed. So abort + alert and draft NOTHING. This caps nothing real: a legit big batch
        # still has normal order data; only the empty-data glitch trips it.
        total_rev30 = sum(p.get('rev30', 0) for p in feed)
        if total_rev30 <= 0:
            msg = (f"ABORTED — Shopify returned £0 revenue across ALL {len(feed)} active products over 30 days. "
                   f"That's an empty/failed orders pull, not real sales. NOTHING was drafted; re-run after Shopify recovers.")
            print("!! " + msg)
            send_telegram(f"⚠️ <b>Auto-Kill ABORTED</b>\n{ts} UK\n{msg}")
            send_report("ALERT: auto-kill ABORTED — no Shopify orders data",
                        f"PMax auto-kill — {ts} (UK)\nMode: {'DRY-RUN' if dry else 'LIVE'}\n\n{msg}")
            return

        # ONE lifetime-sales pull per run (audit 2026-08-16), shared by the entry gate,
        # the LC rule and the once-sold shield. Fail-open: on error every consumer falls
        # back to its own behaviour (gate re-pulls; shield approximates with rev30).
        life_sales = None
        try:
            life_sales, _lo = _lifetime_sales(shopify_token())
        except Exception as _lx:
            print(f"  (lifetime pull warn: {str(_lx)[:90]})")
        for p in feed:   # once-sold shield: no-sale tiers must never draft a product with ANY sale
            p['ever_sold'] = bool(life_sales.get(str(p['pid']))) if life_sales is not None \
                             else p['rev30'] > 0
        # WINNERS first: tag new 2+ lifetime-order actives, then EXEMPT all tagged from the kill rules
        new_winners = tag_new_winners(feed, dry, life_sales)
        exempt = sum(1 for p in feed if WINNER_TAG in p['tags'])
        print(f"winners: +{len(new_winners)} newly tagged | {exempt} total exempt from kill rules")
        fp_err = None
        if new_winners and not dry:
            _, fp_err = ads_fast_path(new_winners, shopify_token())   # instant campaign move (label = backup)

        # BEST SELLERS: every active winner belongs to the storefront collection (add-only)
        bs_added, bs_err = sync_bestseller_collection(shopify_token(), dry)

        # CHAMPIONS TIER (2026-07-20): promote 3-lifetime-order winners into the Champions
        # campaign; demote champions whose trailing 2-gap window fell below 2.0. Runs BEFORE
        # the winner pace rule so fresh promotions are already champion-exempt this same run.
        if CHAMPIONS_ENABLED:
            ch = champion_run(feed, run_date, dry, life_sales)
            print(f"champions: roster {ch['roster']} | promoted {len(ch['promoted'])} | "
                  f"demoted {len(ch['demoted'])}" + (f" | !! {ch['err']}" if ch['err'] else ""))
            if ch.get('warn'): print(f"  !! {ch['warn']}")
            for r in ch['watch']:   # ladder v7.1: figures only in the private Telegram chat (public log must not reveal a break-even)
                print(f"  champion watch {r['pct']:.0f}%: {r['pid']} | {r['name'][:40]}")
        else:
            ch = dict(roster=0, promoted=[], demoted=[], flagged=[], watch=[], err=None)
            print("champions: DISABLED (campaign paused 2026-08-07 — roster folded into Winners)")

        # SERVING-STATE JANITOR (audit 2026-08-16): heal missed node sweeps + draft tags
        js = reconcile_serving_state(dry, extra_roster={str(p['pid']) for p in (new_winners or [])})
        _now_uk = datetime.datetime.now(UK)   # v7.3: once a day (first run of 04:00-04:09 UK) at the 8-min cadence
        if (_now_uk.hour == PRUNE_HOUR_UK and _now_uk.minute < 10) or _TREE_NODES_SEEN > PRUNE_TRIGGER_NODES or '--prune' in sys.argv:
            pj = prune_settled_nodes(dry)
            print(f"fast-path hygiene: {pj['checked']} item-id node(s) checked | {pj['removed']} {'would be ' if dry else ''}removed (label has taken over) | "
                  f"{pj['kept']} kept | biggest tree {_TREE_NODES_SEEN} nodes" + (f" | !! {pj['err']}" if pj['err'] else ""))
        print(f"janitor: {js['nodes_swept']} stray Winners node(s) swept | "
              f"{js['drafts_stripped']} draft w_campaign strip(s)"
              + (f" | !! {js['err']}" if js['err'] else ""))

        # WINNER PACE RULE (v11): judge the Winners campaign at 2.8-pace
        w = winner_pace_run(run_date, dry, life_sales, shared=(ch.get('spend_pids') or set(), ch.get('spend')),
                            skip=ch.get('skip_pace'), exclude=ch.get('pace_exclude'))
        print(f"winner pace ({WINNER_PACE_ROAS}): {w['evaluated']} evaluated | "
              f"{len(w['flagged'])} over allowance | killed {w['killed']} | "
              f"{'LIVE' if w['live'] else ('DRY' if dry else 'dormant -> live ' + WINNER_KILL_START.isoformat())}"
              + (f" | !! {w['err']}" if w['err'] else ""))
        for r in w['closest']:
            print(f"  pace watch {r['pct']:.0f}%: {r['pid']} spent £{r['spent']:.2f} of £{r['allow']:.2f} ({r['opened']}) | {r['name'][:40]}")

        # LAST CHANCE rule (owner 2026-08-13): graduate/exit the lc pool
        lc = lc_run(run_date, dry, life_sales)
        print(f"last chance: pool {lc['pool']} | graduated {len(lc['graduated'])} | "
              f"drafted {len(lc['drafted'])}" + (f" | !! {lc['err']}" if lc['err'] else ""))

        # FEED LABEL SYNC GUARD (v7.6): once an hour, re-send any product whose label Google has not taken after 2 h
        try:
            ls = label_sync_check(feed, dry, force=('--labels' in sys.argv))
        except Exception as _lx:
            ls = dict(checked=0, syncing=[], waiting=[], resent=[], stuck_long=[], label_wrong=[], cleaned=0, err=str(_lx)[:120])
        if ls is not None:
            print(f"feed labels: {ls['checked']} checked | {len(ls['syncing'])} syncing (< {LABEL_STUCK_H:g} h) | {len(ls['resent'])} "
                  f"{'would be ' if dry else ''}re-sent | {len(ls['waiting'])} re-sent earlier, waiting | {len(ls['label_wrong'])} Shopify label != tags"
                  + (f" | cleaned {ls['cleaned']}" if ls['cleaned'] else "") + (f" | !! {ls['err']}" if ls['err'] else ""))
            for r in ls['resent'] + ls['waiting'] + ls['label_wrong']:
                kind = 're-send' if r in ls['resent'] else ('waiting' if r in ls['waiting'] else 'LABEL != TAGS')
                print(f"  label {kind} {r['pid']} should be {r['want']} | {r['wrong']}/{r['offers']} offers wrong at Google | "
                      f"label written {(r['label_age'] or 0):.1f} h ago | {r['name'][:40]}")

        kills = []
        for p in feed:
            if WINNER_TAG in p['tags'] or LC_TAG in p['tags']:
                continue                      # winners + last-chance: their own rules — never killed here
            dec, tier, why = evaluate(p, run_date, is_monday)
            if dec == 'KILL':
                kills.append((p, tier, why))

        # no cap — draft EVERY product the rules flag
        to_draft = kills
        print(f"kills found: {len(kills)}")
        outcomes = {}
        if to_draft:
            wtok = None if dry else shopify_token()
            for p, tier, why in to_draft:
                res = 'DRY (not drafted)' if dry else shopify_draft(wtok, p['pid'])
                outcomes[p['pid']] = res
                print(f"  {'would draft' if dry else 'draft'} {p['pid']} [{tier}] -> {res} | {p['name'][:42]}")
        drafted = 0 if dry else sum(1 for v in outcomes.values() if v == 'ok')

        xlsx = build_report(to_draft, outcomes, run_date, ts, len(feed), len(kills), drafted, dry)
        print(f"report: {xlsx}")

        # log FIRST so the 12h digest can include this run's kills
        _write_kills_log(to_draft, outcomes, run_date, ts, dry)
        print(f"logs: run -> {RUN_LOG} | kills -> {KILLS_LOG}")

        # TELEGRAM — every run: run stats + the .xlsx
        n = len(to_draft) if dry else drafted
        tg = (f"🤖 <b>Auto-Kill</b> — {n} drafted{' (DRY)' if dry else ''}\n"
              f"{ts} UK · {run_date.strftime('%a')}\n"
              f"Active: {len(feed)} | winners exempt: {exempt} | kills found: {len(kills)} | drafted: {n}")
        if new_winners:
            tg += ("\n🏆 <b>new winners → w_campaign:</b> "
                   + ", ".join(f"<code>{p['pid']}</code>" for p in new_winners[:10])
                   + (f" +{len(new_winners)-10} more" if len(new_winners) > 10 else ""))
            tg += ("\n⚡ moved to Winners campaign instantly" if not fp_err
                   else f"\n⚠️ fast-path failed ({html.escape(fp_err[:80])}) — label moves it on next feed sync")
        if to_draft:
            DETAIL = 15                                     # full reason+metrics for up to 15; rest in the Excel
            tg += "\n\n" + "\n\n".join(_fmt_kill(p, tier, why, run_date) for p, tier, why in to_draft[:DETAIL])
            if len(to_draft) > DETAIL:
                tg += f"\n\n…+{len(to_draft)-DETAIL} more — full reasons &amp; metrics in the attached Excel."

        # WINNERS pace section — one status line every run + detail per kill/preview
        tg += (f"\n\n🩹 <b>Last Chance</b>: pool {lc['pool']} | ⬆ {len(lc['graduated'])} back to Winners | "
               f"🪦 {len(lc['drafted'])} drafted" + (f" | ⚠ {lc['err']}" if lc['err'] else ""))
        for g in lc['graduated'][:5]:
            tg += f"\n  ⬆ <code>{g['pid']}</code> {html.escape(g['name'][:40])}"
        for g in lc['drafted'][:5]:
            tg += f"\n  🪦 <code>{g['pid']}</code> {html.escape(g['name'][:40])}"
        tg += (f"\n\n🎯 <b>Winners pace {WINNER_PACE_ROAS}</b>: {w['evaluated']} checked, "
               f"{len(w['flagged'])} over allowance"
               + ("" if w['live'] else (" (DRY)" if dry else f" — dormant, live {WINNER_KILL_START.strftime('%d %b')}")))
        for r in w['flagged'][:8]:
            tg += (f"\n🔻 <b>{html.escape(r['name'][:42])}</b> <code>{r['pid']}</code>\n"
                   f"   spent £{r['spent']:.2f} &gt; allowance £{r['allow']:.2f} ({html.escape(r['opened'])})"
                   + (f" → {html.escape(str(r.get('outcome', '')))}" if w['live'] else " → would kill"))
        if len(w['flagged']) > 8:
            tg += f"\n   …+{len(w['flagged']) - 8} more — see winner_kills_log.csv"
        if w['err']:
            tg += f"\n⚠️ {html.escape(w['err'])}"

        # CHAMPIONS section — roster + every move, every run (single line while disabled)
        if not CHAMPIONS_ENABLED:
            tg += "\n\n👑 Champions: disabled (paused 2026-08-07, folded into Winners)"
        else:
            ch_now = (ch['roster'] + sum(1 for x in ch['promoted'] if x['outcome'] in ('ok', 'DRY'))
                      - sum(1 for x in ch['demoted'] if x.get('outcome') in ('ok', 'DRY') or x.get('moved')))
            tg += (f"\n\n👑 <b>Proven (Champions, tROAS 2.8; out when the last 4 sales fall under {CHAMPION_LINE} or own BE)</b>: "
                   f"roster {ch_now} | promoted {len(ch['promoted'])} | demoted {len(ch['demoted'])}")
        for x in ch['promoted'][:10]:
            tg += (f"\n⬆️ <b>{html.escape(x['name'][:42])}</b> <code>{x['pid']}</code> — "
                   f"{x['orders']} lifetime orders{' (re-promoted)' if x['re'] else ''}"
                   + (f" → {html.escape(str(x['outcome']))}" if x['outcome'] not in ('ok', 'DRY') else ''))
        for r in ch['demoted'][:10]:
            tg += (f"\n⬇️ <b>{html.escape(r['name'][:42])}</b> <code>{r['pid']}</code>\n"
                   f"   spent £{r['spent']:.2f} &gt; allowance £{r['allow']:.2f} "
                   f"({html.escape(r['opened'])}) → "
                   + ("back to Rising" if (r.get('outcome') in ('ok', 'DRY') or r.get('moved'))
                      else f"NOT moved: {html.escape(str(r.get('outcome'))[:120])}"))   # cut BEFORE escaping (never split an entity)
        for r in ch['watch'][:3]:
            tg += (f"\n👀 champion watch {r['pct']:.0f}%: <code>{r['pid']}</code> "
                   f"£{r['spent']:.2f} of £{r['allow']:.2f}")
        if ch['err']:
            tg += f"\n⚠️ {html.escape(ch['err'])}"
        if ch.get('warn'):
            tg += f"\n⚠️ {html.escape(ch['warn'])}"
        if ls is not None and (ls['resent'] or ls['stuck_long'] or ls['label_wrong'] or ls['err']):
            tg += (f"\n\n🏷 <b>Feed labels</b>: {len(ls['resent'])} re-sent to Google | {len(ls['waiting'])} waiting | "
                   f"{len(ls['syncing'])} still syncing (&lt; {LABEL_STUCK_H:g} h)")
            for r in ls['resent'][:8]:
                tg += f"\n  ↻ <code>{r['pid']}</code> {html.escape(r['name'][:40])} - Google still not {r['want']} after {r['label_age']:.0f} h"
            for r in ls['stuck_long'][:5]:
                tg += f"\n  ⚠️ stuck {r['label_age']:.0f} h: <code>{r['pid']}</code> {html.escape(r['name'][:40])}"
            for r in ls['label_wrong'][:5]:
                tg += f"\n  ⚠️ Shopify label disagrees with its tags: <code>{r['pid']}</code> {html.escape(r['name'][:40])}"
            if ls['err']:
                tg += f"\n⚠️ {html.escape(ls['err'])}"
        if bs_added:
            tg += f"\n🛍️ Best Sellers collection: +{bs_added} winner(s) added"
        if bs_err:
            tg += f"\n⚠️ {html.escape(bs_err)}"
        if w['live'] and w['killed'] and w['pool'] < WINNER_POOL_ALERT:
            tg += (f"\n⚠️ <b>WINNER POOL LOW: {w['pool']} left</b> (&lt;{WINNER_POOL_ALERT}) — "
                   f"consider cutting the Winners £100/day budget (your manual call).")

        # offer-slot guard: note if anything drafted this run carried a pp_offer_* tag
        # (smart collections drop it instantly; the nightly rotation refills the pool)
        pp_hit = [p['pid'] for p, _, _ in to_draft
                  if outcomes.get(p['pid']) == 'ok' and any(t.startswith('pp_offer') for t in p.get('tags', []))]
        wk = [str(r['pid']) for r in w['flagged'] if w['live'] and str(r.get('outcome')) == 'ok']
        if wk:
            try:
                ids = ','.join(f'"gid://shopify/Product/{p}"' for p in wk)
                jj = requests.post(f"https://{SHOP}/admin/api/{SHOP_API}/graphql.json",
                                   headers={'X-Shopify-Access-Token': shopify_token(),
                                            'Content-Type': 'application/json'},
                                   json={'query': '{nodes(ids:[%s]){... on Product{legacyResourceId tags}}}' % ids},
                                   timeout=30).json()
                pp_hit += [str(n['legacyResourceId']) for n in ((jj.get('data') or {}).get('nodes') or [])
                           if n and any(t.startswith('pp_offer') for t in n.get('tags', []))]
            except Exception:
                pass
        if pp_hit:
            tg += ("\n🎁 <b>upsell-offer product drafted:</b> "
                   + ", ".join(f"<code>{p}</code>" for p in pp_hit)
                   + " — pool self-heals, refills tonight")
        send_telegram(tg, xlsx)

        # RESEND email — twice a day (SUMMARY_HOURS): TEXT digest of last 12h + the .xlsx
        maybe_send_12h_email(ts, force=('--test' in sys.argv or '--email' in sys.argv))
    except Exception:
        import traceback
        err = traceback.format_exc()
        quota = any(sig in err for sig in
                    ('GOOGLE_QUOTA_EXHAUSTED', 'RESOURCE_EXHAUSTED', '429 Client Error'))
        if quota:
            # Google Ads DEVELOPER-token daily op quota spent (Basic Access 15k/day,
            # resets 00:00 PT = 08:00 UK BST). Not a fault: skip quietly, next grid
            # tick retries. Shopify state untouched; no kills were needed to be safe.
            print("!! run skipped — Google Ads daily API quota exhausted:\n" + err[-300:])
            try:
                send_telegram(f"⏸ <b>Auto-Kill SKIPPED</b> — Google Ads daily API quota exhausted\n"
                              f"{ts} UK\nResets 08:00 UK; next grid tick retries. Shopify untouched.")
            except Exception:
                pass
        else:
            print("!! AUTO-KILL RUN FAILED — nothing further drafted:\n" + err)
            try:
                send_telegram(f"❌ <b>Auto-Kill FAILED</b>\n{ts} UK\n<pre>{err[-500:]}</pre>")
                send_report(f"AUTO-KILL FAILED — {run_date}",
                            f"auto-kill run FAILED at {ts} (UK).\n\nError:\n{err}")
            except Exception:
                pass
    finally:
        sys.stdout = real
        logf.close()

if __name__ == '__main__':
    main()
