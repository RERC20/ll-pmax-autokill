# Tests for the surf scaler rules (surf/rules.py). No network.
#   python -m pytest tests/test_surf_rules.py -q
import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from surf.rules import (NY, UK, UTC, Cfg, Snap, budget_floor, decide, decide_reset, manual_changes,  # noqa: E402
                        new_day_state, ny_run_hours, ny_spend_from_body, parse_schedule, remaining_ny_run_hours,
                        uk_run_hours)

DAYS = ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY"]
ROWS = [{"campaignCriterion": {"adSchedule": {"dayOfWeek": d, "startHour": s, "endHour": e,
                                              "startMinute": "ZERO", "endMinute": "ZERO"}}}
        for d in DAYS for s, e in ((2, 8), (9, 11), (13, 19))]
SCHED = parse_schedule(ROWS)
UK_RUN = [7, 8, 9, 10, 11, 12, 14, 15, 18, 19, 20, 21, 22, 23]
C = Cfg()
PACE = 300 / 14


def at_uk(y, m, d, h, mi=50):
    return dt.datetime(y, m, d, h, mi, tzinfo=UK).astimezone(UTC)


def S(roas=2.5, spend=30.0, day_roas=2.5, day_spend=100.0, budget=300.0, h=10, floor=0.0, cur_run=True, **kw):
    return Snap(h=h, h_spend=spend, h_sales=spend * roas, day_spend=day_spend, day_sales=day_spend * day_roas,
                uk_run_hours=kw.pop("run", UK_RUN), cur_run=cur_run, ny_run_hours_count=14, floor=floor,
                budget=budget, **kw)


class Schedule(unittest.TestCase):
    def test_uk_run_hours_winter(self):              # GMT vs EST: 5 h
        self.assertEqual(uk_run_hours(dt.date(2026, 11, 27), SCHED), UK_RUN)

    def test_uk_run_hours_summer(self):              # BST vs EDT: still 5 h
        self.assertEqual(uk_run_hours(dt.date(2026, 7, 1), SCHED), UK_RUN)

    def test_uk_run_hours_mismatch_week(self):       # UK back on GMT 25 Oct, NY still EDT until 1 Nov: 4 h
        self.assertEqual(uk_run_hours(dt.date(2026, 10, 28), SCHED), [6, 7, 8, 9, 10, 11, 13, 14, 17, 18, 19, 20, 21, 22])

    def test_ny_run_hours(self):
        self.assertEqual(ny_run_hours(dt.date(2026, 11, 27), SCHED), [2, 3, 4, 5, 6, 7, 9, 10, 13, 14, 15, 16, 17, 18])

    def test_no_schedule_runs_all_day(self):
        self.assertIsNone(parse_schedule([]))
        self.assertEqual(len(uk_run_hours(dt.date(2026, 11, 27), None)), 24)

    def test_start_hour_zero_omitted(self):          # the API omits startHour 0
        s = parse_schedule([{"campaignCriterion": {"adSchedule": {"dayOfWeek": "FRIDAY", "endHour": 3}}}])
        self.assertEqual(ny_run_hours(dt.date(2026, 11, 27), s), [0, 1, 2])

    def test_remaining_counts_current_ny_hour(self):
        self.assertEqual(remaining_ny_run_hours(at_uk(2026, 11, 27, 10), SCHED), 11)   # NY 05:50 -> 5,6,7,9,10,13-18
        self.assertEqual(remaining_ny_run_hours(at_uk(2026, 11, 27, 23), SCHED), 1)    # NY 18:50 -> 18
        self.assertEqual(remaining_ny_run_hours(at_uk(2026, 11, 28, 0, 5), SCHED), 0)  # NY 19:05 Fri: day done
        self.assertEqual(remaining_ny_run_hours(at_uk(2026, 11, 28, 4), SCHED), 0)     # NY 23:50 Fri
        self.assertEqual(remaining_ny_run_hours(at_uk(2026, 11, 28, 5), SCHED), 14)    # NY 00:50 Sat: new NY day


