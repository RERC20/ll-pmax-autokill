# surf/shop.py - Shopify Admin GraphQL client, copied from stores-monitor engine.py (class Shop) so the scaler reads
# the same new LolaLooks store with the same SHOPIFY_NEWLOLA_* variables (client-credentials grant).
import json
import os
import time

import requests

API_V = "2026-01"


class Shop:
    def __init__(self, env):
        self.env = env
        self.domain = os.environ.get(f"SHOPIFY_{env}_DOMAIN")
        self.cid = os.environ.get(f"SHOPIFY_{env}_CLIENT_ID")
        self.sec = os.environ.get(f"SHOPIFY_{env}_CLIENT_SECRET")
        self.tok = None; self.tok_at = 0
        self.s = requests.Session()

    @property
    def ok(self):
        return bool(self.domain and self.cid and self.sec)

    def token(self, force=False):
        if self.tok and not force and time.time() - self.tok_at < 6 * 3600:
            return self.tok
        r = self.s.post(f"https://{self.domain}/admin/oauth/access_token", timeout=60,
                        json={"client_id": self.cid, "client_secret": self.sec, "grant_type": "client_credentials"}).json()
        if "access_token" not in r:
            raise RuntimeError(f"{self.env}: token refused")
        self.tok, self.tok_at = r["access_token"], time.time()
        return self.tok

    def gql(self, q, v=None):
        for a in range(8):
            try:
                r = self.s.post(f"https://{self.domain}/admin/api/{API_V}/graphql.json", timeout=120,
                                headers={"X-Shopify-Access-Token": self.token(force=a > 0 and a % 3 == 0)},
                                json={"query": q, "variables": v or {}})
                if r.status_code == 401:
                    self.token(force=True); continue
                j = r.json()
            except (requests.RequestException, ValueError):
                time.sleep(3 + 2 * a); continue
            if "THROTTLED" in json.dumps(j.get("errors", ""))[:500]:
                time.sleep(5); continue
            if j.get("errors") and not j.get("data"):
                raise RuntimeError(f"{self.env}: {json.dumps(j['errors'])[:200]}")
            return j["data"]
        raise RuntimeError(f"{self.env}: Shopify not answering")
