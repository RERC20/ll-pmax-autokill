# -*- coding: utf-8 -*-
# OWNER-APPROVED (chat, 2026-08-07): build 'PMax | Testing | AW | UK' — clone of
# Testing (MaxConv, £30/day, UK+English, feed-only asset group) serving ONLY
# custom_label_2='aw26' (excluding custom_label_1='w_campaign'), and add an aw26
# EXCLUSION to the old Testing campaign's catch-all node so the 530 AW products
# never serve there. Verifies both trees via read-back, then enables.
import sys, requests, json
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import google_ads_connect as ga

CID = ga.CUSTOMER_ID
OLD_AG = "6729681029"                 # 'All products (testing)' asset group
OLD_ELSE_SUBDIV = "14966753949"       # item-id subdivision under INDEX1-else
OLD_CATCHALL = "14966753952"          # UNIT_INCLUDED item-id everything-else
MERCHANT_ID = "5696302146"

tok = ga.get_access_token()
H = ga._headers(tok)
import time
def post(path, body):
    # Google intermittently returns transient 400/5xx (seen all day 2026-08-06/07);
    # retry with backoff before treating it as a genuine validation failure.
    for attempt in range(4):
        r = requests.post(f"{ga.ADS_BASE}/customers/{CID}/{path}", headers=H, json=body, timeout=90)
        if r.status_code == 200:
            return r.json()
        if attempt < 3:
            print(f"   ({path} -> {r.status_code}, retry {attempt+1})"); time.sleep(5 * (attempt + 1)); continue
        print(f"!! {path} -> {r.status_code}: {r.text[:800]}"); sys.exit(1)
def search(q):
    import time as _t
    for attempt in range(4):
        r = requests.post(f"{ga.ADS_BASE}/customers/{CID}/googleAds:search", headers=H, json={"query": q}, timeout=60)
        if r.status_code == 200:
            return r.json().get("results", [])
        if attempt < 3:
            _t.sleep(5 * (attempt + 1)); continue
        r.raise_for_status()

# sanity: the old catch-all node is still what we mapped
rows = search(f"""SELECT asset_group_listing_group_filter.id, asset_group_listing_group_filter.type,
 asset_group_listing_group_filter.parent_listing_group_filter
 FROM asset_group_listing_group_filter WHERE asset_group.id = {OLD_AG}""")
node = next((r["assetGroupListingGroupFilter"] for r in rows
             if str(r["assetGroupListingGroupFilter"]["id"]) == OLD_CATCHALL), None)
assert node and node["type"] == "UNIT_INCLUDED", f"catch-all changed: {node}"
assert str(node.get("parentListingGroupFilter", "")).endswith(OLD_ELSE_SUBDIV)
print("sanity OK: old Testing catch-all confirmed")

FP = f"customers/{CID}/assetGroupListingGroupFilters"

# ── budget (reuse if a prior run already created it) ─────────────────────────
rows = search("SELECT campaign_budget.resource_name, campaign_budget.name FROM campaign_budget "
              "WHERE campaign_budget.name = 'Testing AW budget'")
if rows:
    budget_rn = rows[0]["campaignBudget"]["resourceName"]
    print("budget (reused):", budget_rn)
else:
    res = post("campaignBudgets:mutate", {"operations": [{"create": {
        "name": "Testing AW budget", "amountMicros": "30000000",
        "deliveryMethod": "STANDARD", "explicitlyShared": False}}]})
    budget_rn = res["results"][0]["resourceName"]
    print("budget:", budget_rn)

# ── campaign (reuse if it exists; declare the now-required EU political flag) ─
rows = search("SELECT campaign.resource_name, campaign.id FROM campaign "
              "WHERE campaign.name = 'PMax | Testing | AW | UK' AND campaign.status != 'REMOVED'")
if rows:
    camp_rn = rows[0]["campaign"]["resourceName"]; camp_id = str(rows[0]["campaign"]["id"])
    print("campaign (reused):", camp_rn)
else:
    res = post("campaigns:mutate", {"operations": [{"create": {
        "name": "PMax | Testing | AW | UK", "status": "PAUSED",
        "advertisingChannelType": "PERFORMANCE_MAX", "campaignBudget": budget_rn,
        "maximizeConversions": {}, "urlExpansionOptOut": False,
        "containsEuPoliticalAdvertising": "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING",
        "shoppingSetting": {"merchantId": MERCHANT_ID}}}]})
    camp_rn = res["results"][0]["resourceName"]; camp_id = camp_rn.split("/")[-1]
    print("campaign (paused):", camp_rn)

# geo + language — idempotent (the first run died here, so check-then-create)
crit = search(f"SELECT campaign_criterion.type FROM campaign_criterion "
              f"WHERE campaign.id = {camp_id} AND campaign_criterion.type IN ('LOCATION','LANGUAGE')")
if crit:
    print(f"geo/language already set ({len(crit)} criteria)")
else:
    post("campaignCriteria:mutate", {"operations": [
        {"create": {"campaign": camp_rn, "location": {"geoTargetConstant": "geoTargetConstants/2826"}}},
        {"create": {"campaign": camp_rn, "language": {"languageConstant": "languageConstants/1000"}}}]})
    print("geo UK + language EN set")

# ── asset group (reuse if it exists) ─────────────────────────────────────────
rows = search(f"SELECT asset_group.resource_name, asset_group.id FROM asset_group "
              f"WHERE campaign.id = {camp_id} AND asset_group.name = 'AW products (testing)'")
if rows:
    ag_rn = rows[0]["assetGroup"]["resourceName"]; ag_id = str(rows[0]["assetGroup"]["id"])
    print("asset group (reused):", ag_rn)
