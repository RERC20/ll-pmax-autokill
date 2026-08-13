# -*- coding: utf-8 -*-
# OWNER-APPROVED (chat, 2026-08-13): build 'PMax | Last Chance | UK' — the scavenger
# campaign for pace-killed ex-winners. MCV tROAS 2.4, £20/day, UK PRESENCE-ONLY geo
# (people IN the UK — not "interested in"), feed-only asset group serving ONLY
# custom_label_1='lc_campaign'. Also:
#   * SUMMER testing tree: add UNIT_EXCLUDED INDEX1='lc_campaign' under its root
#   * AW testing tree:     add UNIT_EXCLUDED INDEX1='lc_campaign' under its root
#     (future AW ex-winners keep the aw26 INDEX2 label — without this they'd leak back)
#   * WINNERS asset group: remove stale item-id include nodes of the 55 revived pids
# Idempotent: every step checks before creating. Verifies by read-back, then enables.
import sys, requests, json, time
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import google_ads_connect as ga

CID = ga.CUSTOMER_ID
SUMMER_AG = "6729681029"; SUMMER_ROOT = "14966741205"; SUMMER_WEXCL = "14966741208"
AW_AG = "6738045970"
WINNERS_AG = "6684080392"
MERCHANT_ID = "5696302146"
LC_LABEL = "lc_campaign"
PIDS = [l.strip() for l in open("lastchance_pids.txt", encoding="utf-8") if l.strip()]

tok = ga.get_access_token()
H = ga._headers(tok)

def post(path, body):
    for attempt in range(4):
        r = requests.post(f"{ga.ADS_BASE}/customers/{CID}/{path}", headers=H, json=body, timeout=90)
        if r.status_code == 200:
            return r.json()
        if attempt < 3:
            print(f"   ({path} -> {r.status_code}, retry {attempt+1})"); time.sleep(5 * (attempt + 1)); continue
        print(f"!! {path} -> {r.status_code}: {r.text[:800]}"); sys.exit(1)

def search(q):
    out = []; token = None
    for attempt in range(6):
        body = {"query": q}
        if token: body["pageToken"] = token
        r = requests.post(f"{ga.ADS_BASE}/customers/{CID}/googleAds:search", headers=H, json=body, timeout=90)
        if r.status_code != 200:
            time.sleep(5 * (attempt + 1)); continue
        j = r.json(); out += j.get("results", [])
        token = j.get("nextPageToken")
        if not token: return out
        attempt = 0
    r.raise_for_status()

FP = f"customers/{CID}/assetGroupListingGroupFilters"

def tree(agid):
    out = []
    for r in search(f"""SELECT asset_group_listing_group_filter.id, asset_group_listing_group_filter.type,
      asset_group_listing_group_filter.parent_listing_group_filter,
      asset_group_listing_group_filter.case_value.product_custom_attribute.index,
      asset_group_listing_group_filter.case_value.product_custom_attribute.value,
      asset_group_listing_group_filter.case_value.product_item_id.value
      FROM asset_group_listing_group_filter WHERE asset_group.id = {agid}"""):
        f = r["assetGroupListingGroupFilter"]
        out.append(dict(id=str(f["id"]), type=f["type"],
                        parent=str(f.get("parentListingGroupFilter", "")),
                        cv=f.get("caseValue") or {}))
    return out

# ── sanity: Summer root + w_campaign exclusion are where we mapped them ──────
st = tree(SUMMER_AG)
root = next((n for n in st if n["id"] == SUMMER_ROOT and n["type"] == "SUBDIVISION"), None)
wx = next((n for n in st if n["id"] == SUMMER_WEXCL), None)
assert root and wx and (wx["cv"].get("productCustomAttribute") or {}).get("value") == "w_campaign", "Summer tree changed — abort"
print("sanity OK: Summer root + w_campaign exclusion confirmed")

# ── 1) budget £20/day ────────────────────────────────────────────────────────
rows = search("SELECT campaign_budget.resource_name FROM campaign_budget "
              "WHERE campaign_budget.name = 'Last Chance budget'")
if rows:
    budget_rn = rows[0]["campaignBudget"]["resourceName"]; print("budget (reused):", budget_rn)
