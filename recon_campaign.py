# -*- coding: utf-8 -*-
"""Read-only recon of the existing PMax campaigns: settings, budget, bidding,
asset groups, listing-group filters — so Testing|AW can be built as a clone."""
import sys, requests, json
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import google_ads_connect as ga

tok = ga.get_access_token()
def search(q):
    r = requests.post(f"{ga.ADS_BASE}/customers/{ga.CUSTOMER_ID}/googleAds:search",
                      headers=ga._headers(tok), json={"query": q}, timeout=60)
    r.raise_for_status(); return r.json().get("results", [])

print("=== CAMPAIGNS ===")
rows = search("""SELECT campaign.id, campaign.name, campaign.status, campaign.bidding_strategy_type,
  campaign.maximize_conversion_value.target_roas, campaign.url_expansion_opt_out,
  campaign.shopping_setting.merchant_id, campaign.shopping_setting.feed_label,
  campaign_budget.amount_micros, campaign_budget.resource_name
  FROM campaign WHERE campaign.advertising_channel_type = 'PERFORMANCE_MAX' AND campaign.status != 'REMOVED'""")
for r in rows:
    c = r["campaign"]; b = r.get("campaignBudget", {})
    print(f"  {c['id']}  {c['name']!r}  {c['status']}  bid={c.get('biddingStrategyType')} "
          f"tROAS={(c.get('maximizeConversionValue') or {}).get('targetRoas')} "
          f"urlExpOptOut={c.get('urlExpansionOptOut')} "
          f"MC={(c.get('shoppingSetting') or {}).get('merchantId')} feedLabel={(c.get('shoppingSetting') or {}).get('feedLabel')!r} "
          f"budget=£{int(b.get('amountMicros', 0))/1e6:g}/day")

print("\n=== GEO / LANGUAGE (Testing 24027270949) ===")
for r in search("""SELECT campaign_criterion.type, campaign_criterion.location.geo_target_constant,
  campaign_criterion.language.language_constant, campaign_criterion.negative
  FROM campaign_criterion WHERE campaign.id = 24027270949
  AND campaign_criterion.type IN ('LOCATION','LANGUAGE')"""):
    print("  ", json.dumps(r.get("campaignCriterion")))

print("\n=== ASSET GROUPS (all PMax) ===")
for r in search("""SELECT campaign.id, asset_group.id, asset_group.name, asset_group.status, asset_group.final_urls
  FROM asset_group WHERE campaign.advertising_channel_type = 'PERFORMANCE_MAX'"""):
    print(f"  camp {r['campaign']['id']}  ag {r['assetGroup']['id']}  {r['assetGroup']['name']!r}  "
          f"{r['assetGroup']['status']}  urls={r['assetGroup'].get('finalUrls')}")

print("\n=== LISTING GROUP FILTERS (per asset group) ===")
for r in search("""SELECT campaign.id, asset_group.id, asset_group_listing_group_filter.id,
  asset_group_listing_group_filter.type, asset_group_listing_group_filter.parent_listing_group_filter,
  asset_group_listing_group_filter.case_value.product_custom_attribute.index,
  asset_group_listing_group_filter.case_value.product_custom_attribute.value,
  asset_group_listing_group_filter.listing_source
  FROM asset_group_listing_group_filter"""):
    f = r["assetGroupListingGroupFilter"]
    cv = (f.get("caseValue") or {}).get("productCustomAttribute")
    print(f"  camp {r['campaign']['id']} ag {r['assetGroup']['id']}  filter {f['id']}  {f['type']}  "
          f"parent={f.get('parentListingGroupFilter','-').split('~')[-1]}  "
          f"customAttr={cv}")

print("\n=== how many text assets does the Testing asset group carry? (feed-only check) ===")
for r in search("""SELECT campaign.id, asset_group_asset.field_type, asset_group_asset.status
  FROM asset_group_asset WHERE campaign.id = 24027270949"""):
    print("  ", r["assetGroupAsset"]["fieldType"], r["assetGroupAsset"]["status"])
