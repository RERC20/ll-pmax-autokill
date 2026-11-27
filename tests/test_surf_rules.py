# Tests for the surf scaler rules v2 (surf/rules.py): 30-minute runs, rolling 60-minute window. No network.
#   python -m unittest tests.test_surf_rules -v
import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from surf.rules import (NY, UK, UTC, Cfg, Snap, budget_floor, decide, decide_reset, last_mark,  # noqa: E402
                        manual_changes, new_day_state, ny_run_hours, parse_schedule, remaining_ny_run_hours,
                        runs_at, sales_between, spend_hourly, spend_snapshots, uk_run_hours, window_figures)

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


_mark = [0]


def S(roas=2.5, spend=30.0, prev=20.0, budget=300.0, floor=0.0, end=None, cur_run=True, end_run=True, **kw):
    """A judged window. Each call gets a new mark 60 min after the previous one unless end is given."""
    if end is None:
        _mark[0] += 1
        end = at_uk(2026, 11, 27, 7, 0) + dt.timedelta(minutes=60 * _mark[0])   # non-overlapping windows
    kw.setdefault("pace", PACE)
    return Snap(end=end, w_spend=spend, p_spend=prev, w_sales=spend * roas, day_spend=kw.pop("day_spend", 100.0),
                day_sales=kw.pop("day_sales", 250.0), end_run=end_run, cur_run=cur_run, floor=floor, budget=budget, **kw)


def run_path(cfg, roases, budget=300.0):
    st, out = new_day_state(), []
    for r in roases:
        d, st = decide(cfg, S(roas=r, budget=budget), st)
        budget = d["new"]; out.append((d["action"], budget))
    return out, st


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



class Window(unittest.TestCase):
    """How the rolling 60-minute window is measured (Ads hourly segments + cumulative-spend snapshots)."""
    H = lambda self, h: dt.datetime(2026, 11, 27, h, tzinfo=UTC)       # noqa: E731

    def test_last_mark(self):
        self.assertEqual(last_mark(dt.datetime(2026, 11, 27, 12, 20, 9, tzinfo=UTC)), self.H(12))
        self.assertEqual(last_mark(dt.datetime(2026, 11, 27, 12, 50, tzinfo=UTC)), self.H(12) + dt.timedelta(minutes=30))

    def test_hourly_window_ending_on_the_hour_is_the_full_hour(self):
        seg = {self.H(10): 12.0, self.H(11): 20.0, self.H(12): 3.0}
        now = self.H(12) + dt.timedelta(minutes=20)
        self.assertEqual(spend_hourly(seg, self.H(11), self.H(12), now), 20.0)
        self.assertEqual(spend_hourly(seg, self.H(10), self.H(11), now), 12.0)

    def test_hourly_window_ending_half_past_uses_pro_rata_partial_hour(self):
        seg = {self.H(11): 20.0, self.H(12): 10.0}                      # hour 12 read at 12:50: $10 so far
        now = self.H(12) + dt.timedelta(minutes=50)
        a, b = self.H(11) + dt.timedelta(minutes=30), self.H(12) + dt.timedelta(minutes=30)
        self.assertEqual(spend_hourly(seg, a, b, now), 10.0 + 6.0)       # half of 11 + 10 x 30/50

    def test_snapshot_delta(self):
        m0, m1 = self.H(11), self.H(12)
        snaps = {m0.isoformat(): {"ny_date": "2026-11-27", "cum": 40.0}, m1.isoformat(): {"ny_date": "2026-11-27", "cum": 61.5}}
        self.assertEqual(spend_snapshots(snaps, m0, m1, {"2026-11-27": 70.0}), 21.5)

    def test_snapshot_delta_across_ny_midnight(self):
        m0, m1 = self.H(4) + dt.timedelta(minutes=30), self.H(5) + dt.timedelta(minutes=30)    # NY 23:30 -> 00:30
        snaps = {m0.isoformat(): {"ny_date": "2026-11-26", "cum": 500.0}, m1.isoformat(): {"ny_date": "2026-11-27", "cum": 4.0}}
        self.assertEqual(spend_snapshots(snaps, m0, m1, {"2026-11-26": 506.0, "2026-11-27": 4.0}), 10.0)

    def test_snapshot_missing_or_negative_falls_back(self):
        m0, m1 = self.H(11), self.H(12)
        self.assertIsNone(spend_snapshots({}, m0, m1, {"2026-11-27": 1}))
        snaps = {m0.isoformat(): {"ny_date": "2026-11-27", "cum": 40.0}, m1.isoformat(): {"ny_date": "2026-11-27", "cum": 30.0}}
        self.assertIsNone(spend_snapshots(snaps, m0, m1, {"2026-11-27": 30.0}))

    def test_sales_by_created_at_inside_window(self):
        orders = [(self.H(11) - dt.timedelta(seconds=1), 1000), (self.H(11), 2000), (self.H(12) - dt.timedelta(seconds=1), 3000),
                  (self.H(12), 4000)]
        self.assertEqual(sales_between(orders, self.H(11), self.H(12), 1.3), 65.0)

    def test_window_figures_prefers_snapshots(self):
        end = self.H(12)
        snaps = {(end - dt.timedelta(minutes=m)).isoformat(): {"ny_date": "2026-11-27", "cum": c}
                 for m, c in ((120, 10.0), (60, 25.0), (0, 45.0))}
        f = window_figures({self.H(10): 15.0, self.H(11): 20.0}, {"2026-11-27": 45.0}, snaps, [], 1.3, end,
                           end + dt.timedelta(minutes=20), self.H(0))
        self.assertEqual((f["w_spend"], f["p_spend"], f["spend_method"]), (20.0, 15.0, "snapshots"))
        f = window_figures({self.H(10): 15.0, self.H(11): 20.0}, {"2026-11-27": 45.0}, {}, [], 1.3, end,
                           end + dt.timedelta(minutes=20), self.H(0))
        self.assertEqual((f["w_spend"], f["p_spend"], f["spend_method"]), (20.0, 15.0, "hourly"))

    def test_runs_at_minute_resolution(self):
        self.assertTrue(runs_at(at_uk(2026, 11, 27, 12, 30), SCHED))        # NY 07:30
        self.assertFalse(runs_at(at_uk(2026, 11, 27, 13, 20), SCHED))       # NY 08:20: gap
        self.assertTrue(runs_at(at_uk(2026, 11, 27, 14, 20), SCHED))        # NY 09:20


