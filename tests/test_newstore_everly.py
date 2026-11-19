# Everly mode of the new-store engine: ONE campaign (Testing only), store in USD + New York time, caps in USD.
# Every rule is fired (and not fired) with Everly's settings. No network.
#   python -m pytest tests/test_newstore_everly.py -q
import datetime
import importlib
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

EVERLY = {
    'NS_SHOPIFY_STORE': 'z6hjj1-ae.myshopify.com', 'NS_GOOGLE_CUSTOMER_ID': '1230989417',
    'NS_TESTING_CAMPAIGN_ID': '111', 'NS_TESTING_AG_ID': '222',
    'NS_WINNERS_CAMPAIGN_ID': '', 'NS_LC_CAMPAIGN_ID': '',
    'NS_STORE_TIMEZONE': 'America/New_York', 'NS_STORE_CURRENCY': 'USD',
    'NS_NEVER_SOLD_CAP': '6.5', 'NS_LC_CAP': '26', 'NS_CUTOVER': '2026-11-19T10:00:00+00:00',
    'NS_WINNERS_LAUNCH_DATE': '2026-11-19', 'NS_ORDERS_SINCE': '2026-11-18', 'NS_ACCOUNT_SPEND_FROM': '2026-11-19',
    'NS_LABEL': 'Everly',
}
_saved = {}
C = data = rules = notify = actions = None


def setUpModule():
    global C, data, rules, notify, actions
    for k, v in EVERLY.items():
        _saved[k] = os.environ.get(k)
        os.environ[k] = v
    from newstore import config, data as d, rules as r, notify as n, actions as a
    C = importlib.reload(config)
    rules = importlib.reload(r)
    data = importlib.reload(d)
    notify = importlib.reload(n)
    actions = importlib.reload(a)


def tearDownModule():
    for k, v in _saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    from newstore import config, data as d, rules as r, notify as n, actions as a
    importlib.reload(config); importlib.reload(r); importlib.reload(d); importlib.reload(n); importlib.reload(a)


D = datetime.date
RUN = D(2026, 12, 20)


def ago(n):
    return RUN - datetime.timedelta(days=n)


def P(tags=(), price=55.0, pid='1', status='ACTIVE'):
    return rules.Product(pid=pid, handle=f'h{pid}', title='t', price=price, tags=set(tags), status=status)


def ev(p, sales=(), spend=None, clicks=None, run=RUN):
    out = rules.evaluate([p], {p.pid: list(sales)}, {p.pid: spend or {}}, {p.pid: clicks or {}}, run)
    return out[0] if out else None


def flat(total, start, end):
    n = (end - start).days + 1
    return {start + datetime.timedelta(days=i): total / n for i in range(n)}


class Settings(unittest.TestCase):
    def test_one_campaign_only(self):
        self.assertEqual(C.campaign_ids(), ['111'])

    def test_usd_new_york_caps(self):
        self.assertEqual((C.STORE_CURRENCY, C.STORE_TIMEZONE, C.NEVER_SOLD_CAP_GBP, C.LC_CAP_GBP), ('USD', 'America/New_York', 6.5, 26.0))
        self.assertEqual(str(data.UK), 'America/New_York')

    def test_zombie_clock_on(self):
        self.assertEqual(C.winners_launch(), D(2026, 11, 19))


class SpendIsUsdOnNewYorkDays(unittest.TestCase):
    class FakeAds:
        run_ops = 0

        def __init__(self, rows, hourly):
            self.rows, self.hourly = rows, hourly

        def stream(self, q):
            assert 'campaign.id IN (111)' in q, q          # only the one Testing campaign is read
            return self.rows if 'shopping_performance_view' in q else self.hourly

    def test_no_conversion_and_same_day(self):
        rows = [{'campaign': {'id': '111'}, 'segments': {'date': '2026-12-19', 'productItemId': 'shopify_US_42_99'},
                 'metrics': {'costMicros': '7250000', 'clicks': '3'}}]
        hourly = [{'campaign': {'id': '111'}, 'segments': {'date': '2026-12-19', 'hour': 23}, 'metrics': {'costMicros': '7250000'}}]
        orig = data.ecb_usd_gbp
        data.ecb_usd_gbp = lambda *a: (_ for _ in ()).throw(AssertionError('ECB must not be called for USD stores'))
        try:
            spend, clicks, info = data.fetch_spend(self.FakeAds(rows, hourly), RUN)
        finally:
            data.ecb_usd_gbp = orig
        self.assertEqual(spend, {'42': {D(2026, 12, 19): 7.25}})   # NY 23:00 stays on 19 Dec (UK would be 20 Dec)
        self.assertEqual(clicks, {'42': {D(2026, 12, 19): 3.0}})
        self.assertEqual(info['fx_latest'], 1.0)

    def test_item_id_us_prefix(self):
        self.assertEqual(data.pid_of_item('shopify_US_10365181591850_52980065239338'), '10365181591850')


