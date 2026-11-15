# newstore/config.py - settings for the NEW LolaLooks store (gwvmrw-vm) + NEW Google Ads account (778-370-4709).
# No secrets here (public repo). Every value can be overridden by an environment variable of the same name with an
# NS_ prefix (e.g. NS_SCHEDULE_MINUTES=30). The old-store engine (kill_engine_google_auto.py) does NOT read this file.
import datetime
import os


def _env(name, default):
    v = os.environ.get('NS_' + name)
    return default if v is None or v == '' else v


# ---- targets -------------------------------------------------------------------------------------------------------
SHOPIFY_STORE = _env('SHOPIFY_STORE', 'gwvmrw-vm.myshopify.com')
SHOPIFY_API = _env('SHOPIFY_API', '2026-01')
GOOGLE_CUSTOMER_ID = _env('GOOGLE_CUSTOMER_ID', '7783704709')      # bills in USD, America/New_York
ACCOUNT_CURRENCY = 'USD'                                             # checked against the account on every run
ACCOUNT_TIMEZONE = 'America/New_York'
STORE_TIMEZONE = 'Europe/London'                                     # every rule window is a UK calendar day

# ---- campaigns (week 1: only Testing exists) -----------------------------------------------------------------------
TESTING_CAMPAIGN_ID = _env('TESTING_CAMPAIGN_ID', '24425197606')    # PMax | Testing | UK
TESTING_AG_ID = _env('TESTING_AG_ID', '6767707734')
# Empty = that campaign does not exist yet. While Winners/Last Chance are empty the engine ONLY writes tags, the
# custom_label_1 metafield and drafts - it never moves items between campaigns through listing trees.
WINNERS_CAMPAIGN_ID = _env('WINNERS_CAMPAIGN_ID', '')
WINNERS_AG_ID = _env('WINNERS_AG_ID', '')
LC_CAMPAIGN_ID = _env('LC_CAMPAIGN_ID', '')
LC_AG_ID = _env('LC_AG_ID', '')


def campaign_ids():
    """The 778 campaigns whose spend counts (Testing + Winners/Last Chance once they exist)."""
    return [c for c in (TESTING_CAMPAIGN_ID, WINNERS_CAMPAIGN_ID, LC_CAMPAIGN_ID) if c]


def tree_moves_enabled():
    """Listing-tree moves need BOTH the Winners and Last Chance campaign + asset group ids. Not built in this branch."""
    return all((WINNERS_CAMPAIGN_ID, WINNERS_AG_ID, LC_CAMPAIGN_ID, LC_AG_ID))


# ---- schedule (owner 2026-11-15 18:43: every 30 minutes = 48 runs a day) ------------------------------------------
SCHEDULE_MINUTES = int(_env('SCHEDULE_MINUTES', '30'))
RAILWAY_CRON = _env('RAILWAY_CRON', '7,37 * * * *')                 # = every 30 min, off the :00/:30 rush

# ---- tags / labels (same names the old engine uses, so earned w/lc tags carry over) ------------------------------
WINNER_TAG = 'w_campaign'
LC_TAG = 'lc_campaign'
LOST_TAG = 'l_camp'
LC_STAMP = 'lc:'             # lc:YYYY-MM-DD = the UK day the product entered Last Chance
W_STAMP = 'w_since:'         # w_since:YYYY-MM-DD = the UK day the product entered Winners (sale day or return day)
DRAFT_TAG = 'draft_bad_product'
OLD_ORDER_TAG = 'old-order'  # copies of old-store orders (createdAt = 13 Nov 2026; processedAt = the real sale time)
LABEL_NAMESPACE = 'mm-google-shopping'
LABEL_KEY = 'custom_label_1'
MENS_PAUSED_LABEL = 'mens_paused'   # custom_label_2 - excluded from Testing by the listing tree, not by the engine

# Products with no w_since: stamp yet (earned their w_campaign tag on the old store) use their last sale; if they have
# no sale on record either, this day (the store move) is their Winners anchor.
WINNERS_ANCHOR_FALLBACK = datetime.date.fromisoformat(_env('WINNERS_ANCHOR_FALLBACK', '2026-11-13'))
LC_ANCHOR_FALLBACK = datetime.date.fromisoformat(_env('LC_ANCHOR_FALLBACK', '2026-11-13'))

# ---- the owner's confirmed rules (2026-11-15) ----------------------------------------------------------------------
NEVER_SOLD_PRICE_DIV = 7.0           # rule 2: spend > min(price / 7, GBP 5) ...
NEVER_SOLD_CAP_GBP = 5.0
NEVER_SOLD_CLICKS = 25               # ... or 25+ clicks, with 0 sales -> draft
NEVER_SOLD_LOOKBACK_D = int(_env('NEVER_SOLD_LOOKBACK_D', '30'))   # spend/click window (UK days incl. today)
WEAK_PRICE_DIV = 2.0                 # rule 3: spend >= price / 2 in the window -> Last Chance
WEAK_WINDOWS_D = (7, 14)             # 3a, 3b
WEAK_SINCE_SALE_D = 30               # 3c: the 30 days after the last sale
ZOMBIE_D = 30                        # rule 4: no sale for 30 days since the last sale / Winners entry -> Last Chance
LC_PRICE_DIV = 2.0                   # rule 5: spend > min(price / 2, GBP 20) with no sale since entry -> draft
LC_CAP_GBP = 20.0

# ---- glitch guards: more than this many moves of one kind in one run = data problem, NONE of that kind is written --
CAPS = {
    'first_sale': int(_env('CAP_FIRST_SALE', '60')),
    'never_sold': int(_env('CAP_NEVER_SOLD', '120')),
    'weak_seller': int(_env('CAP_WEAK', '40')),
    'zombie': int(_env('CAP_ZOMBIE', '40')),
    'lc_return': int(_env('CAP_LC_RETURN', '40')),
    'lc_draft': int(_env('CAP_LC_DRAFT', '15')),
}

# ---- Google Ads API budget (developer token shared with other services; Basic access = 15,000 ops/day) -----------
# One run = 3 operations: customer check + 1 searchStream (product x day spend) + 1 searchStream (campaign hourly cost).
# 48 runs/day -> 144 ops/day. The run refuses to start past this many calls in one Pacific day (Google's quota day).
DAILY_GOOGLE_OPS_BUDGET = int(_env('DAILY_GOOGLE_OPS_BUDGET', '600'))
SPEND_LOOKBACK_D = 400               # how far back spend is read (covers any Winners/LC anchor; 778 started 2026-11-01)

# ---- writes are OFF unless both are set (the branch ships dry) ----------------------------------------------------
LIVE = _env('LIVE', '0') == '1'