else:
    res = post("campaignBudgets:mutate", {"operations": [{"create": {
        "name": "Last Chance budget", "amountMicros": "20000000",
        "deliveryMethod": "STANDARD", "explicitlyShared": False}}]})
    budget_rn = res["results"][0]["resourceName"]; print("budget £20/day:", budget_rn)

# ── 2) campaign: MCV tROAS 2.4, UK PRESENCE-only ─────────────────────────────
rows = search("SELECT campaign.resource_name, campaign.id FROM campaign "
              "WHERE campaign.name = 'PMax | Last Chance | UK' AND campaign.status != 'REMOVED'")
if rows:
    camp_rn = rows[0]["campaign"]["resourceName"]; camp_id = str(rows[0]["campaign"]["id"])
    print("campaign (reused):", camp_rn)
else:
    res = post("campaigns:mutate", {"operations": [{"create": {
        "name": "PMax | Last Chance | UK", "status": "PAUSED",
        "advertisingChannelType": "PERFORMANCE_MAX", "campaignBudget": budget_rn,
        "maximizeConversionValue": {"targetRoas": 2.4},
        "containsEuPoliticalAdvertising": "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING",
        "geoTargetTypeSetting": {"positiveGeoTargetType": "PRESENCE",
                                 "negativeGeoTargetType": "PRESENCE"},
        "shoppingSetting": {"merchantId": MERCHANT_ID}}}]})
    camp_rn = res["results"][0]["resourceName"]; camp_id = camp_rn.split("/")[-1]
    print("campaign (paused, tROAS 2.4, PRESENCE geo):", camp_rn)

crit = search(f"SELECT campaign_criterion.type FROM campaign_criterion "
              f"WHERE campaign.id = {camp_id} AND campaign_criterion.type IN ('LOCATION','LANGUAGE')")
if crit:
    print(f"geo/language already set ({len(crit)} criteria)")
else:
    post("campaignCriteria:mutate", {"operations": [
        {"create": {"campaign": camp_rn, "location": {"geoTargetConstant": "geoTargetConstants/2826"}}},
        {"create": {"campaign": camp_rn, "language": {"languageConstant": "languageConstants/1000"}}}]})
    print("geo UK + language EN set")

# ── 3) asset group + include-only-lc tree ────────────────────────────────────
rows = search(f"SELECT asset_group.resource_name, asset_group.id FROM asset_group "
              f"WHERE campaign.id = {camp_id} AND asset_group.name = 'Last Chance products'")
if rows:
    ag_rn = rows[0]["assetGroup"]["resourceName"]; ag_id = str(rows[0]["assetGroup"]["id"])
    print("asset group (reused):", ag_rn)
else:
    res = post("assetGroups:mutate", {"operations": [{"create": {
        "campaign": camp_rn, "name": "Last Chance products",
        "finalUrls": ["https://lolalooks.com"], "status": "ENABLED"}}]})
    ag_rn = res["results"][0]["resourceName"]; ag_id = ag_rn.split("/")[-1]
    print("asset group:", ag_rn)

if tree(ag_id):
    print("LC listing tree already exists — skipping create")
else:
    post("assetGroupListingGroupFilters:mutate", {"operations": [
        {"create": {"resourceName": f"{FP}/{ag_id}~-1", "assetGroup": ag_rn,
                    "type": "SUBDIVISION", "listingSource": "SHOPPING"}},
        {"create": {"resourceName": f"{FP}/{ag_id}~-2", "assetGroup": ag_rn,
                    "type": "UNIT_INCLUDED", "listingSource": "SHOPPING",
                    "parentListingGroupFilter": f"{FP}/{ag_id}~-1",
                    "caseValue": {"productCustomAttribute": {"index": "INDEX1", "value": LC_LABEL}}}},
        {"create": {"resourceName": f"{FP}/{ag_id}~-3", "assetGroup": ag_rn,
                    "type": "UNIT_EXCLUDED", "listingSource": "SHOPPING",
                    "parentListingGroupFilter": f"{FP}/{ag_id}~-1",
                    "caseValue": {"productCustomAttribute": {"index": "INDEX1"}}}}]})
    print(f"LC listing tree created (include ONLY {LC_LABEL})")

# ── 4) Summer tree: exclude lc_campaign at root (sibling of w_campaign excl) ─
have = [n for n in st if (n["cv"].get("productCustomAttribute") or {}).get("value") == LC_LABEL]
if have:
    print("Summer lc exclusion already present")