class SalesUseNewYorkDayAndCutover(unittest.TestCase):
    def order(self, ts, pid='5', amount=55.0, **kw):
        o = {'id': ts, 'name': '#EL1', 'processedAt': ts, 'createdAt': ts, 'cancelledAt': None, 'test': False, 'tags': [],
             'subtotal': amount, 'list_total': amount, 'discount_codes': [], 'discount_titles': [],
             'lineItems': [{'pid': pid, 'amount': amount}]}
        o.update(kw)
        return o

    def test_new_york_day(self):
        self.assertEqual(data.sales_by_product([self.order('2026-11-21T03:00:00Z')]), {'5': [D(2026, 11, 20)]})

    def test_before_cutover_ignored(self):           # old winners do not come back from old sales
        self.assertEqual(data.sales_by_product([self.order('2026-11-19T09:59:00Z')]), {})

    def test_store_credit_order_ignored(self):
        o = self.order('2026-11-21T03:00:00Z', subtotal=0.0, discount_codes=['CREDIT-ABC123'])
        self.assertEqual(data.sales_by_product([o]), {})

    def test_cancelled_ignored(self):
        self.assertEqual(data.sales_by_product([self.order('2026-11-21T03:00:00Z', cancelledAt='2026-11-21T04:00:00Z')]), {})


class Rule1FirstSale(unittest.TestCase):
    def test_sale_makes_winner(self):
        self.assertEqual(ev(P(), sales=[ago(1)]).rule, 'first_sale')

    def test_old_winner_is_testing_again(self):      # was_w_campaign = history only
        self.assertEqual(ev(P(['was_w_campaign']), sales=[ago(1)]).rule, 'first_sale')
        self.assertEqual(ev(P(['was_w_campaign']), spend={RUN: 7.0}).rule, 'never_sold')


class Rule2NeverSold(unittest.TestCase):
    def test_cap_usd_6_50(self):                      # $55 / 7 = $7.86 -> cap $6.50 applies
        self.assertIsNone(ev(P(), spend={RUN: 6.5}))
        self.assertEqual(ev(P(), spend={RUN: 6.51}).rule, 'never_sold')

    def test_price_over_7_when_lower(self):           # $35 / 7 = $5.00
        self.assertIsNone(ev(P(price=35), spend={RUN: 5.0}))
        self.assertEqual(ev(P(price=35), spend={RUN: 5.01}).rule, 'never_sold')

    def test_25_clicks(self):
        self.assertIsNone(ev(P(), clicks={RUN: 24}))
        self.assertEqual(ev(P(), clicks={RUN: 25}).rule, 'never_sold')

    def test_30_day_window(self):
        self.assertIsNone(ev(P(), spend={ago(30): 100.0}))
        self.assertEqual(ev(P(), spend={ago(29): 7.0}).rule, 'never_sold')


class Rule3WeakSeller(unittest.TestCase):
    W = ['w_campaign', 'w_since:2026-11-20']

    def test_3a_7_days(self):                         # last sale 8 days ago, $28 in 7 days >= $55/2
        d = ev(P(self.W), sales=[ago(8)], spend=flat(28, ago(6), RUN))
        self.assertEqual((d.rule, d.detail['window']), ('weak_seller', '7d'))

    def test_3a_not_enough_spend(self):
        self.assertIsNone(ev(P(self.W), sales=[ago(8)], spend=flat(27, ago(6), RUN)))

    def test_3b_14_days(self):
        d = ev(P(self.W), sales=[ago(15)], spend=flat(28, ago(13), ago(8)))
        self.assertEqual((d.rule, d.detail['window']), ('weak_seller', '14d'))

    def test_3c_since_sale(self):                     # sold 3 days ago, $28 spent since
        d = ev(P(self.W), sales=[ago(3)], spend=flat(28, ago(2), RUN))
        self.assertEqual(d.rule, 'weak_seller')

    def test_recent_seller_kept(self):
        self.assertIsNone(ev(P(self.W), sales=[RUN], spend=flat(100, ago(6), ago(1))))