class Floor(unittest.TestCase):
    def test_formula(self):
        self.assertAlmostEqual(budget_floor(500, 300, 14, 3), round((500 + PACE * 3) * 1.1, 2))

    def test_no_remaining_hours(self):
        self.assertAlmostEqual(budget_floor(800, 300, 14, 0), 880.0)

    def test_step_down_never_below_floor(self):
        fl = budget_floor(1000, 300, 14, 4)          # 1194.29
        d, _ = decide(C, S(roas=1.5, budget=1350, floor=fl), new_day_state())
        self.assertEqual(d["action"], "step_down")
        self.assertEqual(d["new"], 1195.0)            # not 900

    def test_base_pace_never_below_floor(self):
        fl = budget_floor(700, 300, 14, 2)
        d, st = decide(C, S(roas=0.5, budget=1350, floor=fl), new_day_state())
        self.assertEqual(d["action"], "base_pace")
        self.assertGreaterEqual(d["new"], fl)
        self.assertTrue(st["base_lock"])

    def test_floor_above_budget_holds_not_raises(self):
        d, _ = decide(C, S(roas=1.5, budget=900, floor=950), new_day_state())
        self.assertEqual(d["action"], "hold")
        self.assertEqual(d["new"], 900)

    def test_floor_uses_ny_day_not_uk_day(self):
        # UK 27 Nov 02:00-03:00 = NY 26 Nov 21:00: belongs to NY 26 Nov, not to NY 27 Nov.
        body = {"days": [{"date": "2026-11-27", "hours": [
            {"hour": 2, "split": {"testing": {"cost_usd": 40.0}}},
            {"hour": 7, "split": {"testing": {"cost_usd": 10.0}}},
            {"hour": 8, "split": {"testing": {"cost_usd": 12.0}}}]}]}
        now = at_uk(2026, 11, 27, 9)
        self.assertEqual(ny_spend_from_body(body, "2026-11-27", now), 22.0)
        self.assertEqual(ny_spend_from_body(body, "2026-11-26", now), 40.0)


class StepUp(unittest.TestCase):
    def test_ladder(self):
        st = new_day_state(); b = 300.0; path = []
        for h in (8, 9, 10, 11, 12):
            d, st = decide(C, S(budget=b, h=h, spend=0.8 * b / 14), st)
            b = d["new"]; path.append(b)
        self.assertEqual(path, [600, 900, 1350, 1500, 1500])
        self.assertEqual(d["action"], "hold")

    def test_pace_needed(self):
        d, _ = decide(C, S(budget=600, spend=0.69 * 600 / 14), new_day_state())
        self.assertEqual(d["action"], "hold")
        self.assertIn("70% of hourly pace", d["reasons"][0])

    def test_day_roas_needed(self):
        d, _ = decide(C, S(day_roas=2.1), new_day_state())
        self.assertEqual(d["action"], "hold")

    def test_hour_roas_needed(self):
        d, _ = decide(C, S(roas=2.19), new_day_state())
        self.assertEqual(d["action"], "hold")

    def test_day_guard_after_step_up(self):
        st = new_day_state()
        d, st = decide(C, S(h=8), st); self.assertEqual(d["new"], 600)
        d, st = decide(C, S(h=9, budget=600, roas=2.0, day_roas=2.1), st)   # day ROAS dips: guard on
        self.assertTrue(st["guard"])
        d, st = decide(C, S(h=10, budget=600, roas=5, day_roas=3.0, spend=50), st)   # recovers - still no step up
        self.assertEqual(d["action"], "hold")
        self.assertIn("day guard", d["reasons"][-1])

    def test_max_budget_guard(self):
        c = Cfg(max_budget=1000); c.cap = min(c.cap, c.max_budget)
        d, _ = decide(c, S(budget=900, spend=60), new_day_state())
        self.assertEqual(d["new"], 1000)


class StepDown(unittest.TestCase):
    def test_one_notch(self):
        up = new_day_state(); up["stepped_up"] = True
        for b, want in ((1350, 900), (900, 600), (600, 400), (400, 300)):
            d, _ = decide(C, S(roas=1.5, budget=b), up)
            self.assertEqual((d["action"], d["new"]), ("step_down", want))

    def test_at_base_bad_hour_is_hold_no_lock(self):
        d, st = decide(C, S(roas=0.0, budget=300), new_day_state())
        self.assertEqual(d["action"], "hold")
        self.assertFalse(st["base_lock"])

    def test_very_bad_goes_to_base_and_locks(self):
        st = new_day_state()
        d, st = decide(C, S(roas=1.1, budget=1350, h=9), st)
        self.assertEqual((d["action"], d["new"]), ("base_pace", 300))
        d, st = decide(C, S(roas=4, budget=300, h=10, spend=40), st)
        self.assertEqual(d["action"], "hold")
        self.assertIn("base pace locked", d["reasons"][-1])

    def test_two_in_a_row(self):
        st = new_day_state()
        d, st = decide(C, S(roas=1.6, budget=1350, h=11), st); self.assertEqual(d["new"], 900)
        d, st = decide(C, S(roas=1.6, budget=900, h=12), st)
        self.assertEqual((d["action"], d["new"]), ("base_pace", 300))

    def test_two_in_a_row_across_schedule_gap(self):  # 12 and 14 are consecutive run hours
        st = new_day_state()
        d, st = decide(C, S(roas=1.6, budget=1350, h=12, cur_run=False), st)
        self.assertEqual(d["action"], "hold_outside_run")       # 13:50: current hour 13 is off schedule
        d, st = decide(C, S(roas=1.6, budget=1350, h=14), st)
        self.assertEqual((d["action"], d["new"]), ("base_pace", 300))


    def test_floor_held_budget_counts_as_base(self):
        # budget a few dollars over base only because of the floor: a bad hour neither locks nor micro-steps
        d, st = decide(C, S(roas=0.0, budget=317, floor=316.5), new_day_state())
        self.assertEqual(d["action"], "hold")
        self.assertFalse(st["base_lock"])

    def test_reset_floor_budget_is_not_a_surf_level(self):
        # no step-up today and below the first step: a budget left on the floor by the reset is not stepped down
        d, st = decide(C, S(roas=0.0, budget=522, floor=488), new_day_state())
        self.assertEqual(d["action"], "hold")
        self.assertFalse(st["base_lock"])

    def test_manual_raise_is_stepped_down_after_bad_hour(self):
        d, _ = decide(C, S(roas=1.5, budget=3000), new_day_state())
        self.assertEqual((d["action"], d["new"]), ("step_down", 1500))

    def test_small_step_not_written(self):
        d, _ = decide(C, S(roas=1.5, budget=500, floor=495), new_day_state())
        self.assertEqual(d["action"], "hold")

    def test_step_up_from_floor_held_budget_goes_to_first_step(self):
        d, _ = decide(C, S(budget=317, spend=20), new_day_state())
        self.assertEqual((d["action"], d["new"]), ("step_up", 600))


