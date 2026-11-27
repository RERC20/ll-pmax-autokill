# Tests for the surf scaler rules v3 (surf/rules.py decide_v3, surf/run.py SURF_RULESET=v3, surf/backtest3.py).
#   python -m unittest tests.test_surf_v3 -v        (no network)
import datetime as dt
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from surf import backtest3, run, sales                                            # noqa: E402
from surf.rules import (NY, UK, UTC, CfgV3, SnapV3, decide_v3, floor_v3, new_state_v3, parse_schedule,  # noqa: E402
                        remaining_ny_run_hours, reset_v3, round_dollar, ruleset, runs_at, smoothed_roas,
                        spend_prorated, v3_window, window_sales_orders)

DAYS = ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY"]
ROWS = [{"campaignCriterion": {"adSchedule": {"dayOfWeek": d, "startHour": s, "endHour": e}}}
        for d in DAYS for s, e in ((2, 8), (9, 11), (13, 19))]
SCHED = parse_schedule(ROWS)
C = CfgV3()


def uk(d, h, m=0):
    return dt.datetime(2026, 11, d, h, m, tzinfo=UK).astimezone(UTC)


def snap(sm=None, spend=60.0, orders=3, budget=300.0, floor=0.0, mark=None, sales=None, **kw):
    """A window whose SMOOTHED ROAS is sm (sales solved from the formula) unless sales is given."""
    if sales is None:
        sales = sm * (spend + 20) - 2.46 * 20
    return SnapV3(mark=mark or uk(27, 12), spend=spend, sales=sales, orders=orders, in_hours=kw.pop("in_hours", True),
                  floor=floor, budget=budget, **kw)


class Window(unittest.TestCase):
    def test_window_is_3h_ending_60_min_before_mark(self):
        self.assertEqual(v3_window(uk(27, 12)), (uk(27, 8), uk(27, 11)))
        self.assertEqual(v3_window(uk(27, 12, 30)), (uk(27, 8, 30), uk(27, 11, 30)))

    def test_spend_prorated_evenly_inside_hours(self):
        seg = {uk(27, 8): 12.0, uk(27, 9): 6.0, uk(27, 10): 30.0, uk(27, 11): 60.0}
        self.assertAlmostEqual(spend_prorated(seg, uk(27, 8), uk(27, 11)), 48.0)
        self.assertAlmostEqual(spend_prorated(seg, uk(27, 8, 30), uk(27, 11, 30)), 6 + 6 + 30 + 30)
        self.assertAlmostEqual(spend_prorated(seg, uk(27, 8, 15), uk(27, 8, 45)), 6.0)
        self.assertEqual(spend_prorated(seg, uk(27, 13), uk(27, 14)), 0.0)

    def test_sales_and_orders_half_open_and_positive_only(self):
        o = [(uk(27, 8), 10.0), (uk(27, 9), 0.0), (uk(27, 10, 59), 5.0), (uk(27, 11), 99.0), (uk(27, 7, 59), 7.0)]
        self.assertEqual(window_sales_orders(o, uk(27, 8), uk(27, 11)), (15.0, 2))     # 0-sale order not counted

    def test_smoothed_roas(self):
        self.assertAlmostEqual(smoothed_roas(150, 50), (150 + 49.2) / 70)
        self.assertAlmostEqual(smoothed_roas(0, 0), 2.46)
        d, _ = decide_v3(C, snap(spend=50, sales=150), None)
        self.assertEqual((d["raw_roas"], d["smoothed_roas"]), (3.0, round(199.2 / 70, 4)))

    def test_round_dollar_half_up(self):
        self.assertEqual(round_dollar(1318.2), 1318); self.assertEqual(round_dollar(461.5), 462)
        self.assertEqual(round_dollar(600 * 1.3), 780); self.assertEqual(round_dollar(780 / 1.3), 600)

    def test_floor(self):
        self.assertAlmostEqual(floor_v3(100, 14, 7), (100 + 300 / 14 * 7) * 1.1, places=2)
        self.assertEqual(remaining_ny_run_hours(uk(27, 12), SCHED), 9)          # NY 07:00: 7, 9, 10, 13-18

    def test_marks_inside_run_hours(self):
        ins = [h for h in range(24) for m in (0, 30) if runs_at(uk(27, h, m), SCHED)]
        self.assertEqual(sorted(set(ins)), [7, 8, 9, 10, 11, 12, 14, 15, 18, 19, 20, 21, 22, 23])
        self.assertTrue(runs_at(uk(27, 3), None))

    def test_ruleset_switch(self):
        self.assertEqual(ruleset({}), "v2"); self.assertEqual(ruleset({"SURF_RULESET": "v3"}), "v3")
        self.assertEqual(ruleset({"SURF_RULESET": " V3 "}), "v3"); self.assertEqual(ruleset({"SURF_RULESET": "v2"}), "v2")

    def test_env_overrides(self):
        c = CfgV3.from_env({"SURF3_UP_ROAS": "2.8", "SURF3_UP_MIN_ORDERS": "2"})
        self.assertEqual((c.up_roas, c.up_min_orders, c.base), (2.8, 2, 300.0))
        with self.assertRaises(ValueError):
            CfgV3.from_env({"SURF3_STEP_MULT": "1"})


