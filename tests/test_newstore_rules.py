# Tests for the new-store rules (newstore/rules.py) and data helpers (newstore/data.py). No network.
#   python -m pytest tests/test_newstore_rules.py -q
import datetime
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from newstore import config as C, data, rules           # noqa: E402
from newstore.rules import Product                       # noqa: E402

D = datetime.date
RUN = D(2026, 11, 20)


def days_ago(n):
    return RUN - datetime.timedelta(days=n)


def P(tags=(), price=40.0, pid='1', status='ACTIVE'):
    return Product(pid=pid, handle=f'h{pid}', title='t', price=price, tags=set(tags), status=status)


def ev(p, sales=(), spend=None, clicks=None, run=RUN):
    out = rules.evaluate([p], {p.pid: list(sales)}, {p.pid: spend or {}}, {p.pid: clicks or {}}, run)
    return out[0] if out else None


def flat(total, start, end):
    """Spread `total` GBP evenly over start..end (inclusive)."""
    n = (end - start).days + 1
    return {start + datetime.timedelta(days=i): total / n for i in range(n)}


# ------------------------------------------------------------------------------------------------ rule 1
class FirstSale(unittest.TestCase):
    def test_testing_product_with_one_sale_becomes_winner(self):
        d = ev(P(), sales=[days_ago(2)])
        self.assertEqual((d.rule, d.action), ('first_sale', 'tag_winner'))

    def test_old_store_sale_counts(self):          # copied order: the processedAt date is the sale day
        d = ev(P(), sales=[D(2026, 8, 1)])
        self.assertEqual(d.rule, 'first_sale')

    def test_sale_wins_over_never_sold_spend(self):
        d = ev(P(price=20), sales=[RUN], spend={RUN: 100.0})
        self.assertEqual(d.rule, 'first_sale')

    def test_already_tagged_not_retagged(self):
        self.assertNotEqual(getattr(ev(P([C.WINNER_TAG]), sales=[RUN]), 'rule', None), 'first_sale')

    def test_future_sale_ignored(self):
        self.assertIsNone(ev(P(), sales=[RUN + datetime.timedelta(days=1)]))

    def test_drafted_product_never_judged(self):
        self.assertIsNone(ev(P(status='DRAFT'), sales=[RUN]))


# ------------------------------------------------------------------------------------------------ rule 2
class NeverSold(unittest.TestCase):
    def test_spend_exactly_price_over_7_is_kept(self):        # strictly greater than
        self.assertIsNone(ev(P(price=21.0), spend={RUN: 3.0}))

    def test_spend_just_over_price_over_7_drafts(self):
        d = ev(P(price=21.0), spend={RUN: 3.01})
        self.assertEqual((d.rule, d.action), ('never_sold', 'draft'))

    def test_gbp5_cap_for_expensive_products(self):
        self.assertIsNone(ev(P(price=70.0), spend={RUN: 5.0}))   # min(10, 5) = 5, not > 5
        self.assertEqual(ev(P(price=70.0), spend={RUN: 5.01}).rule, 'never_sold')

    def test_25_clicks_drafts_even_with_low_spend(self):
        self.assertIsNone(ev(P(), spend={RUN: 0.5}, clicks={RUN: 24}))
        self.assertEqual(ev(P(), spend={RUN: 0.5}, clicks={RUN: 25}).rule, 'never_sold')

    def test_spend_outside_lookback_ignored(self):
        old = days_ago(C.NEVER_SOLD_LOOKBACK_D)                 # one day before the 30-day window
        self.assertIsNone(ev(P(price=21.0), spend={old: 50.0}))
        edge = days_ago(C.NEVER_SOLD_LOOKBACK_D - 1)            # first day inside it
        self.assertEqual(ev(P(price=21.0), spend={edge: 3.5}).rule, 'never_sold')

    def test_no_price_uses_gbp5(self):
        self.assertIsNone(ev(P(price=0), spend={RUN: 5.0}))
        self.assertEqual(ev(P(price=0), spend={RUN: 5.5}).rule, 'never_sold')