class StepUp(unittest.TestCase):
    def test_first_step_x2_from_base(self):
        d, st = decide(C, S(roas=2.5, budget=300), new_day_state())
        self.assertEqual((d["action"], d["new"], d["mult"]), ("step_up", 600, 2.0))
        self.assertTrue(st["stepped_up"])

    def test_then_x1_5(self):
        path, _ = run_path(C, [2.5, 2.5, 2.5])
        self.assertEqual(path, [("step_up", 600), ("step_up", 900), ("step_up", 1350)])

    def test_no_cap(self):
        path, st = run_path(C, [3.0] * 8)
        b = 600
        for _ in range(7):
            b = round(b * 1.5)
        self.assertEqual(path[-1], ("step_up", b))
        self.assertGreater(path[-1][1], 1500)
        self.assertEqual(st["changes"], 8)                          # and no limit on the number of changes

    def test_keep_doubling_with_surf_step_2(self):
        cfg = Cfg.from_env({"SURF_STEP": "2"})
        path, _ = run_path(cfg, [2.5, 2.5, 2.5])
        self.assertEqual([b for _, b in path], [600, 1200, 2400])

    def test_spend_not_rising_still_steps_up(self):
        d, _ = decide(C, S(roas=2.5, spend=25, prev=30, budget=600), new_day_state())
        self.assertEqual((d["action"], d["new"], d["rising"]), ("step_up", 900, False))

    def test_bad_day_roas_does_not_block(self):                        # no day guard
        st = new_day_state(); st["stepped_up"] = True
        d, _ = decide(C, S(roas=2.5, budget=600, day_spend=500, day_sales=500), st)
        self.assertEqual((d["action"], d["new"]), ("step_up", 900))

    def test_step_up_never_below_floor(self):
        d, _ = decide(C, S(roas=2.5, budget=300, floor=700.2), new_day_state())
        self.assertEqual(d["new"], 701)