else:
    post("assetGroupListingGroupFilters:mutate", {"operations": [
        {"create": {"resourceName": f"{FP}/{SUMMER_AG}~-1",
                    "assetGroup": f"customers/{CID}/assetGroups/{SUMMER_AG}",
                    "type": "UNIT_EXCLUDED", "listingSource": "SHOPPING",
                    "parentListingGroupFilter": f"{FP}/{SUMMER_AG}~{SUMMER_ROOT}",
                    "caseValue": {"productCustomAttribute": {"index": "INDEX1", "value": LC_LABEL}}}}]})
    print("Summer tree now EXCLUDES lc_campaign")

# ── 5) AW tree: same exclusion under ITS root ────────────────────────────────
at = tree(AW_AG)
aw_root = next(n for n in at if n["type"] == "SUBDIVISION" and not n["parent"])
have = [n for n in at if (n["cv"].get("productCustomAttribute") or {}).get("value") == LC_LABEL]
if have:
    print("AW lc exclusion already present")
else:
    post("assetGroupListingGroupFilters:mutate", {"operations": [
        {"create": {"resourceName": f"{FP}/{AW_AG}~-1",
                    "assetGroup": f"customers/{CID}/assetGroups/{AW_AG}",
                    "type": "UNIT_EXCLUDED", "listingSource": "SHOPPING",
                    "parentListingGroupFilter": f"{FP}/{AW_AG}~{aw_root['id']}",
                    "caseValue": {"productCustomAttribute": {"index": "INDEX1", "value": LC_LABEL}}}}]})
    print("AW tree now EXCLUDES lc_campaign")

# ── 6) Winners AG: remove stale item-id includes of the 55 ───────────────────
wt = tree(WINNERS_AG)
stale = [n for n in wt if n["type"] == "UNIT_INCLUDED"
         and any(p in str((n["cv"].get("productItemId") or {}).get("value", "")) for p in PIDS)]
print(f"Winners AG: {len(stale)} stale item-id include node(s) for the 55")
if stale:
    post("assetGroupListingGroupFilters:mutate",
         {"operations": [{"remove": f"{FP}/{WINNERS_AG}~{n['id']}"} for n in stale]})
    print("removed")

# ── verify by read-back ──────────────────────────────────────────────────────
lt = tree(ag_id)
inc = [n for n in lt if n["type"] == "UNIT_INCLUDED"
       and (n["cv"].get("productCustomAttribute") or {}).get("value") == LC_LABEL]
assert len(inc) == 1 and len(lt) == 3, f"LC tree wrong shape: {lt}"
sx = [n for n in tree(SUMMER_AG) if (n["cv"].get("productCustomAttribute") or {}).get("value") == LC_LABEL and n["type"] == "UNIT_EXCLUDED"]
ax = [n for n in tree(AW_AG) if (n["cv"].get("productCustomAttribute") or {}).get("value") == LC_LABEL and n["type"] == "UNIT_EXCLUDED"]
assert len(sx) == 1, "Summer lc exclusion missing"
assert len(ax) == 1, "AW lc exclusion missing"
left = [n for n in tree(WINNERS_AG) if n["type"] == "UNIT_INCLUDED"
        and any(p in str((n["cv"].get("productItemId") or {}).get("value", "")) for p in PIDS)]
assert not left, f"Winners AG still has {len(left)} stale nodes"
g = search(f"SELECT campaign.geo_target_type_setting.positive_geo_target_type FROM campaign WHERE campaign.id = {camp_id}")
geo = g[0]["campaign"]["geoTargetTypeSetting"]["positiveGeoTargetType"]
assert geo == "PRESENCE", f"geo type is {geo}"
tr = search(f"SELECT campaign.maximize_conversion_value.target_roas FROM campaign WHERE campaign.id = {camp_id}")
troas = tr[0]["campaign"]["maximizeConversionValue"]["targetRoas"]
assert abs(troas - 2.4) < 1e-6, f"tROAS is {troas}"
print("\nverification PASSED — enabling campaign")

post("campaigns:mutate", {"operations": [{"update": {
    "resourceName": camp_rn, "status": "ENABLED"}, "updateMask": "status"}]})
print(f"\nLIVE: 'PMax | Last Chance | UK'  id={camp_id}  £20/day  tROAS 2.4  UK PRESENCE-only")
print(f"CAMPAIGN_ID={camp_id}")