else:
    res = post("assetGroups:mutate", {"operations": [{"create": {
        "campaign": camp_rn, "name": "AW products (testing)",
        "finalUrls": ["https://lolalooks.com"], "status": "ENABLED"}}]})
    ag_rn = res["results"][0]["resourceName"]; ag_id = ag_rn.split("/")[-1]
    print("asset group:", ag_rn)

existing_new = search(f"SELECT asset_group_listing_group_filter.id FROM asset_group_listing_group_filter "
                      f"WHERE asset_group.id = {ag_id}")
if existing_new:
    print(f"AW listing tree already has {len(existing_new)} node(s) — skipping create")
else:
    post("assetGroupListingGroupFilters:mutate", {"operations": [
    {"create": {"resourceName": f"{FP}/{ag_id}~-1", "assetGroup": ag_rn,
                "type": "SUBDIVISION", "listingSource": "SHOPPING"}},
    {"create": {"resourceName": f"{FP}/{ag_id}~-2", "assetGroup": ag_rn,
                "type": "UNIT_EXCLUDED", "listingSource": "SHOPPING",
                "parentListingGroupFilter": f"{FP}/{ag_id}~-1",
                "caseValue": {"productCustomAttribute": {"index": "INDEX1", "value": "w_campaign"}}}},
    {"create": {"resourceName": f"{FP}/{ag_id}~-3", "assetGroup": ag_rn,
                "type": "SUBDIVISION", "listingSource": "SHOPPING",
                "parentListingGroupFilter": f"{FP}/{ag_id}~-1",
                "caseValue": {"productCustomAttribute": {"index": "INDEX1"}}}},
    {"create": {"resourceName": f"{FP}/{ag_id}~-4", "assetGroup": ag_rn,
                "type": "UNIT_INCLUDED", "listingSource": "SHOPPING",
                "parentListingGroupFilter": f"{FP}/{ag_id}~-3",
                "caseValue": {"productCustomAttribute": {"index": "INDEX2", "value": "aw26"}}}},
    {"create": {"resourceName": f"{FP}/{ag_id}~-5", "assetGroup": ag_rn,
                "type": "UNIT_EXCLUDED", "listingSource": "SHOPPING",
                "parentListingGroupFilter": f"{FP}/{ag_id}~-3",
                "caseValue": {"productCustomAttribute": {"index": "INDEX2"}}}},
]})
print("AW listing tree created (exclude w_campaign; include ONLY aw26)")

post("assetGroupListingGroupFilters:mutate", {"operations": [
    {"remove": f"{FP}/{OLD_AG}~{OLD_CATCHALL}"},
    {"create": {"resourceName": f"{FP}/{OLD_AG}~-1",
                "assetGroup": f"customers/{CID}/assetGroups/{OLD_AG}",
                "type": "SUBDIVISION", "listingSource": "SHOPPING",
                "parentListingGroupFilter": f"{FP}/{OLD_AG}~{OLD_ELSE_SUBDIV}",
                "caseValue": {"productItemId": {}}}},
    {"create": {"resourceName": f"{FP}/{OLD_AG}~-2",
                "assetGroup": f"customers/{CID}/assetGroups/{OLD_AG}",
                "type": "UNIT_EXCLUDED", "listingSource": "SHOPPING",
                "parentListingGroupFilter": f"{FP}/{OLD_AG}~-1",
                "caseValue": {"productCustomAttribute": {"index": "INDEX2", "value": "aw26"}}}},
    {"create": {"resourceName": f"{FP}/{OLD_AG}~-3",
                "assetGroup": f"customers/{CID}/assetGroups/{OLD_AG}",
                "type": "UNIT_INCLUDED", "listingSource": "SHOPPING",
                "parentListingGroupFilter": f"{FP}/{OLD_AG}~-1",
                "caseValue": {"productCustomAttribute": {"index": "INDEX2"}}}},
]})
print("old Testing catch-all now EXCLUDES aw26 (everything else unchanged)")

def tree(agid):
    out = []
    for r in search(f"""SELECT asset_group_listing_group_filter.id, asset_group_listing_group_filter.type,
      asset_group_listing_group_filter.case_value.product_custom_attribute.index,
      asset_group_listing_group_filter.case_value.product_custom_attribute.value
      FROM asset_group_listing_group_filter WHERE asset_group.id = {agid}"""):
        f = r["assetGroupListingGroupFilter"]
        out.append((str(f["id"]), f["type"], (f.get("caseValue") or {})))
    return out

new_tree = tree(ag_id)
print("\nNEW AG tree:")
for fid, t, cv in new_tree:
    print("  ", fid, t, json.dumps(cv))
aw_inc = [x for x in new_tree if x[1] == "UNIT_INCLUDED"
          and (x[2].get("productCustomAttribute") or {}).get("value") == "aw26"]
assert len(aw_inc) == 1 and len(new_tree) == 5, "new tree wrong shape"
old_aw_excl = [x for x in tree(OLD_AG) if x[1] == "UNIT_EXCLUDED"
               and (x[2].get("productCustomAttribute") or {}).get("value") == "aw26"]
assert len(old_aw_excl) == 1, "old Testing aw26 exclusion missing"
print("\nverification PASSED — enabling campaign")

post("campaigns:mutate", {"operations": [{"update": {
    "resourceName": camp_rn, "status": "ENABLED"}, "updateMask": "status"}]})
print(f"\nLIVE: 'PMax | Testing | AW | UK'  id={camp_id}  £30/day MaxConv, serves ONLY aw26")
print(f"CAMPAIGN_ID={camp_id}")