class Hold(unittest.TestCase):
    def test_hold_mid(self):
        for r in (1.75, 2.0, 2.2, 2.49):
            d, st = decide(C, S(roas=r, budget=900), new_day_state())
            self.assertEqual((d["action"], d["new"]), ("hold", 900))
            self.assertFalse(st["last_bad"])

    def test_hold_mid_breaks_the_two_in_a_row(self):
        path, _ = run_path(C, [2.5, 2.5, 1.5, 2.0, 1.5], 300)
        self.assertEqual([a for a, _ in path], ["step_up", "step_up", "step_down", "hold", "step_down"])


class StepDown(unittest.TestCase):
    def test_one_notch(self):
        d, st = decide(C, S(roas=1.5, budget=1350), new_day_state())
        self.assertEqual((d["action"], d["new"]), ("step_down", 900))
        self.assertTrue(st["last_bad"])

    def test_never_below_base(self):
        d, _ = decide(C, S(roas=1.0, budget=400), new_day_state())
        self.assertEqual((d["action"], d["new"]), ("step_down", 300))
        d, _ = decide(C, S(roas=1.0, budget=300), new_day_state())
        self.assertEqual((d["action"], d["new"]), ("hold", 300))

    def test_never_below_floor(self):
        fl = budget_floor(1000, 300, 14, 4)                             # 1194.29
        d, _ = decide(C, S(roas=1.5, budget=1350, floor=fl), new_day_state())
        self.assertEqual((d["action"], d["new"]), ("step_down", 1195))

    def test_floor_above_budget_holds_not_raises(self):
        d, _ = decide(C, S(roas=1.5, budget=900, floor=950), new_day_state())
        self.assertEqual((d["action"], d["new"]), ("hold", 900))

    def test_small_step_not_written(self):
        d, _ = decide(C, S(roas=1.5, budget=305), new_day_state())
        self.assertEqual(d["action"], "hold")

    def test_one_bad_window_no_longer_locks(self):                    # the old "< 1.2 -> lock" rule is gone
        path, _ = run_path(C, [2.5, 2.5, 0.5, 2.5], 300)
        self.assertEqual(path, [("step_up", 600), ("step_up", 900), ("step_down", 600), ("step_up", 900)])


class TwoDown(unittest.TestCase):
    def test_two_in_a_row_goes_to_base_pace(self):
        path, _ = run_path(C, [2.5, 2.5, 2.5, 1.5, 1.5], 300)
        self.assertEqual(path[-2:], [("step_down", 900), ("base_pace", 300)])

    def test_base_pace_respects_floor(self):
        st = new_day_state(); st["last_bad"] = True
        d, _ = decide(C, S(roas=1.0, budget=1350, floor=700.2), st)
        self.assertEqual((d["action"], d["new"]), ("base_pace", 701))

    def test_surfs_again_after_base_pace(self):                       # no lock for the rest of the day
        path, _ = run_path(C, [2.5, 2.5, 1.5, 1.0, 2.4, 2.6, 2.6], 300)
        self.assertEqual([b for _, b in path], [600, 900, 600, 300, 300, 600, 900])
        self.assertEqual(path[5][0], "step_up")


class DayGuard(unittest.TestCase):
    """The day guard was removed (owner, 27 Nov): a bad start that recovers surfs normally."""
    def test_bad_start_then_recovery_surfs(self):
        st = new_day_state(); b = 300.0; acts = []
        for r, ds, dsa in ((1.0, 50, 50), (1.0, 100, 100), (2.5, 150, 200), (2.5, 200, 300), (2.5, 250, 400)):
            d, st = decide(C, S(roas=r, budget=b, day_spend=ds, day_sales=dsa), st)
            b = d["new"]; acts.append((d["action"], b))
        self.assertEqual(acts, [("hold", 300), ("hold", 300), ("step_up", 600), ("step_up", 900), ("step_up", 1350)])
        self.assertNotIn("guard", st)


