# -*- coding: utf-8 -*-
# OWNER-APPROVED (2026-08-07): pause 'PMax | Champions | UK' (24047674442).
# Roster already folded back into Winners (c_champion untagged, labels w_campaign).
import sys, requests, time
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import google_ads_connect as ga

tok = ga.get_access_token(); H = ga._headers(tok)
CID = ga.CUSTOMER_ID
for attempt in range(4):
    r = requests.post(f"{ga.ADS_BASE}/customers/{CID}/campaigns:mutate", headers=H, json={
        "operations": [{"update": {"resourceName": f"customers/{CID}/campaigns/24047674442",
                                   "status": "PAUSED"}, "updateMask": "status"}]}, timeout=60)
    if r.status_code == 200:
        print("PAUSED: PMax | Champions | UK (24047674442)"); break
    print(f"  ({r.status_code}, retry {attempt+1})"); time.sleep(5 * (attempt + 1))
else:
    print("FAILED:", r.text[:400]); sys.exit(1)
# verify
q = "SELECT campaign.status FROM campaign WHERE campaign.id = 24047674442"
r = requests.post(f"{ga.ADS_BASE}/customers/{CID}/googleAds:search", headers=H, json={"query": q}, timeout=60)
print("verified status:", r.json()["results"][0]["campaign"]["status"])