class Rule4Zombie(unittest.TestCase):
    def test_30_days_no_sale(self):
        d = ev(P(['w_campaign', 'w_since:2026-11-20']), sales=[D(2026, 11, 20)], run=D(2026, 12, 20))
        self.assertEqual(d.rule, 'zombie')

    def test_29_days_kept(self):
        self.assertIsNone(ev(P(['w_campaign', 'w_since:2026-11-20']), sales=[D(2026, 11, 20)], run=D(2026, 12, 19)))


class Rule5LastChance(unittest.TestCase):
    L = ['lc_campaign', 'lc:2026-12-10']

    def test_sale_after_entry_returns(self):
        self.assertEqual(ev(P(self.L), sales=[D(2026, 12, 15)]).rule, 'lc_return')

    def test_sale_on_entry_day_does_not_count(self):
        self.assertIsNone(ev(P(self.L), sales=[D(2026, 12, 10)]))

    def test_cap_26(self):                            # $60 / 2 = $30 -> cap $26
        self.assertIsNone(ev(P(self.L, price=60), spend=flat(26, D(2026, 12, 11), RUN)))
        self.assertEqual(ev(P(self.L, price=60), spend=flat(26.5, D(2026, 12, 11), RUN)).rule, 'lc_draft')

    def test_half_price_when_lower(self):             # $40 / 2 = $20
        self.assertEqual(ev(P(self.L, price=40), spend=flat(20.5, D(2026, 12, 11), RUN)).rule, 'lc_draft')


class ReportAndWrites(unittest.TestCase):
    def test_report_says_everly_usd(self):
        rep = {'run_date': '2026-12-20', 'live': True, 'account': '1230989417', 'roles': {},
               'spend': {'usd_total': 12.5, 'fx_latest': 1.0}, 'rules': {}, 'writes': {}}
        txt = notify.summary(rep)
        self.assertIn('Auto-Kill v2 (Everly)', txt)
        self.assertIn('1 USD = USD 1.0000', txt)

    def test_reason_in_usd(self):
        self.assertIn('USD', ev(P(), spend={RUN: 7.0}).reason)

    def test_first_sale_write(self):
        calls = []

        class Shop:
            def gql(self, q, v):
                calls.append((q.split('(')[0].split('{')[0].strip(), v))
                return {'data': {'metafieldsSet': {'userErrors': []}, 'tagsAdd': {'userErrors': []}, 'tagsRemove': {'userErrors': []}}}

        p = P(['was_w_campaign'])
        d = ev(p, sales=[ago(1)])
        self.assertEqual(actions.apply(Shop(), d, p, RUN), 'ok')
        mf = calls[0][1]['mf'][0]
        self.assertEqual((mf['namespace'], mf['key'], mf['value']), ('mm-google-shopping', 'custom_label_1', 'w_campaign'))
        self.assertIn('w_campaign', calls[1][1]['add'])
        self.assertIn('w_since:' + ago(1).isoformat(), calls[1][1]['add'])


class ShopGuard(unittest.TestCase):
    class Shop:
        def __init__(self, cur, tz, dom='z6hjj1-ae.myshopify.com'):
            self.v = {'shop': {'currencyCode': cur, 'ianaTimezone': tz, 'myshopifyDomain': dom}}

        def read(self, q, v=None):
            return self.v

    def test_matching_store_passes(self):
        data.check_shop(self.Shop('USD', 'America/New_York'))

    def test_wrong_currency_stops(self):
        with self.assertRaises(RuntimeError):
            data.check_shop(self.Shop('GBP', 'America/New_York'))

    def test_wrong_store_stops(self):
        with self.assertRaises(RuntimeError):
            data.check_shop(self.Shop('USD', 'America/New_York', 'gwvmrw-vm.myshopify.com'))


if __name__ == '__main__':
    unittest.main()
