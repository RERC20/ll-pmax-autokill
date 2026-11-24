"""daily_product_count: LolaLooks = the NEW store (gwvmrw-vm + Google Ads 778), and one store failing never blocks the rest.
Owner 2026-11-24: the old store was closed by Shopify ~22 Nov and every run since sent nothing for ANY store."""
import os, sys, json, datetime, importlib, types
from unittest import mock
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

ENV = {'NS_SHOPIFY_CLIENT_ID': 'ns-id', 'NS_SHOPIFY_CLIENT_SECRET': 'ns-secret', 'GOOGLE_CLIENT_ID': 'g-id',
       'GOOGLE_CLIENT_SECRET': 'g-sec', 'GOOGLE_REFRESH_TOKEN': 'g-ref', 'GOOGLE_DEVELOPER_TOKEN': 'dev-tok',
       'LOR_SHOPIFY_STORE': 'loreya.myshopify.com', 'LOR_SHOPIFY_CLIENT_ID': 'l', 'LOR_SHOPIFY_CLIENT_SECRET': 'l',
       'SOL_SHOPIFY_STORE': '', 'SHOPIFY_STORE': 'old-closed.myshopify.com'}


class R:
    def __init__(self, status=200, body=None, ctype='application/json'):
        self.status_code, self._b, self.headers = status, body or {}, {'content-type': ctype}
        self.text = json.dumps(self._b)
    def json(self): return self._b


def load():
    with mock.patch.dict(os.environ, ENV, clear=False):
        import daily_product_count as m
        return importlib.reload(m)


def fake_post(calls):
    def post(url, **kw):
        calls.append((url, kw))
        if url.endswith('/admin/oauth/access_token'):
            if 'gwvmrw-vm' in url or 'loreya' in url: return R(body={'access_token': 'shop-tok'})
            return R(404, ctype='text/html')
        if 'oauth2.googleapis.com' in url: return R(body={'access_token': 'g-tok'})
        if 'googleAds:search' in url:
            q = kw['json']['query']
            if 'FROM customer' in q: return R(body={'results': [{'customer': {'currencyCode': 'USD', 'timeZone': 'America/New_York'}}]})
            # 23 Nov UK day = 22 Nov 19:00 .. 23 Nov 18:59 New York; one row inside (23 Nov 10 NY), one outside (22 Nov 10 NY)
            return R(body={'results': [{'segments': {'date': '2026-11-23', 'hour': '10'}, 'metrics': {'costMicros': '100000000'}},
                                       {'segments': {'date': '2026-11-22', 'hour': '10'}, 'metrics': {'costMicros': '7000000'}}]})
        return R(body={})
    return post


def test_lolalooks_is_the_new_store_and_a_failing_store_is_not_fatal():
    m = load(); calls = []
    with mock.patch.dict(os.environ, ENV, clear=False), mock.patch.object(m.requests, 'post', side_effect=fake_post(calls)):
        stores = m.open_stores()
    labels = [(l, st is not None, err) for st, l, err in stores]
    assert labels[0][0] == 'LolaLooks' and labels[0][1], labels
    lola = stores[0][0]
    assert lola.shop == 'gwvmrw-vm.myshopify.com' and lola.prefix == 'NS'
    assert lola.state_file == '_product_count_state_lolalooks.json' and lola.state_file != m.STATE_FILE
    assert stores[1][0] is not None                      # Loreya opened
    assert stores[2][0] is None and 'SOL_SHOPIFY_STORE' in stores[2][2]   # SoleVair missing -> reported, not fatal
    assert not any('old-closed' in u for u, _ in calls), 'never touches the closed store'
    assert m.anchor(stores) is lola


def test_new_lolalooks_ad_spend_uses_778_dev_token_and_uk_day():
    m = load(); calls = []
    with mock.patch.dict(os.environ, ENV, clear=False), mock.patch.object(m.requests, 'post', side_effect=fake_post(calls)):
        st = m.open_stores()[0][0]
        spend, ccy = st.ad_spend(datetime.date(2026, 11, 23))
    assert (round(spend, 2), ccy) == (100.0, 'USD')
    ads = [kw for u, kw in calls if 'googleAds:search' in u]
    assert all('/customers/7783704709/' in u for u, kw in calls if 'googleAds:search' in u)
    assert all(kw['headers'].get('developer-token') == 'dev-tok' for kw in ads)


def test_closed_store_alone_down_still_sends_others():
    m = load()
    lor = types.SimpleNamespace(label='Loreya')
    stores = [(None, 'LolaLooks', 'RuntimeError: x'), (lor, 'Loreya', None)]
    assert m.anchor(stores) is lor
