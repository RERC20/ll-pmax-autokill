# Wiring tests for surf/run.py (rules v2) with Google Ads / Shopify / Telegram faked. No network.
#   python -m unittest tests.test_surf_run -v
import datetime as dt
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from surf import run                                             # noqa: E402
from surf.rules import NY, UK, UTC, Cfg                          # noqa: E402

DAYS = ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY"]
SCHED = [{"campaignCriterion": {"adSchedule": {"dayOfWeek": d, "startHour": s, "endHour": e}}}
         for d in DAYS for s, e in ((2, 8), (9, 11), (13, 19))]


def uk(d, h, m):
    return dt.datetime(2026, 11, d, h, m, tzinfo=UK).astimezone(UTC)


class Fake:
    """seg: {utc_hour_start: usd} (Testing hourly spend as Ads reports it)."""
    def __init__(self, budget=300.0, status="ENABLED", seg=None, events=()):
        self.micros, self.status, self.seg, self.events, self.writes = int(budget * 1e6), status, dict(seg or {}), list(events), []

    def campaign(self, a=None):
        return {"id": "1", "name": "t", "status": self.status, "serving_status": "SERVING",
                "budget_rn": run.gads.BUDGET_RN, "amount_micros": self.micros, "explicitly_shared": False, "period": "DAILY"}

    def schedule_rows(self, a=None):
        return SCHED

    def budget_events(self, now, a=None):
        return self.events

    def testing_spend(self, ny_from, ny_to, a=None):
        days = {}
        d = dt.date.fromisoformat(ny_from)
        while d <= dt.date.fromisoformat(ny_to):
            days[d.isoformat()] = 0.0; d += dt.timedelta(days=1)
        for t, v in self.seg.items():
            k = t.astimezone(NY).date().isoformat()
            if k in days:
                days[k] += v
        return self.seg, days

    def ny_day_spend(self, d, a=None):
        return sum(v for t, v in self.seg.items() if t.astimezone(NY).date().isoformat() == d)

    def set_budget(self, micros, a=None):
        self.writes.append(micros); self.micros = micros
        return dt.datetime.now(UTC)