class Guards(unittest.TestCase):
    def test_incomplete_data_skipped(self):
        d, st = decide(C, S(spend=0, roas=0), new_day_state())
        self.assertEqual(d["action"], "skip_lag")
        self.assertEqual(st["evals"], {})
        d, _ = decide(C, S(roas=3, last_half_spend=0), new_day_state())
        self.assertEqual(d["action"], "skip_lag")

    def test_paused_refused(self):
        d, _ = decide(C, S(spend=0, campaign_status="PAUSED"), new_day_state())
        self.assertEqual(d["action"], "refused")
        d, _ = decide(C, S(roas=3, campaign_status="PAUSED"), new_day_state())
        self.assertEqual((d["action"], d["new"]), ("refused", 300))

    def test_manual_override_refused(self):
        d, st = decide(C, S(roas=3, manual_recent=("14:02 UK $300->$450 via GOOGLE_ADS_WEB_CLIENT",)), new_day_state())
        self.assertEqual((d["action"], d["new"]), ("refused", 300))
        self.assertEqual(d["would"]["new"], 600)
        self.assertEqual(st["changes"], 0)

    def test_window_outside_run_hours(self):
        d, _ = decide(C, S(roas=3, end_run=False, cur_run=False), new_day_state())
        self.assertEqual(d["action"], "no_eval")

    def test_same_window_judged_once(self):
        end = at_uk(2026, 11, 27, 12, 30)
        d, st = decide(C, S(roas=3, end=end), new_day_state())
        d, st = decide(C, S(roas=3, end=end, budget=600), st)
        self.assertEqual((d["action"], d["new"]), ("already_evaluated", 600))

    def test_previous_uk_day_window_not_acted(self):
        d, _ = decide(C, S(roas=3, same_day=False), new_day_state())
        self.assertEqual(d["action"], "no_eval")


class GapCarry(unittest.TestCase):
    """A decision earned in the last window before a schedule gap is applied at the first run of the next block."""
    def test_step_up_carried_over_gap(self):
        # 13:20 UK (NY 08:20, gap): window 12:00-13:00 is good, but the run time is outside run hours
        d, st = decide(C, S(roas=3, end=at_uk(2026, 11, 27, 13, 0), cur_run=False), new_day_state())
        self.assertEqual((d["action"], d["new"]), ("carry", 300))
        self.assertEqual(st["pending"]["action"], "step_up")
        # 13:50: window 12:30-13:30 ends in the gap -> nothing judged, carry kept
        d, st = decide(C, S(roas=0, spend=5, end=at_uk(2026, 11, 27, 13, 30), cur_run=False, end_run=False), st)
        self.assertEqual(d["action"], "no_eval"); self.assertIsNotNone(st["pending"])
        # 14:20 (NY 09:20, run hours again): window 13:00-14:00 is all gap -> apply the carried step-up
        d, st = decide(C, S(roas=0, spend=0, end=at_uk(2026, 11, 27, 14, 0), end_run=False), st)
        self.assertEqual((d["action"], d["new"], d["carried"]), ("step_up", 600, True))
        self.assertIsNone(st["pending"])

    def test_carry_dropped_when_next_window_contradicts(self):
        d, st = decide(C, S(roas=3, end=at_uk(2026, 11, 27, 13, 0), cur_run=False), new_day_state())
        d, st = decide(C, S(roas=2.3, end=at_uk(2026, 11, 27, 14, 30)), st)
        self.assertEqual((d["action"], d["new"]), ("hold", 300))
        self.assertIn("dropped", " ".join(d["reasons"]))
        self.assertIsNone(st["pending"])

    def test_carry_confirmed_by_next_window_steps_once(self):
        d, st = decide(C, S(roas=3, end=at_uk(2026, 11, 27, 13, 0), cur_run=False), new_day_state())
        d, st = decide(C, S(roas=2.5, end=at_uk(2026, 11, 27, 14, 30)), st)
        self.assertEqual((d["action"], d["new"]), ("step_up", 600))

    def test_step_down_carried_over_gap(self):
        d, st = decide(C, S(roas=1.0, budget=900, end=at_uk(2026, 11, 27, 13, 0), cur_run=False), new_day_state())
        self.assertEqual(d["action"], "carry")
        d, st = decide(C, S(roas=0, spend=0, budget=900, end=at_uk(2026, 11, 27, 14, 0), end_run=False), st)
        self.assertEqual((d["action"], d["new"]), ("step_down", 600))

    def test_carry_does_not_survive_the_day(self):                    # day states are per UK day
        d, st = decide(C, S(roas=3, end=at_uk(2026, 11, 27, 23, 30), cur_run=False), new_day_state())
        d, _ = decide(C, S(roas=0, spend=0, end=at_uk(2026, 11, 28, 7, 0), end_run=False), new_day_state())
        self.assertEqual(d["action"], "no_eval")