# ------------------------------------------------------------------------------------------------ rule 3
class WeakSeller(unittest.TestCase):
    W = [C.WINNER_TAG]

    def test_7d_exactly_half_price_moves(self):               # >= price / 2
        last = days_ago(7)                                     # no sale in the last 7 UK days (RUN-6..RUN)
        d = ev(P(self.W, price=40), sales=[last], spend=flat(20.0, days_ago(6), RUN))
        self.assertEqual((d.rule, d.action, d.detail['window']), ('weak_seller', 'to_last_chance', '7d'))

    def test_7d_just_under_half_price_stays(self):
        self.assertIsNone(ev(P(self.W, price=40), sales=[days_ago(7)], spend=flat(19.99, days_ago(6), RUN)))

    def test_sale_on_first_day_of_7d_window_blocks_3a(self):
        # sale 6 days ago is INSIDE the 7-day window: 3a cannot fire. 7 x GBP 3 = 21 >= 20 in the window, but 3c only
        # counts the 6 days AFTER the sale day = GBP 18 < 20 -> stays.
        self.assertIsNone(ev(P(self.W, price=40), sales=[days_ago(6)], spend=flat(21.0, days_ago(6), RUN)))
        d = ev(P(self.W, price=40), sales=[days_ago(6)], spend=flat(28.0, days_ago(6), RUN))    # 6 x 4 = 24 >= 20
        self.assertEqual((d.rule, d.detail['window']), ('weak_seller', 'since-anchor(<= 30d)'))

    def test_sale_day_spend_not_counted_in_3c(self):
        self.assertIsNone(ev(P(self.W, price=40), sales=[days_ago(3)], spend={days_ago(3): 30.0, days_ago(2): 19.99}))
        self.assertEqual(ev(P(self.W, price=40), sales=[days_ago(3)], spend={days_ago(2): 20.0}).rule, 'weak_seller')

    def test_14d_window(self):
        # last sale exactly 14 days ago; spend 8..13 days ago (outside 7d window) adds up in the 14-day window
        d = ev(P(self.W, price=40), sales=[days_ago(14)], spend=flat(20.0, days_ago(13), days_ago(8)))
        self.assertEqual(d.detail['window'], '14d')

    def test_since_sale_window_caps_at_30_days(self):
        # spend 31+ days after the last sale is outside 3c (and the product is a zombie anyway)
        d = ev(P(self.W, price=40), sales=[days_ago(35)], spend={days_ago(2): 0.0, days_ago(3): 0.0})
        self.assertEqual(d.rule, 'zombie')

    def test_entry_stamp_is_the_anchor_when_later_than_sale(self):
        # back from Last Chance 3 days ago (w_since stamp) - old sale 40 days ago must not make it a zombie
        p = P(self.W + [C.W_STAMP + days_ago(3).isoformat()], price=40)
        self.assertIsNone(ev(p, sales=[days_ago(40)]))

    def test_lc_grad_stamp_counts_as_entry(self):
        p = P(self.W + ['lc_grad:' + days_ago(5).isoformat()], price=40)
        self.assertIsNone(ev(p, sales=[days_ago(60)]))

    def test_no_price_skips_spend_rules(self):
        self.assertIsNone(ev(P(self.W, price=0), sales=[days_ago(10)], spend={days_ago(1): 999.0}))


# ------------------------------------------------------------------------------------------------ rule 4
class Zombie(unittest.TestCase):
    W = [C.WINNER_TAG]

    def test_exactly_30_days_without_sale_moves(self):
        d = ev(P(self.W), sales=[days_ago(30)])
        self.assertEqual((d.rule, d.action, d.detail['days_without_sale']), ('zombie', 'to_last_chance', 30))

    def test_29_days_stays(self):
        self.assertIsNone(ev(P(self.W), sales=[days_ago(29)]))

    def test_moves_with_zero_spend(self):
        self.assertEqual(ev(P(self.W), sales=[days_ago(45)], spend={}).rule, 'zombie')

    def test_winner_without_any_sale_uses_store_move_day(self):
        run = C.WINNERS_ANCHOR_FALLBACK + datetime.timedelta(days=30)
        self.assertEqual(ev(P(self.W), run=run).rule, 'zombie')
        self.assertIsNone(ev(P(self.W), run=run - datetime.timedelta(days=1)))