class Guards(unittest.TestCase):
    def test_outside_run_hours_hold_with_would(self):
        d, _ = decide(C, S(cur_run=False), new_day_state())
        self.assertEqual(d["action"], "hold_outside_run")
        self.assertEqual(d["would"], {"action": "step_up", "new": 600})
        self.assertEqual(d["new"], 300)

    def test_h_outside_run_hours(self):
        d, _ = decide(C, S(h=13), new_day_state())
        self.assertEqual(d["action"], "no_eval")

    def test_manual_override(self):
        d, _ = decide(C, S(manual_recent=("15:22 UK $300->$600 via GOOGLE_ADS_WEB_CLIENT",)), new_day_state())
        self.assertEqual(d["action"], "refused")

    def test_paused(self):
        d, _ = decide(C, S(campaign_status="PAUSED"), new_day_state())
        self.assertEqual(d["action"], "refused")

    def test_lag_skip(self):
        d, st = decide(C, S(spend=0.0), new_day_state())
        self.assertEqual(d["action"], "skip_lag")
        self.assertEqual(st["evals"], {})

    def test_max_changes(self):
        st = new_day_state(); st["changes"] = 8
        d, _ = decide(C, S(), st)
        self.assertEqual(d["action"], "refused")

    def test_no_double_action_same_hour(self):
        d, st = decide(C, S(), new_day_state())
        d, st = decide(C, S(budget=600), st)
        self.assertEqual(d["action"], "already_evaluated")


class Reset(unittest.TestCase):
    def test_reset_to_base_when_ny_day_done(self):
        d = decide_reset(C, 1350, 1500, 0)
        self.assertEqual((d["action"], d["new"]), ("reset", 300))

    def test_reset_respects_floor_if_ny_hours_left(self):
        d = decide_reset(C, 1350, 700.2, 2)
        self.assertEqual(d["new"], 701)

    def test_reset_noop(self):
        self.assertEqual(decide_reset(C, 300, 0, 0)["action"], "reset_noop")

    def test_reset_refused_on_manual(self):
        self.assertEqual(decide_reset(C, 900, 0, 0, manual_recent=("x",))["action"], "refused")


class Manual(unittest.TestCase):
    def ev(self, t, old, new, client="GOOGLE_ADS_WEB_CLIENT"):
        return {"changeEvent": {"changeDateTime": t, "clientType": client,
                                "oldResource": {"campaignBudget": {"amountMicros": str(old)}},
                                "newResource": {"campaignBudget": {"amountMicros": str(new)}}}}

    def test_unmatched_change_is_manual(self):
        now = dt.datetime(2026, 11, 27, 15, 0, tzinfo=UTC)          # NY 10:00
        evs = [self.ev("2026-11-27 09:30:01.5", 300_000_000, 600_000_000)]
        self.assertEqual(len(manual_changes(evs, [], now)), 1)

    def test_own_write_is_not_manual(self):
        now = dt.datetime(2026, 11, 27, 15, 0, tzinfo=UTC)
        evs = [self.ev("2026-11-27 09:30:01.5", 300_000_000, 600_000_000, "GOOGLE_ADS_API")]
        own = [{"at": "2026-11-27T14:30:00+00:00", "old_micros": 300_000_000, "new_micros": 600_000_000}]
        self.assertEqual(manual_changes(evs, own, now), [])

    def test_old_change_ignored(self):
        now = dt.datetime(2026, 11, 27, 15, 0, tzinfo=UTC)
        evs = [self.ev("2026-11-27 08:59:00", 300_000_000, 600_000_000)]
        self.assertEqual(manual_changes(evs, [], now), [])


class Config(unittest.TestCase):
    def test_env(self):
        c = Cfg.from_env({"SURF_MAX_CHANGES": "5", "SURF_BASE": "250", "SURF_MAX_BUDGET": "1200"})
        self.assertEqual((c.max_changes, c.base, c.cap), (5, 250.0, 1200.0))


if __name__ == "__main__":
    unittest.main()
