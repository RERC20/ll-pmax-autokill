# google_ads_connect.py
# ──────────────────────────────────────────────────────────────
# PMax Auto-Kill Engine — STEP 1: prove the connection
#
# Mirrors your Shopify auth (client_id + client_secret -> token in a header),
# but Google Ads needs ONE extra durable secret: a refresh_token.
#
# This does the smallest possible "are we connected?" check — read only,
# no campaign data, no writes:
#   1. Exchange refresh_token -> short-lived access_token   (the login)
#   2. ListAccessibleCustomers                              (proves auth works at all)
#   3. One GAQL query against your account                 (proves we can read YOUR account)
# Run it until all 3 lines print ✅.
# ──────────────────────────────────────────────────────────────

import requests, sys, json
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
from creds import cred

# ── Google Ads credentials — from env vars / GitHub Secrets (no secrets in this file;
#    local runs read them from the git-ignored _secrets_local.py) ─────────────────
DEVELOPER_TOKEN   = cred('GOOGLE_DEVELOPER_TOKEN')
CLIENT_ID         = cred('GOOGLE_CLIENT_ID')
CLIENT_SECRET     = cred('GOOGLE_CLIENT_SECRET')
REFRESH_TOKEN     = cred('GOOGLE_REFRESH_TOKEN')
LOGIN_CUSTOMER_ID = cred('GOOGLE_LOGIN_CUSTOMER_ID')   # empty → direct access (no manager account)
CUSTOMER_ID       = cred('GOOGLE_CUSTOMER_ID')         # Google Ads customer id (from secret)

API_VERSION = 'v21'   # if the 1st real call 404s with "version not found", bump this — Google ships ~3x/yr

OAUTH_TOKEN_URL = 'https://oauth2.googleapis.com/token'
ADS_BASE = f'https://googleads.googleapis.com/{API_VERSION}'


# ── 1. The login: refresh_token -> access_token ──────────────
# Shopify:    client_id + client_secret            -> access_token
# Google Ads: client_id + client_secret + refresh  -> access_token
# The refresh_token is the only new ingredient. It encodes "a human said yes".
def get_access_token():
    r = requests.post(OAUTH_TOKEN_URL, data={
        'grant_type':    'refresh_token',
        'client_id':     CLIENT_ID,
        'client_secret': CLIENT_SECRET,
        'refresh_token': REFRESH_TOKEN,
    }, timeout=30)
    if r.status_code != 200:
        print(f"❌ LOGIN FAILED ({r.status_code}): {r.text}")
        if 'invalid_grant'  in r.text: print("   → refresh_token is wrong/expired, or made with a different client_id/secret.")
        if 'invalid_client' in r.text: print("   → client_id / client_secret don't match.")
        sys.exit(1)
    print("✅ 1/3  Got access token — OAuth chain works.")
    return r.json()['access_token']


def _headers(access_token, with_login=True):
    h = {
        'Authorization':   f'Bearer {access_token}',  # WHO (the human, via OAuth)
        'developer-token': DEVELOPER_TOKEN,           # WHAT software (your app)
        'Content-Type':    'application/json',
    }
    if with_login and LOGIN_CUSTOMER_ID:
        h['login-customer-id'] = LOGIN_CUSTOMER_ID    # THROUGH which manager (only if set)
    return h


# ── 2. ListAccessibleCustomers — cheapest proof auth is valid ─
# Needs ONLY the dev token + access token. No customer id, no login-customer-id.
# This isolates "is my auth good?" from "am I targeting the right account?".
def list_accessible_customers(access_token):
    r = requests.get(f'{ADS_BASE}/customers:listAccessibleCustomers',
                     headers=_headers(access_token, with_login=False), timeout=30)
    if r.status_code != 200:
        print(f"❌ ListAccessibleCustomers FAILED ({r.status_code}): {r.text}")
        if 'DEVELOPER_TOKEN_NOT_APPROVED' in r.text:
            print("   → dev token still 'Test' level. Apply for Basic access to reach real accounts.")
        sys.exit(1)
    names = r.json().get('resourceNames', [])
    print(f"✅ 2/3  Auth valid. Accounts this login can see: {names}")
    return names


# ── 3. One GAQL query against YOUR account ───────────────────
# Proves you can actually read the specific account
# (this is where login-customer-id + CUSTOMER_ID have to line up).
def query_customer(access_token):
    url  = f'{ADS_BASE}/customers/{CUSTOMER_ID}/googleAds:search'
    body = {'query': 'SELECT customer.id, customer.descriptive_name, customer.currency_code FROM customer'}
    r = requests.post(url, headers=_headers(access_token), json=body, timeout=30)
    if r.status_code != 200:
        print(f"❌ Account query FAILED ({r.status_code}): {r.text}")
        if 'USER_PERMISSION_DENIED' in r.text:
            print("   → the authorized human can't see CUSTOMER_ID, or login-customer-id is wrong.")
        if 'NOT_FOUND' in r.text or r.status_code == 404:
            print(f"   → maybe bump API_VERSION (currently {API_VERSION}).")
        sys.exit(1)
    rows = r.json().get('results', [])
    print(f"✅ 3/3  Read your account OK:\n{json.dumps(rows, indent=2)}")


if __name__ == '__main__':
    tok = get_access_token()
    list_accessible_customers(tok)
    query_customer(tok)
    print("\n🎉 Connection proven. Auth chain is solid — ready to pull PMax data next.")
