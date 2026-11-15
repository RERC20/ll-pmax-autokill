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
# Empty = that campaign does not exist yet (set on Railway: Winners 24467845823 / AG 6773604242, Last Chance
# 24467846096 / AG 6773605874, created PAUSED 2026-11-15). Their spend counts in the rules once set.
# ROUTING never uses listing-tree item moves: the engine writes the w_campaign / lc_campaign tag + the
# mm-google-shopping.custom_label_1 metafield; the Google & YouTube feed carries the METAFIELD into custom_label_1
# (verified 2026-11-15: 100% of 90k items match the metafields, cat_* labels exist only as metafields), and the
# Winners / Last Chance listing filters select custom_label_1 = w_campaign / lc_campaign.
WINNERS_CAMPAIGN_ID = _env('WINNERS_CAMPAIGN_ID', '')
WINNERS_AG_ID = _env('WINNERS_AG_ID', '')
LC_CAMPAIGN_ID = _env('LC_CAMPAIGN_ID', '')
LC_AG_ID = _env('LC_AG_ID', '')


def campaign_ids():
    """The 778 campaigns whose spend counts (Testing + Winners/Last Chance once they exist)."""
    return [c for c in (TESTING_CAMPAIGN_ID, WINNERS_CAMPAIGN_ID, LC_CAMPAIGN_ID) if c]


def tree_moves_enabled():
    """Always False: this engine routes by custom_label_1 (feed label), never by listing-tree item moves."""
    return False


# ---- the NEW setup (owner 2026-11-15 19:16) ------------------------------------------------------------------------
# Only sales of the new setup count in EVERY rule: orders processed at/after CUTOVER that are not copies of old-store
# orders (tag old-order / old-order-#...). The old w/lc tags were renamed to was_w_campaign / was_lc_campaign (history
# only - those products are Testing products now), so Winners and Last Chance start empty and fill from new sales.
CUTOVER = _env('CUTOVER', '2026-11-14T13:00:00+00:00')
# Zombie clock (owner-safe default 2026-11-15): 30 days from the LATER of the last sale and the day the Winners campaign
# was first enabled. Empty = Winners has never been enabled = no zombie moves. Set it (YYYY-MM-DD) on launch day.
WINNERS_LAUNCH_DATE = _env('WINNERS_LAUNCH_DATE', '')


def winners_launch():
    return datetime.date.fromisoformat(WINNERS_LAUNCH_DATE) if WINNERS_LAUNCH_DATE else None


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

# A Winner with neither a new sale nor a w_since: stamp (should not happen in the new setup) is anchored at the cutover
# day; a Last Chance product without an lc: stamp likewise.
WINNERS_ANCHOR_FALLBACK = datetime.date.fromisoformat(_env('WINNERS_ANCHOR_FALLBACK', '2026-11-14'))
LC_ANCHOR_FALLBACK = datetime.date.fromisoformat(_env('LC_ANCHOR_FALLBACK', '2026-11-14'))
WAS_TAGS = ('was_w_campaign', 'was_lc_campaign')   # history only - never read by the rules

# ---- store-credit orders are NOT sales (owner 2026-11-16 00:25) -----------------------------------------------------
# The support desk issues credit as discount codes "CREDIT-XXXXXXXXXX" titled "Store credit #<order>". An order is
# ignored by EVERY rule when either:
#   (a) it used a store-credit code/title AND the discounts covered >= CREDIT_MAIN_SHARE of its list price
#       ("paid wholly or mainly with store credit"), or
#   (b) its product revenue after all discounts is < MIN_REVENUE_SHARE of its list price (any name - catches credits
#       given another way, e.g. a manual discount).
# list price = sum of the lines' original prices x quantity; product revenue = order subtotal after discounts.
CREDIT_CODE_PREFIX = 'CREDIT-'
CREDIT_TITLE_PREFIX = 'store credit'
CREDIT_MAIN_SHARE = float(_env('CREDIT_MAIN_SHARE', '0.5'))
MIN_REVENUE_SHARE = float(_env('MIN_REVENUE_SHARE', '0.2'))

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

# ---- per-run Telegram report (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID, same as the old engine); NS_TELEGRAM=0 = off --
TELEGRAM = _env('TELEGRAM', '1') == '1'

# ---- writes are OFF unless both are set (the branch ships dry) ----------------------------------------------------
LIVE = _env('LIVE', '0') == '1'
