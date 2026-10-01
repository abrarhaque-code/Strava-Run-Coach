"""weekly_check: plan-driven, grades from the review, a live checkpoint, and a
useful line when there is no plan at all."""

import contextlib
import io
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import weekly_check as wc
from enrichment import enrich
from tests.helpers import make_activity, make_plan, temp_activity_data, temp_plan

MI = 1609.34
TUE_WK2 = date(2026, 5, 26)          # Tuesday of plan week 2 (plan starts Mon 2026-05-18)


def _run_on(day: str, miles: float = 5.0, hour: int = 19, hr: float = 140.0, **over):
    start = f"{day}T{hour:02d}:00:00"
    laps = []
    for i in range(int(miles)):
        laps.append({"lap_index": i + 1, "distance": MI, "moving_time": 600, "elapsed_time": 603,
                     "average_heartrate": hr})
    return enrich(make_activity(start_date_local=start, start_date=start + "Z",
                                distance=miles * MI, moving_time=int(miles * 600),
                                elapsed_time=int(miles * 603), average_heartrate=hr, laps=laps,
                                **over))


GOOD_WEEK = [_run_on("2026-05-19", 6), _run_on("2026-05-21", 6), _run_on("2026-05-23", 8.5, hour=8)]


@contextlib.contextmanager
def _world(tmp, plan=None, state=None, acts=(), with_plan=True):
    with temp_plan(Path(tmp), plan=(plan or make_plan()) if with_plan else None,
                   state=(state if state is not None else {}) if with_plan else None):
        with temp_activity_data(Path(tmp), cache_activities=list(acts)):
            with mock.patch.object(wc, "REPORT_DIR", Path(tmp) / "weekly"):
                yield