# ------------------------------------------------------------------------------------------------ rule 5
class LastChance(unittest.TestCase):
    def lc(self, entry, price=40.0):
        return P([C.LC_TAG, C.LOST_TAG, C.LC_STAMP + entry.isoformat()], price=price)

    def test_sale_after_entry_returns_to_winners(self):
        d = ev(self.lc(days_ago(10)), sales=[days_ago(20), days_ago(4)])
        self.assertEqual((d.rule, d.action, d.detail['rescue_sale']), ('lc_return', 'back_to_winners', days_ago(4).isoformat()))

    def test_sale_on_entry_day_does_not_count(self):          # pre-stamp sales never graduate (old engine rule)
        self.assertIsNone(ev(self.lc(days_ago(10)), sales=[days_ago(10)]))

    def test_spend_over_half_price_drafts(self):
        self.assertIsNone(ev(self.lc(days_ago(10), price=30), spend={days_ago(5): 15.0}))    # = 15, not > 15
        d = ev(self.lc(days_ago(10), price=30), spend={days_ago(5): 15.01})
        self.assertEqual((d.rule, d.action), ('lc_draft', 'draft_last_chance'))

    def test_gbp20_cap(self):
        self.assertIsNone(ev(self.lc(days_ago(10), price=80), spend={days_ago(5): 20.0}))
        self.assertEqual(ev(self.lc(days_ago(10), price=80), spend={days_ago(5): 20.5}).rule, 'lc_draft')

    def test_spend_before_entry_ignored(self):
        self.assertIsNone(ev(self.lc(days_ago(10), price=30), spend={days_ago(10): 50.0, days_ago(11): 50.0}))

    def test_newest_stamp_wins(self):
        p = P([C.LC_TAG, C.LC_STAMP + '2026-09-01', C.LC_STAMP + days_ago(3).isoformat()])
        self.assertIsNone(ev(p, sales=[days_ago(5)]))         # the sale is before the CURRENT stamp

    def test_lc_tag_beats_winner_tag(self):
        p = P([C.LC_TAG, C.WINNER_TAG, C.LC_STAMP + days_ago(10).isoformat()])
        self.assertEqual(rules.role(p), 'last_chance')


# ------------------------------------------------------------------------------------------------ caps
class Caps(unittest.TestCase):
    def test_over_cap_blocks_only_that_rule(self):
        ds = [rules.Decision('zombie', str(i), 'h', 'r') for i in range(5)] + [rules.Decision('first_sale', 'x', 'h', 'r')]
        allowed, blocked = rules.apply_caps(ds, {'zombie': 4, 'first_sale': 10})
        self.assertEqual(blocked, {'zombie': 5})
        self.assertEqual([d.rule for d in allowed], ['first_sale'])

    def test_at_cap_is_allowed(self):
        ds = [rules.Decision('zombie', str(i), 'h', 'r') for i in range(4)]
        self.assertEqual(len(rules.apply_caps(ds, {'zombie': 4})[0]), 4)


# ------------------------------------------------------------------------------------------------ USD -> GBP + days
class Currency(unittest.TestCase):
    def test_weekend_uses_friday_rate(self):
        raw = {D(2026, 11, 13): 0.756, D(2026, 11, 16): 0.760}   # Fri, Mon
        fx = data.fill_rates(raw, D(2026, 11, 13), D(2026, 11, 17))
        self.assertEqual([fx[D(2026, 11, d)] for d in (13, 14, 15, 16, 17)], [0.756, 0.756, 0.756, 0.760, 0.760])

    def test_missing_rate_stops(self):
        with self.assertRaises(RuntimeError):
            data.fill_rates({D(2026, 11, 16): 0.76}, D(2026, 11, 14), D(2026, 11, 16))

    def test_usd_converted_with_the_account_day_rate(self):
        day = D(2026, 11, 18)
        hourly = {('c', day): {10: 1.0}}                       # all spend at 10:00 New York = 15:00 UK same day
        sp, ck = data.align_to_uk([('p', 'c', day, 10.0, 4)], hourly, {day: 0.75})
        self.assertAlmostEqual(sp['p'][day], 7.50)
        self.assertAlmostEqual(ck['p'][day], 4)

    def test_late_new_york_spend_lands_on_next_uk_day(self):
        day = D(2026, 11, 18)
        hourly = {('c', day): {21: 3.0, 9: 1.0}}               # 21:00 NY = 02:00 UK next day
        sp, _ = data.align_to_uk([('p', 'c', day, 8.0, 0)], hourly, {day: 1.0})
        self.assertAlmostEqual(sp['p'][day], 2.0)
        self.assertAlmostEqual(sp['p'][day + datetime.timedelta(days=1)], 6.0)

    def test_no_hourly_data_splits_by_hours(self):
        s = data.uk_split(D(2026, 11, 18))                     # EST (UTC-5): hours 19-23 NY are the next UK day
        self.assertAlmostEqual(s[D(2026, 11, 18)], 19 / 24)
        self.assertAlmostEqual(s[D(2026, 11, 19)], 5 / 24)

    def test_dst_gap_week(self):
        s = data.uk_split(D(2026, 10, 28))                     # UK on GMT, US still on EDT (UTC-4): 4 h difference
        self.assertAlmostEqual(s[D(2026, 10, 29)], 4 / 24)

    def test_exchange_rate_changes_threshold_outcome(self):
        # USD 6.50 at 0.756 = GBP 4.91 (kept, under GBP 5); read as GBP it would have drafted
        day = RUN
        sp, _ = data.align_to_uk([('1', 'c', day, 6.50, 0)], {('c', day): {9: 1.0}}, {day: 0.756})
        self.assertIsNone(ev(P(price=70), spend=sp['1']))
        self.assertEqual(ev(P(price=70), spend={RUN: 6.50}).rule, 'never_sold')

    def test_item_id_parsing(self):
        self.assertEqual(data.pid_of_item('shopify_ZZ_8118985097277_44538667597885'), '8118985097277')
        self.assertEqual(data.pid_of_item('shopify_zz_8118985097277_44538667597885'), '8118985097277')
        self.assertIsNone(data.pid_of_item('12345'))