class MinSpend(unittest.TestCase):
    """Owner 27 Nov: a step-up needs window spend >= base pace (~$21); under half of it nothing is judged."""
    def test_step_up_needs_base_pace_spend(self):
        d, st = decide(C, S(roas=10, spend=20.0, budget=600), new_day_state())
        self.assertEqual((d["action"], d["new"]), ("hold", 600))
        self.assertIn("not enough spend to judge", " ".join(d["reasons"]))
        self.assertFalse(st["stepped_up"])
        d, _ = decide(C, S(roas=10, spend=21.5, budget=600), new_day_state())
        self.assertEqual((d["action"], d["new"]), ("step_up", 900))

    def test_step_down_needs_half_pace_spend(self):
        d, st = decide(C, S(roas=0, spend=3.0, budget=900), new_day_state())
        self.assertEqual((d["action"], d["new"]), ("hold", 900))
        self.assertIn("not enough spend to judge", " ".join(d["reasons"]))
        self.assertFalse(st["last_bad"])
        d, _ = decide(C, S(roas=0, spend=11.0, budget=900), new_day_state())
        self.assertEqual((d["action"], d["new"]), ("step_down", 600))

    def test_thin_window_neither_extends_nor_breaks_bad_streak(self):
        st = new_day_state(); b = 900.0; acts = []
        for r, sp in ((1.0, 15), (0.0, 3), (1.0, 15)):
            d, st = decide(C, S(roas=r, spend=sp, budget=b), st)
            b = d["new"]; acts.append((d["action"], b))
        self.assertEqual(acts, [("step_down", 600), ("hold", 600), ("base_pace", 300)])

    def test_configurable(self):
        c = Cfg.from_env({"SURF_MIN_WINDOW_SPEND": "40", "SURF_MIN_DOWN_SHARE": "1"})
        d, _ = decide(c, S(roas=3, spend=35), new_day_state()); self.assertEqual(d["action"], "hold")
        d, _ = decide(c, S(roas=1, spend=20, budget=900), new_day_state()); self.assertEqual(d["action"], "hold")
        d, _ = decide(c, S(roas=1, spend=22, budget=900), new_day_state()); self.assertEqual(d["action"], "step_down")

    def test_pace_follows_run_hours(self):                    # no schedule: 24 run hours -> $12.50
        d, _ = decide(C, S(roas=3, spend=13, pace=12.5), new_day_state()); self.assertEqual(d["action"], "step_up")