class Decide(unittest.TestCase):
    def test_skip_under_10_dollars(self):
        d, st = decide_v3(C, snap(3.5, spend=9.99), {"last_bad": True})
        self.assertEqual(d["action"], "skip"); self.assertTrue(st["last_bad"])        # not judged: streak kept
        self.assertEqual(decide_v3(C, snap(2.0, spend=10.0), None)[0]["action"], "hold")

    def test_incomplete_last_hour_skips_live_only(self):
        self.assertEqual(decide_v3(C, snap(2.0, live=True, last_hour_incomplete=True), None)[0]["action"], "skip")
        self.assertEqual(decide_v3(C, snap(2.0, live=False, last_hour_incomplete=True), None)[0]["action"], "hold")

    def test_out_of_hours_no_action_no_state(self):
        st0 = dict(new_state_v3(), last_bad=True)
        d, st = decide_v3(C, snap(1.0, spend=80, in_hours=False), st0)
        self.assertEqual((d["action"], d["budget_after"]), ("out_of_hours", 300)); self.assertEqual(st, st0)

    def test_step_up_from_base_doubles(self):
        d, st = decide_v3(C, snap(3.0, spend=45, orders=3), None)
        self.assertEqual((d["action"], d["budget_after"], d["mult"]), ("step_up", 600, 2.0))
        self.assertFalse(st["at_base"]); self.assertEqual(st["last_up_mark"], uk(27, 12).isoformat())

    def test_step_up_not_at_base_x13_rounded(self):
        st = dict(new_state_v3(), at_base=False)
        self.assertEqual(decide_v3(C, snap(3.2, budget=600), st)[0]["budget_after"], 780)
        self.assertEqual(decide_v3(C, snap(3.2, budget=1014), st)[0]["budget_after"], 1318)

    def test_step_up_conditions(self):
        for kw in ({"sm": 2.999}, {"sm": 3.5, "spend": 44.99}, {"sm": 3.5, "orders": 2}):
            d, st = decide_v3(C, snap(**kw), None)
            self.assertEqual(d["action"], "hold", kw); self.assertIsNone(st["last_up_mark"])
        self.assertIn("2 orders < 3", decide_v3(C, snap(3.5, orders=2), None)[0]["reason"])

    def test_count_once(self):
        _, st = decide_v3(C, snap(3.5, mark=uk(27, 12)), None)                            # 300 -> 600
        d, st2 = decide_v3(C, snap(3.5, budget=600, mark=uk(27, 14, 30)), st)
        self.assertEqual(d["action"], "hold"); self.assertIn("count-once", d["reason"]); self.assertIn("15:00", d["reason"])
        d, _ = decide_v3(C, snap(3.5, budget=600, mark=uk(27, 15)), st2)                  # T + 180: allowed
        self.assertEqual((d["action"], d["budget_after"]), ("step_up", 780))
        d, _ = decide_v3(C, snap(1.0, spend=40, budget=600, mark=uk(27, 12, 30)), st)    # downs not blocked
        self.assertEqual((d["action"], d["budget_after"]), ("step_down", 462))

    def test_hold_band(self):
        for sm in (1.75, 2.5, 2.999):
            d, st = decide_v3(C, snap(sm, spend=80), {"last_bad": True})
            self.assertEqual(d["action"], "hold"); self.assertFalse(st["last_bad"])

    def test_step_down(self):
        d, st = decide_v3(C, snap(1.7, spend=30, budget=780), None)
        self.assertEqual((d["action"], d["budget_after"]), ("step_down", 600)); self.assertTrue(st["last_bad"])
        self.assertEqual(decide_v3(C, snap(1.0, spend=30, budget=1000), None)[0]["budget_after"], 769)  # 769.23

    def test_step_down_never_below_floor_or_base(self):
        self.assertEqual(decide_v3(C, snap(1.0, spend=40, budget=350), None)[0]["budget_after"], 300)
        d, st = decide_v3(C, snap(1.0, spend=40, budget=600, floor=500.2), None)
        self.assertEqual(d["budget_after"], 501); self.assertFalse(st["at_base"])          # floor rounded up
        d, st = decide_v3(C, snap(1.0, spend=40, budget=390), None)
        self.assertEqual(d["budget_after"], 300); self.assertTrue(st["at_base"])          # landed on $300 = base
        d, _ = decide_v3(C, snap(1.0, spend=40, budget=320, floor=400), None)
        self.assertEqual((d["action"], d["budget_after"]), ("step_down", 320))            # floor never raises it

    def test_low_spend_bad_window_holds_and_breaks_streak(self):
        d, st = decide_v3(C, snap(1.0, spend=29.99), {"last_bad": True})
        self.assertEqual(d["action"], "hold"); self.assertFalse(st["last_bad"])

    def test_base_pace_after_two_consecutive_judged_bad_marks(self):
        _, st = decide_v3(C, snap(1.0, spend=40, budget=1318, mark=uk(27, 12)), dict(new_state_v3(), at_base=False))
        _, st = decide_v3(C, snap(1.0, spend=5, budget=1014, mark=uk(27, 12, 30)), st)      # skip: transparent
        _, st = decide_v3(C, snap(1.0, spend=40, budget=1014, in_hours=False, mark=uk(27, 13)), st)  # off hours
        d, st = decide_v3(C, snap(1.0, spend=40, budget=1014, floor=320.4, mark=uk(27, 14)), st)
        self.assertEqual((d["action"], d["budget_after"]), ("base_pace", 321)); self.assertTrue(st["at_base"])
        d, st = decide_v3(C, snap(1.0, spend=40, budget=321, floor=100, mark=uk(27, 14, 30)), st)
        self.assertEqual((d["action"], d["budget_after"]), ("base_pace", 300))             # still bad: base pace again
        d, st = decide_v3(C, snap(1.0, spend=40, budget=300, floor=450, mark=uk(27, 15)), st)
        self.assertEqual(d["budget_after"], 300)                                            # base pace never raises

    def test_hold_between_breaks_streak(self):
        _, st = decide_v3(C, snap(1.0, spend=40, budget=780), None)
        _, st = decide_v3(C, snap(2.0, spend=40, budget=600, mark=uk(27, 12, 30)), st)
        d, _ = decide_v3(C, snap(1.0, spend=40, budget=600, mark=uk(27, 13)), st)
        self.assertEqual(d["action"], "step_down")

    def test_recovery_after_base_pace_steps_up_x2_from_floor_value(self):
        st = dict(new_state_v3(), at_base=True, last_bad=True)
        d, st = decide_v3(C, snap(1.0, spend=40, budget=500, floor=340.0), st)
        self.assertEqual((d["action"], d["budget_after"]), ("base_pace", 340))
        d, st = decide_v3(C, snap(3.4, budget=340, mark=uk(27, 12, 30)), st)
        self.assertEqual((d["action"], d["budget_after"], d["mult"]), ("step_up", 680, 2.0))

    def test_manual_override_pauses_live_only(self):
        d, st = decide_v3(C, snap(3.5, live=True, manual_recent=("12:10 UK $300->$500",)), None)
        self.assertEqual((d["action"], d["budget_after"]), ("paused", 300)); self.assertIsNone(st["last_up_mark"])
        self.assertEqual(decide_v3(C, snap(3.5, manual_recent=("x",)), None)[0]["action"], "step_up")

    def test_already_evaluated(self):
        _, st = decide_v3(C, snap(2.0), None)
        self.assertEqual(decide_v3(C, snap(3.5), st)[0]["action"], "already_evaluated")

    def test_reset_keeps_count_once_and_streak(self):
        st = dict(new_state_v3(), at_base=False, last_bad=True, last_up_mark=uk(27, 23).isoformat())
        r, st2 = reset_v3(C, st, 1014)
        self.assertEqual((r["action"], r["budget_after"]), ("reset", 300))
        self.assertTrue(st2["at_base"]); self.assertTrue(st2["last_bad"]); self.assertEqual(st2["last_up_mark"], st["last_up_mark"])
        self.assertEqual(reset_v3(C, st, 300)[0]["action"], "reset_noop")