# ------------------------------------------------------------------------------------------------ orders -> sales
def order(oid, processed, created='2026-11-13T20:43:53Z', lines=(('1', 30.0),), subtotal=None, **kw):
    o = {'id': oid, 'processedAt': processed, 'createdAt': created, 'cancelledAt': None, 'test': False, 'tags': [],
         'subtotal': subtotal if subtotal is not None else sum(a for _, a in lines),
         'lineItems': [{'pid': p, 'amount': a} for p, a in lines]}
    o.update(kw)
    return o


class Sales(unittest.TestCase):
    def test_processed_at_not_created_at(self):
        s = data.sales_by_product([order('a', '2026-08-02T10:00:00Z', tags=['old-order', 'old-order-#550001'])])
        self.assertEqual(s['1'], [D(2026, 8, 2)])

    def test_cancelled_and_test_excluded(self):
        s = data.sales_by_product([order('a', '2026-11-15T13:56:24Z', cancelledAt='2026-11-15T14:00:00Z'),
                                   order('b', '2026-11-15T13:56:24Z', test=True),
                                   order('c', '2026-11-15T13:56:24Z', tags=['test-order'])])
        self.assertEqual(s, {})

    def test_zero_value_line_is_not_a_sale(self):
        s = data.sales_by_product([order('a', '2026-11-13T15:16:58Z', lines=(('1', 49.0),), subtotal=0.0)])
        self.assertEqual(s, {})

    def test_uk_day_boundary_in_summer(self):                 # 23:30 UTC in BST = 00:30 UK next day
        s = data.sales_by_product([order('a', '2026-08-01T23:30:00Z')])
        self.assertEqual(s['1'], [D(2026, 8, 2)])

    def test_two_orders_same_day_are_two_sales(self):
        s = data.sales_by_product([order('a', '2026-11-15T10:00:00Z'), order('b', '2026-11-15T11:00:00Z')])
        self.assertEqual(len(s['1']), 2)

    def test_line_without_product_skipped(self):
        s = data.sales_by_product([order('a', '2026-11-15T10:00:00Z', lines=((None, 30.0), ('2', 10.0)))])
        self.assertEqual(list(s), ['2'])


class Config(unittest.TestCase):
    def test_schedule_is_30_minutes(self):
        self.assertEqual(C.SCHEDULE_MINUTES, 30)
        self.assertEqual(len(C.RAILWAY_CRON.split()[0].split(',')), 2)    # two runs an hour = 48 a day

    def test_week1_only_testing_campaign_and_no_tree_moves(self):
        self.assertEqual(C.campaign_ids(), ['24425197606'])
        self.assertFalse(C.tree_moves_enabled())

    def test_ships_dry(self):
        self.assertFalse(C.LIVE)


if __name__ == '__main__':
    unittest.main()