class CountOnce(unittest.TestCase):
    """An order (and a spend minute) can count in one step-up only: next step-up needs a window ending >= T+60."""
    def test_overlapping_window_cannot_step_up_again(self):
        t = at_uk(2026, 11, 27, 12, 0)
        d, st = decide(C, S(roas=5, end=t), new_day_state())
        self.assertEqual((d["action"], d["new"]), ("step_up", 600))
        d, st = decide(C, S(roas=5, end=t + dt.timedelta(minutes=30), budget=600), st)   # shares 11:30-12:00
        self.assertEqual((d["action"], d["new"]), ("hold", 600))
        self.assertIn("already counted", " ".join(d["reasons"]))
        d, st = decide(C, S(roas=5, end=t + dt.timedelta(minutes=60), budget=600), st)   # fresh 12:00-13:00
        self.assertEqual((d["action"], d["new"]), ("step_up", 900))
        self.assertEqual([w for w, _ in st["up_windows"]], ["11:00-12:00", "12:00-13:00"])

    def test_one_order_in_two_windows_counts_once(self):
        # one GBP 50 order at 11:45 sits in both 11:00-12:00 and 11:30-12:30; only the first window may use it
        t = at_uk(2026, 11, 27, 12, 0)
        d, st = decide(C, S(roas=3.0, spend=22, end=t), new_day_state())
        d, st = decide(C, S(roas=3.0, spend=22, end=t + dt.timedelta(minutes=30), budget=600), st)
        self.assertEqual(d["action"], "hold")

    def test_step_down_is_not_blocked(self):
        t = at_uk(2026, 11, 27, 12, 0)
        d, st = decide(C, S(roas=5, end=t), new_day_state())
        d, st = decide(C, S(roas=1.0, end=t + dt.timedelta(minutes=30), budget=600), st)
        self.assertEqual((d["action"], d["new"]), ("step_down", 400))

    def test_carried_step_up_is_credited_to_its_earned_window(self):
        t = at_uk(2026, 11, 27, 13, 0)
        d, st = decide(C, S(roas=3, end=t, cur_run=False), new_day_state())
        d, st = decide(C, S(roas=0, spend=0, end=at_uk(2026, 11, 27, 14, 0), end_run=False), st)
        self.assertEqual((d["action"], st["last_up_end"]), ("step_up", t.isoformat()))
        d, st = decide(C, S(roas=3, end=at_uk(2026, 11, 27, 14, 30), budget=600), st)   # >= 13:00 + 60
        self.assertEqual((d["action"], d["new"]), ("step_up", 900))

    def test_new_day_starts_clean(self):
        self.assertIsNone(new_day_state()["last_up_end"])


class Floor(unittest.TestCase):
    def test_formula(self):
        self.assertAlmostEqual(budget_floor(500, 300, 14, 3), round((500 + PACE * 3) * 1.1, 2))

    def test_no_remaining_hours(self):
        self.assertAlmostEqual(budget_floor(800, 300, 14, 0), 880.0)


class Reset(unittest.TestCase):
    def test_reset_to_base_when_ny_day_done(self):
        d = decide_reset(C, 1350, 1500, 0)
        self.assertEqual((d["action"], d["new"]), ("reset", 300))

    def test_reset_from_uncapped_level(self):
        self.assertEqual(decide_reset(C, 6834, 0, 0)["new"], 300)

    def test_reset_respects_floor_if_ny_hours_left(self):
        self.assertEqual(decide_reset(C, 1350, 700.2, 2)["new"], 701)

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
    def test_defaults(self):
        c = Cfg.from_env({})
        self.assertEqual((c.first_mult, c.step_mult, c.down_div, c.up_roas, c.down_roas), (2.0, 1.5, 1.5, 2.5, 1.75))

    def test_threshold_env(self):
        c = Cfg.from_env({"SURF_UP_ROAS": "3", "SURF_DOWN_ROAS": "1.5"})
        self.assertEqual((c.up_roas, c.down_roas), (3.0, 1.5))
        d, _ = decide(c, S(roas=2.9), new_day_state()); self.assertEqual(d["action"], "hold")
        d, _ = decide(c, S(roas=1.6, budget=900), new_day_state()); self.assertEqual(d["action"], "hold")

    def test_step_up_threshold_is_2_5(self):
        d, _ = decide(C, S(roas=2.49), new_day_state()); self.assertEqual(d["action"], "hold")
        d, _ = decide(C, S(roas=2.5), new_day_state()); self.assertEqual(d["action"], "step_up")

    def test_env_names(self):
        c = Cfg.from_env({"SURF_FIRST_STEP": "3", "SURF_STEP": "2", "SURF_BASE": "250"})
        self.assertEqual((c.first_mult, c.step_mult, c.base), (3.0, 2.0, 250.0))

    def test_old_cap_and_change_limit_ignored(self):
        c = Cfg.from_env({"SURF_MAX_CHANGES": "5", "SURF_MAX_BUDGET": "1200", "SURF_CAP": "1500"})
        self.assertFalse(hasattr(c, "cap") or hasattr(c, "max_changes") or hasattr(c, "max_budget"))

    def test_bad_multiplier_refused(self):
        with self.assertRaises(ValueError):
            Cfg.from_env({"SURF_STEP": "1"})


if __name__ == "__main__":
    unittest.main()
