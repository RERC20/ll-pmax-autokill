# Wiring tests for surf/run.py with Google Ads / Shopify / Telegram faked. No network.
#   python -m pytest tests/test_surf_run.py -q
import datetime as dt
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from surf import run                                             # noqa: E402
from surf.rules import UK, UTC, Cfg                              # noqa: E402

DAYS = ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY"]
SCHED = [{"campaignCriterion": {"adSchedule": {"dayOfWeek": d, "startHour": s, "endHour": e}}}
         for d in DAYS for s, e in ((2, 8), (9, 11), (13, 19))]


def body(date, hours):
    """hourly.py-shaped body: hours = {uk_hour: (cost_usd, sales_usd)}"""
    hs = [{"hour": h, "split": {"testing": {"cost_usd": hours.get(h, (0, 0))[0], "sales_usd": hours.get(h, (0, 0))[1],
                                            "cost_gbp": 0, "sales_gbp": 0}}} for h in range(24)]
    return {"ads": {"ok": True}, "shopify": {"ok": True}, "split_rules": {"ok": True},
            "fx": {"gbp_usd_source": "test"}, "days": [{"date": date, "gbp_usd": 1.3, "hours": hs}]}


class Fake:
    def __init__(self, budget=300.0, status="ENABLED", ny_spend=0.0, events=()):
        self.micros, self.status, self.ny_spend, self.events, self.writes = int(budget * 1e6), status, ny_spend, list(events), []
        self.ny_dates = []

    def campaign(self, a=None):
        return {"id": "1", "name": "t", "status": self.status, "serving_status": "SERVING",
                "budget_rn": run.gads.BUDGET_RN, "amount_micros": self.micros, "explicitly_shared": False, "period": "DAILY"}

    def schedule_rows(self, a=None):
        return SCHED

    def budget_events(self, now, a=None):
        return self.events

    def ny_day_spend(self, d, a=None):
        self.ny_dates.append(d); return self.ny_spend

    def set_budget(self, micros, a=None):
        self.writes.append(micros); self.micros = micros
        return dt.datetime.now(UTC)


class HourlyRun(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["SURF_DATA_DIR"] = self.tmp
        self.store = run.Store(os.path.join(self.tmp, "s.json"))
        self.sent = []

    def go(self, fake, now, hours, live=False):
        with mock.patch.multiple(run.gads, campaign=fake.campaign, schedule_rows=fake.schedule_rows,
                                 budget_events=fake.budget_events, ny_day_spend=fake.ny_day_spend,
                                 set_budget=fake.set_budget), \
             mock.patch.object(run.hourly, "build", lambda a, b, shop: body(a.isoformat(), hours)), \
             mock.patch.object(run, "telegram", lambda t: self.sent.append(t) or True):
            return run.hourly_run(now, Cfg(), live, self.store)

    def test_dry_run_steps_virtually_and_never_writes(self):
        f = Fake()
        now = dt.datetime(2026, 11, 27, 11, 50, tzinfo=UK).astimezone(UTC)        # evaluates UK 10:00-11:00
        rec = self.go(f, now, {9: (20, 60), 10: (20, 60)})
        self.assertEqual(rec["decision"]["action"], "step_up")
        self.assertEqual(rec["decision"]["new"], 600)
        self.assertEqual(f.writes, [])
        self.assertIn("DRY RUN", self.sent[-1]); self.assertIn("Would set budget: $300 -> $600", self.sent[-1])
        # next hour follows the virtual $600 path
        rec = self.go(f, now + dt.timedelta(hours=1), {9: (20, 60), 10: (20, 60), 11: (30, 90)})
        self.assertEqual((rec["decision"]["old"], rec["decision"]["new"]), (600, 900))
        self.assertEqual(f.writes, [])

    def test_live_writes_and_verifies(self):
        f = Fake()
        now = dt.datetime(2026, 11, 27, 11, 50, tzinfo=UK).astimezone(UTC)
        rec = self.go(f, now, {9: (20, 60), 10: (20, 60)}, live=True)
        self.assertEqual(f.writes, [600_000_000])
        self.assertTrue(rec["decision"]["verified"])
        # its own write is not a manual change in the next run
        f.events = [{"changeEvent": {"changeDateTime": (dt.datetime.now(UTC).astimezone(run.NY)).strftime("%Y-%m-%d %H:%M:%S.%f"),
                                     "clientType": "GOOGLE_ADS_API",
                                     "oldResource": {"campaignBudget": {"amountMicros": "300000000"}},
                                     "newResource": {"campaignBudget": {"amountMicros": "600000000"}}}}]
        rec = self.go(f, dt.datetime.now(UTC), {})
        self.assertEqual(rec["manual_recent"], [])

    def test_floor_uses_ny_account_day(self):
        f = Fake(budget=1350, ny_spend=1000)
        st = self.store.load(); st["days"]["2026-11-27"] = {"evals": {}, "stepped_up": True, "guard": False,
                                                          "base_lock": False, "changes": 2}; self.store.save(st)
        now = dt.datetime(2026, 11, 27, 20, 50, tzinfo=UK).astimezone(UTC)        # NY 15:50 -> hours 15..18 left
        rec = self.go(f, now, {19: (50, 75)}, live=True)                             # ROAS 1.5: one notch down
        self.assertEqual(f.ny_dates[-1], "2026-11-27")
        self.assertEqual(rec["remaining_ny_run_hours"], 4)
        self.assertEqual(rec["decision"]["action"], "step_down")
        self.assertEqual(rec["decision"]["new"], 1195)                               # floor, not 900
        self.assertEqual(f.writes, [1_195_000_000])

    def test_after_uk_midnight_floor_is_previous_ny_day(self):
        f = Fake()
        now = dt.datetime(2026, 11, 28, 0, 50, tzinfo=UK).astimezone(UTC)          # NY 27 Nov 19:50
        rec = self.go(f, now, {23: (10, 0)})
        self.assertEqual(f.ny_dates[-1], "2026-11-27")
        self.assertEqual(rec["h_date"], "2026-11-27")
        self.assertFalse(rec["cur_run"])

    def test_manual_override_refused(self):
        t = (dt.datetime.now(UTC) - dt.timedelta(minutes=20)).astimezone(run.NY).strftime("%Y-%m-%d %H:%M:%S.%f")
        f = Fake(events=[{"changeEvent": {"changeDateTime": t, "clientType": "GOOGLE_ADS_WEB_CLIENT",
                                          "oldResource": {"campaignBudget": {"amountMicros": "300000000"}},
                                          "newResource": {"campaignBudget": {"amountMicros": "300000000"}}}}])
        now = dt.datetime.now(UTC)
        uk = now.astimezone(UK)
        hours = {(uk.hour - 1) % 24: (20, 60)}
        rec = self.go(f, now, hours, live=True)
        if rec["decision"]["action"] not in ("no_eval", "skip_lag"):
            self.assertIn(rec["decision"]["action"], ("refused", "hold", "hold_outside_run"))
        self.assertEqual(len(rec["manual_recent"]), 1)
        self.assertEqual(f.writes, [])

    def test_paused_campaign_never_written(self):
        f = Fake(status="PAUSED")
        now = dt.datetime(2026, 11, 27, 11, 50, tzinfo=UK).astimezone(UTC)
        rec = self.go(f, now, {10: (20, 60)}, live=True)
        self.assertEqual(rec["decision"]["action"], "refused")
        self.assertEqual(f.writes, [])


if __name__ == "__main__":
    unittest.main()