class Fake:
    def __init__(self, budget=300.0, seg=None, events=(), status="ENABLED"):
        self.micros, self.seg, self.events, self.writes, self.status = int(budget * 1e6), dict(seg or {}), list(events), [], status

    def campaign(self, a=None):
        return {"id": "1", "name": "t", "status": self.status, "serving_status": "SERVING",
                "budget_rn": run.gads.BUDGET_RN, "amount_micros": self.micros, "explicitly_shared": False, "period": "DAILY"}

    def schedule_rows(self, a=None):
        return ROWS

    def budget_events(self, now, a=None):
        return self.events

    def testing_spend(self, ny_from, ny_to, a=None):
        days = {}
        d = dt.date.fromisoformat(ny_from)
        while d <= dt.date.fromisoformat(ny_to):
            days[d.isoformat()] = 0.0; d += dt.timedelta(days=1)
        for t, v in self.seg.items():
            if t.astimezone(NY).date().isoformat() in days:
                days[t.astimezone(NY).date().isoformat()] += v
        return self.seg, days

    def set_budget(self, micros, a=None):
        self.writes.append(micros); self.micros = micros
        return dt.datetime.now(UTC)


class RunV3(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SURF_DATA_DIR"] = self.tmp.name
        self.store = run.Store(os.path.join(self.tmp.name, "s.json"))
        self.msgs = []

    def tearDown(self):
        self.tmp.cleanup()

    def go(self, f, now, orders=(), live=False, mode="window"):
        calls = []

        def to(shop, a, b, drop_store_credit=False):
            calls.append(drop_store_credit)
            return [o for o in orders if a <= o[0] < b]
        with mock.patch.multiple(run.gads, campaign=f.campaign, schedule_rows=f.schedule_rows,
                                 budget_events=f.budget_events, testing_spend=f.testing_spend, set_budget=f.set_budget), \
                mock.patch.object(run.sales, "testing_orders", to), \
                mock.patch.object(run.sales, "gbp_usd", lambda d: (1.25, "test")), \
                mock.patch.object(run, "telegram", lambda text, html_mode=True: self.msgs.append(text) or True), \
                mock.patch.object(run, "log_line", lambda r: None):
            fn = run.window_run_v3 if mode == "window" else run.reset_run_v3
            rec = fn(now, CfgV3(), live, self.store)
        if mode == "window":
            self.assertEqual(calls, [True])                       # store-credit orders always dropped in v3
        return rec

    SEG = {uk(27, 8): 20.0, uk(27, 9): 20.0, uk(27, 10): 20.0, uk(27, 11): 10.0}
    ORD = [(uk(27, 8, 10), 6000), (uk(27, 9, 10), 6000), (uk(27, 10, 50), 6000), (uk(27, 11, 5), 9999)]

    def test_dry_run_step_up_and_table(self):
        rec = self.go(Fake(seg=self.SEG), uk(27, 12, 20), self.ORD)       # mark 12:00, window 08:00-11:00
        d = rec["decision"]
        self.assertEqual((d["spend"], d["sales"], d["orders"]), (60.0, 225.0, 3))   # smoothed 3.43
        self.assertEqual((d["action"], d["budget_before"], d["budget_after"]), ("step_up", 300, 600))
        self.assertEqual(self.store.load()["v3"]["virtual_budget"], 600)
        m = self.msgs[-1]
        for col in ("window", "spend", "sales", "ord", "raw", "smooth", "action", "old→new", "floor", "Reason:",
                    "08:00-11:00", "300→600", "UP x2", "DRY RUN", "v3"):
            self.assertIn(col, m)

    def test_dry_run_never_writes_and_count_once_across_runs(self):
        f = Fake(seg=self.SEG)
        self.go(f, uk(27, 12, 20), self.ORD)
        rec = self.go(f, uk(27, 12, 50), self.ORD + [(uk(27, 11, 20), 6000)])          # mark 12:30
        self.assertEqual(rec["decision"]["action"], "hold"); self.assertIn("count-once", rec["decision"]["reason"])
        self.assertEqual(f.writes, [])

    def test_rerun_same_mark_is_not_judged_twice(self):
        f = Fake(seg=self.SEG)
        self.go(f, uk(27, 12, 20), self.ORD)
        self.assertEqual(self.go(f, uk(27, 12, 25), self.ORD)["decision"]["action"], "already_evaluated")

    def test_live_write_verify(self):
        f = Fake(budget=780, seg=self.SEG)
        st = self.store.load(); st["v3"] = dict(new_state_v3(780), at_base=False); self.store.save(st)
        rec = self.go(f, uk(27, 12, 20), [(uk(27, 9), 2000)], live=True)              # $25 on $60: down
        self.assertEqual((rec["decision"]["action"], rec["decision"]["budget_after"]), ("step_down", 600))
        self.assertEqual(f.writes, [600_000_000]); self.assertIn("Verified: yes", self.msgs[-1])

    def test_live_manual_override_pauses(self):
        t = uk(27, 12, 0).astimezone(NY).strftime("%Y-%m-%d %H:%M:%S.%f")
        ev = [{"changeEvent": {"changeDateTime": t, "clientType": "GOOGLE_ADS_WEB_CLIENT",
                               "oldResource": {"campaignBudget": {"amountMicros": "300000000"}},
                               "newResource": {"campaignBudget": {"amountMicros": "500000000"}}}}]
        f = Fake(seg=self.SEG, events=ev)
        rec = self.go(f, uk(27, 12, 20), self.ORD, live=True)
        self.assertEqual(rec["decision"]["action"], "paused"); self.assertEqual(f.writes, [])

    def test_live_incomplete_last_hour_skips(self):
        seg = dict(self.SEG); seg.pop(uk(27, 10))                         # last window hour (10:00) has no data
        self.assertEqual(self.go(Fake(seg=seg), uk(27, 12, 20), self.ORD, live=True)["decision"]["action"], "skip")

    def test_out_of_hours_mark(self):
        rec = self.go(Fake(seg=self.SEG), uk(27, 13, 20), self.ORD)       # 13:00 UK = NY 08:00: gap
        self.assertEqual(rec["decision"]["action"], "out_of_hours"); self.assertIn("OFF HRS", self.msgs[-1])

    def test_reset(self):
        st = self.store.load(); st["v3"] = dict(new_state_v3(1014), at_base=False, virtual_budget=1014); self.store.save(st)
        rec = self.go(Fake(), uk(28, 0, 5), mode="reset")
        self.assertEqual((rec["decision"]["action"], rec["decision"]["budget_after"]), ("reset", 300))
        self.assertEqual(self.store.load()["v3"]["virtual_budget"], 300)

    def test_main_dispatches_on_ruleset(self):
        with mock.patch.dict(os.environ, {"SURF_RULESET": "v3", "SURF_LIVE": "0"}), \
                mock.patch.object(run, "window_run_v3") as w3, mock.patch.object(run, "window_run") as w2:
            run.main(["--mode", "window"])
        self.assertTrue(w3.called); self.assertFalse(w2.called)
        with mock.patch.dict(os.environ, {"SURF_RULESET": ""}), \
                mock.patch.object(run, "window_run_v3") as w3, mock.patch.object(run, "window_run") as w2:
            run.main(["--mode", "window"])
        self.assertFalse(w3.called); self.assertTrue(w2.called)


class SalesFilter(unittest.TestCase):
    def test_store_credit_dropped_only_when_asked(self):
        def o(i, credit):
            return {"id": i, "created": uk(27, 9), "credit": credit}
        orders = [o(1, None), o(2, "store_credit"), o(3, "low_revenue")]
        with mock.patch.multiple(sales.hourly, _pull=lambda shop, q: orders, split_cfg=lambda: {"cutover": None, "min_prior": 1},
                                 history_orders=lambda shop, c: ([], 0), total_exclusion=lambda x: None,
                                 credit_reason=lambda x: x["credit"],
                                 split_order=lambda x, h, m: ({"testing": {"p": 100 * x["id"]}}, [])), \
                mock.patch.object(sales.hourly, "History") as H:
            H.return_value.add.return_value = None
            self.assertEqual([p for _, p in sales.testing_orders(None, uk(27, 8), uk(27, 10))], [100, 200, 300])
            self.assertEqual([p for _, p in sales.testing_orders(None, uk(27, 8), uk(27, 10), True)], [100, 300])


class Backtest(unittest.TestCase):
    def test_reset_schedule_switch_and_csv(self):
        seg = {uk(23, h): 20.0 for h in range(8, 24)}
        seg.update({uk(24, h): 20.0 for h in range(0, 24)})
        orders = [(uk(23, h, 15), 70.0) for h in range(8, 22)]                      # ROAS 3.5, 3 orders per window
        rows, ch = backtest3.simulate(seg, orders, SCHED, uk(23, 12), uk(24, 13))
        by = {r["mark"]: r for r in rows}
        self.assertEqual(by[uk(23, 12)]["action"], "step_up")                       # 08-11 window: 60/210/3
        self.assertEqual(by[uk(23, 14, 30)]["action"], "hold")                      # count-once
        self.assertEqual(by[uk(23, 15)]["action"], "step_up")
        self.assertEqual(by[uk(24, 0, 30)]["budget_before"], 300)                    # 00:05 reset
        self.assertEqual(by[uk(24, 3)]["action"], "base_pace")                      # no schedule before 12:42 UK 24 Nov
        self.assertEqual(by[uk(24, 12, 30)]["action"], "base_pace")
        self.assertEqual(by[uk(24, 13)]["action"], "out_of_hours")                  # schedule from 24 Nov 07:42 NY
        self.assertIn((uk(23, 12, 20), 600.0), ch)
        self.assertEqual(list(next(backtest3.csv_rows(rows)).keys()), backtest3.CSV_COLS)

    def test_permutation_p(self):
        outs = [1, 1, 1, 0, 0, 0, 0, 0, 0, 0]
        self.assertAlmostEqual(backtest3.perm_p(outs, [0, 1, 2], n=2000), 1 / 120, delta=0.01)
        self.assertEqual(backtest3.perm_p(outs, [3, 4], n=100), 1.0)


if __name__ == "__main__":
    unittest.main()