class TestReportSections(unittest.TestCase):
    def test_complete_week_then_this_week_layout_and_graded_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _world(tmp, acts=GOOD_WEEK):
                text = wc.build_weekly_report(today=TUE_WK2)
        self.assertIn("WEEKLY CHECK-IN  |  City Marathon", text)
        self.assertIn("1. LAST WEEK (wk 1, Base)", text)
        self.assertIn("Run volume:     20.5 mi of 20.0 mi (102% of target)", text)
        self.assertIn("Long run:       8.5 mi of 8.0 mi (hit)", text)
        self.assertIn("Status:         COMPLETE", text)
        self.assertIn("Verdict: executed", text)
        self.assertIn("2. THIS WEEK (wk 2 of 4, Base)", text)
        self.assertIn("7-DAY LAYOUT:", text)
        self.assertIn("> Tue May 26:", text)
        self.assertIn("Sat May 30:  Long run 8.0 mi, HR < 145", text)
        self.assertIn("3. RUNS, LAST 7 DAYS", text)
        self.assertIn("Thu May 21", text)
        self.assertIn("Sat May 23", text)
        self.assertNotIn("Tue May 19", text)                             # 7 days back, exclusive
        self.assertRegex(text, r"Sat May 23 .*  [A-D] \(\d+\)")
        self.assertIn("4. CHECKPOINT", text)
        self.assertIn("NEXT: Test DP (2026-06-01, in 6 days)", text)
        self.assertIn("criteria met so far", text)
        self.assertIn("The race is won in the weeks", text)

    def test_missed_week_adjusts_and_moves_forward(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _world(tmp, acts=[_run_on("2026-05-20", 4)]):
                text = wc.build_weekly_report(today=TUE_WK2)
        self.assertIn("Status:         MISSED", text)
        self.assertIn("Verdict: missed (20% of target)", text)
        self.assertIn("adjust and move forward", " ".join(text.split()))

    def test_volume_there_long_run_missed(self):
        acts = [_run_on("2026-05-19", 5), _run_on("2026-05-20", 5), _run_on("2026-05-21", 5),
                _run_on("2026-05-23", 4, hour=8)]
        with tempfile.TemporaryDirectory() as tmp:
            with _world(tmp, acts=acts):
                text = wc.build_weekly_report(today=TUE_WK2)
        self.assertIn("Status:         PARTIAL", text)
        self.assertIn("the long run was not", text)

    def test_data_stale_week_shows_the_record_not_zeros(self):
        state = {"weeks_actuals": {"1": {"data_stale": True, "run_mi": 18.0, "runs": 3,
                                          "long_run_mi": 8.0, "status": "complete"}}}
        with tempfile.TemporaryDirectory() as tmp:
            with _world(tmp, state=state, acts=[]):
                text = wc.build_weekly_report(today=TUE_WK2)
        self.assertIn("[DATA STALE]", text)
        self.assertIn("Recorded:       18.0 mi of 20.0 mi, 3 runs", text)
        self.assertNotIn("Status:         MISSED", text)

    def test_standing_note_prints_with_its_expiry(self):
        state = {"notes": {"2": [{"date": "2026-05-25", "text": "no speedwork until the calf settles",
                                  "until": "2026-06-10"}],
                           "general": ["2026-05-01: new shoes"]}}
        with tempfile.TemporaryDirectory() as tmp:
            with _world(tmp, state=state, acts=GOOD_WEEK):
                text = wc.build_weekly_report(today=TUE_WK2)
        self.assertIn("Standing notes (they shape the layout above):", text)
        self.assertIn("- no speedwork until the calf settles (until 2026-06-10)", text)
        self.assertIn("(general) 2026-05-01: new shoes", text)

    def test_no_plan_still_shows_recent_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _world(tmp, acts=GOOD_WEEK, with_plan=False):
                text = wc.build_weekly_report(today=TUE_WK2)
        self.assertIn("No training plan yet", text)
        self.assertIn("coach.py plan --from-data", text)
        self.assertIn("3. RUNS, LAST 7 DAYS", text)
        self.assertNotIn("1. LAST WEEK", text)
        self.assertNotIn("4. CHECKPOINT", text)

    def test_before_start_and_after_race(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _world(tmp, acts=[]):
                before = wc.build_weekly_report(today=date(2026, 5, 10))
                after = wc.build_weekly_report(today=date(2026, 11, 5))
        self.assertIn("The plan starts 2026-05-18 (8 days)", before)
        self.assertIn("Race was 4 days ago", after)


class TestHelpers(unittest.TestCase):
    def test_driver_is_the_first_miss_then_the_first_watch(self):
        vs = [{"dim": "intent", "verdict": "ok", "line": "fine."},
              {"dim": "easy", "verdict": "watch", "line": "3 of 5 easy splits under the cap. More."},
              {"dim": "quality", "verdict": "miss", "line": "2.0 mi of 8.0 mi at MP. Rest."},
              {"dim": "load", "verdict": "info", "line": "x"}]
        self.assertEqual(wc._driver(vs), "quality: 2.0 mi of 8.0 mi at MP.")
        self.assertEqual(wc._driver(vs[:2]), "easy: 3 of 5 easy splits under the cap.")
        self.assertIsNone(wc._driver(vs[:1]))

    def test_report_path_is_iso_week(self):
        y, w, _ = TUE_WK2.isocalendar()
        self.assertEqual(wc.report_path(TUE_WK2).name, f"{y}-W{w:02d}.md")

    def test_main_writes_and_reports_reconcile_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _world(tmp, acts=GOOD_WEEK):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    rc = wc.main(["--date", TUE_WK2.isoformat(), "--no-reconcile"])
                self.assertEqual(rc, 0)
                self.assertTrue(wc.report_path(TUE_WK2).exists())
                self.assertIn("Report written", out.getvalue())
                with mock.patch("reconcile.reconcile", side_effect=RuntimeError("boom")):
                    with contextlib.redirect_stdout(io.StringIO()):
                        rc = wc.main(["--date", TUE_WK2.isoformat(), "--no-write"])
                self.assertEqual(rc, 3)


if __name__ == "__main__":
    unittest.main()