class Run(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SURF_DATA_DIR"] = self.tmp.name
        self.store = run.Store(os.path.join(self.tmp.name, "s.json"))
        self.msgs = []

    def tearDown(self):
        self.tmp.cleanup()

    def go(self, f, now, orders=(), live=False, mode="window", cfg=None):
        sent = lambda text, html_mode=True: self.msgs.append(text) or True     # noqa: E731
        with mock.patch.multiple(run.gads, campaign=f.campaign, schedule_rows=f.schedule_rows,
                                 budget_events=f.budget_events, testing_spend=f.testing_spend,
                                 ny_day_spend=f.ny_day_spend, set_budget=f.set_budget), \
                mock.patch.object(run.sales, "testing_orders", lambda shop, a, b: [o for o in orders if a <= o[0] < b]), \
                mock.patch.object(run.sales, "gbp_usd", lambda d: (1.25, "test")), \
                mock.patch.object(run, "telegram", sent), mock.patch.object(run, "log_line", lambda r: None):
            fn = run.window_run if mode == "window" else run.reset_run
            return fn(now, cfg or Cfg(), live, self.store)

    def test_dry_run_step_up_virtual_budget_and_table_message(self):
        # Fri 27 Nov 12:20 UK (NY 07:20, run hours): window 11:00-12:00 UK
        f = Fake(seg={uk(27, 10, 0): 15.0, uk(27, 11, 0): 20.0})
        rec = self.go(f, uk(27, 12, 20), orders=[(uk(27, 11, 10), 4000)])            # GBP 40 -> $50, ROAS 2.5
        d = rec["decision"]
        self.assertEqual((d["window"], d["w_spend"], d["p_spend"], d["w_sales"]), ("11:00-12:00", 20.0, 15.0, 50.0))
        self.assertEqual((d["action"], d["new"]), ("step_up", 600))
        self.assertEqual(f.writes, [])                                              # dry run never writes
        self.assertEqual(self.store.load()["virtual_budget"], 600)
        m = self.msgs[-1]
        self.assertIn("DRY RUN", m); self.assertIn("<pre>", m)
        for col in ("window", "spend", "prev", "rise", "sales", "ROAS", "dROAS", "action", "old→new", "floor"):
            self.assertIn(col, m)
        self.assertIn("11:00-12:00", m); self.assertIn("300→600", m); self.assertIn("UP x2", m)

    def test_next_run_uses_virtual_budget_and_snapshots(self):
        f = Fake(seg={uk(27, 10, 0): 15.0, uk(27, 11, 0): 20.0, uk(27, 12, 0): 10.0})
        self.go(f, uk(27, 12, 20), orders=[(uk(27, 11, 10), 4000)])
        st = self.store.load()
        self.assertIn(uk(27, 12, 0).isoformat(), st["snaps"])
        f.seg[uk(27, 12, 0)] = 12.0
        rec = self.go(f, uk(27, 12, 50), orders=[(uk(27, 11, 10), 4000), (uk(27, 12, 5), 4000)])
        d = rec["decision"]
        self.assertEqual(rec["figures"]["spend_method"], "hourly")                  # mark 11:30 / 10:30 readings missing
        self.assertEqual(d["w_spend"], round(10.0 + 12.0 * 30 / 50, 2))
        self.assertEqual((d["old"], d["action"], d["new"]), (600, "step_up", 900))

    def test_live_write_and_verify(self):
        f = Fake(budget=900, seg={uk(27, 10, 0): 15.0, uk(27, 11, 0): 20.0})
        rec = self.go(f, uk(27, 12, 20), orders=[(uk(27, 11, 10), 2000)], live=True)  # $25 / $20 = 1.25: down
        self.assertEqual((rec["decision"]["action"], rec["decision"]["new"]), ("step_down", 600))
        self.assertEqual(f.writes, [600_000_000])
        self.assertTrue(rec["decision"]["verified"])
        self.assertIn("LIVE", self.msgs[-1]); self.assertIn("Verified: yes", self.msgs[-1])

    def test_manual_override_refused(self):
        t = (uk(27, 12, 0)).astimezone(NY).strftime("%Y-%m-%d %H:%M:%S.%f")
        f = Fake(seg={uk(27, 10, 0): 15.0, uk(27, 11, 0): 20.0},
                 events=[{"changeEvent": {"changeDateTime": t, "clientType": "GOOGLE_ADS_WEB_CLIENT",
                                          "oldResource": {"campaignBudget": {"amountMicros": "300000000"}},
                                          "newResource": {"campaignBudget": {"amountMicros": "300000000"}}}}])
        rec = self.go(f, uk(27, 12, 20), orders=[(uk(27, 11, 10), 4000)], live=True)
        self.assertEqual(rec["decision"]["action"], "refused")
        self.assertEqual(len(rec["manual_recent"]), 1)
        self.assertEqual(f.writes, [])

    def test_paused_campaign_never_written(self):
        f = Fake(status="PAUSED", seg={uk(27, 10, 0): 15.0, uk(27, 11, 0): 20.0})
        rec = self.go(f, uk(27, 12, 20), orders=[(uk(27, 11, 10), 4000)], live=True)
        self.assertEqual(rec["decision"]["action"], "refused")
        self.assertEqual(f.writes, [])

    def test_gap_carry_over_end_to_end(self):
        f = Fake(seg={uk(27, 11, 0): 15.0, uk(27, 12, 0): 20.0})
        rec = self.go(f, uk(27, 13, 20), orders=[(uk(27, 12, 10), 4000)])          # NY 08:20: gap
        self.assertEqual(rec["decision"]["action"], "carry")
        rec = self.go(f, uk(27, 14, 20), orders=[(uk(27, 12, 10), 4000)])          # NY 09:20: next block
        self.assertEqual((rec["decision"]["action"], rec["decision"]["new"]), ("step_up", 600))
        self.assertIn("UP x2*", self.msgs[-1])

    def test_incomplete_data_skipped(self):
        f = Fake(seg={uk(27, 10, 0): 15.0})                                        # hour 11 missing
        rec = self.go(f, uk(27, 12, 20), orders=[(uk(27, 11, 10), 4000)])
        self.assertEqual(rec["decision"]["action"], "skip_lag")
        self.assertIn("SKIP data", self.msgs[-1])

    def test_reset(self):
        f = Fake(budget=2025)
        st = self.store.load(); st["virtual_budget"] = 2025; self.store.save(st)
        rec = self.go(f, uk(28, 0, 5), mode="reset")
        self.assertEqual((rec["decision"]["action"], rec["decision"]["new"]), ("reset", 300))
        self.assertEqual(self.store.load()["virtual_budget"], 300)
        self.assertIn("RESET", self.msgs[-1])

    def test_old_state_file_is_upgraded(self):
        self.store.path.write_text('{"days": {"2026-11-27": {"evals": {"8": {}}, "guard": false}}, "writes": []}')
        st = self.store.load()
        self.assertEqual((st["v"], st["days"]), (2, {}))

    def test_auto_mode(self):
        self.assertEqual(run.auto_mode(uk(28, 0, 5)), "reset")
        self.assertEqual(run.auto_mode(uk(27, 12, 5)), "idle")
        self.assertEqual(run.auto_mode(uk(27, 12, 20)), "window")
        self.assertEqual(run.auto_mode(uk(27, 12, 50)), "window")


if __name__ == "__main__":
    unittest.main()
