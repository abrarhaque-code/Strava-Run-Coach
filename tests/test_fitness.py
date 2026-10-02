"""Tests for fitness_tracker TSS and CTL/ATL/TSB load computation."""

import unittest
from datetime import datetime, date, timedelta

from fitness_tracker import compute_tss, compute_loads


class TestComputeTss(unittest.TestCase):
    def test_hr_based_positive(self):
        run = {"moving_min": 45.0, "dist_mi": 4.5, "avg_hr": 150.0}
        self.assertGreater(compute_tss(run), 0.0)

    def test_capped_at_200(self):
        # Absurdly long, very high HR -> would exceed 200, must be capped.
        run = {"moving_min": 600.0, "dist_mi": 60.0, "avg_hr": 250.0}
        self.assertLessEqual(compute_tss(run), 200.0)
        self.assertGreater(compute_tss(run), 0.0)

    def test_pace_fallback_no_hr(self):
        # No avg_hr -> pace-based path. Still positive.
        run = {"moving_min": 40.0, "dist_mi": 4.0, "avg_hr": None}
        self.assertGreater(compute_tss(run), 0.0)

    def test_zero_duration_is_zero(self):
        run = {"moving_min": 0.0, "dist_mi": 0.0, "avg_hr": 150.0}
        self.assertEqual(compute_tss(run), 0.0)


class TestComputeLoads(unittest.TestCase):
    def test_series_has_keys(self):
        today = date.today()
        runs = [
            {"date": datetime.combine(today - timedelta(days=5),
                                      datetime.min.time()),
             "dist_mi": 4.0, "moving_min": 40.0, "avg_hr": 150.0},
            {"date": datetime.combine(today - timedelta(days=2),
                                      datetime.min.time()),
             "dist_mi": 6.0, "moving_min": 60.0, "avg_hr": 148.0},
        ]
        series = compute_loads(runs, days_back=30)
        self.assertTrue(series)
        last = series[-1]
        for key in ("date", "tss", "ctl", "atl", "tsb"):
            self.assertIn(key, last)
        # Some training happened, so fitness should be above zero by the end.
        self.assertGreater(last["ctl"], 0.0)


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# Guards: CTL warm-up, consecutive-day overreach
# ---------------------------------------------------------------------------

from fitness_tracker import (  # noqa: E402
    CTL_WARMUP_DAYS, MIN_HISTORY_DAYS, classify_phase, history_days,
    history_is_sufficient, tsb_context,
)


def _sessions(*day_offsets):
    return [{"date": datetime.now() - timedelta(days=d)} for d in day_offsets]


class TestHistoryDepth(unittest.TestCase):
    def test_history_days_empty(self):
        self.assertEqual(history_days([]), 0)

    def test_history_days_spans_earliest_session(self):
        self.assertEqual(history_days(_sessions(90, 3, 1)), 90)

    def test_short_history_is_insufficient(self):
        self.assertFalse(history_is_sufficient(_sessions(21, 1)))

    def test_long_history_is_sufficient(self):
        self.assertTrue(history_is_sufficient(_sessions(120, 1)))

    def test_warmup_shorter_than_min_history(self):
        self.assertGreater(MIN_HISTORY_DAYS, CTL_WARMUP_DAYS)


class TestClassifyPhaseGuard(unittest.TestCase):
    def test_short_history_blocks_overreaching_call(self):
        phase, advice = classify_phase(13.4, 39.0, -31.8, 98, hist_days=21)
        self.assertEqual(phase, "UNRELIABLE")
        self.assertIn("21d", advice)

    def test_full_history_gives_real_phase(self):
        phase, _ = classify_phase(24.0, 34.1, -15.2, 97, hist_days=120)
        self.assertEqual(phase, "BUILDING")

    def test_omitting_history_preserves_legacy_behaviour(self):
        phase, _ = classify_phase(13.4, 39.0, -31.8, 98)
        self.assertEqual(phase, "OVERREACHING")

    def test_boundary_exactly_min_history_is_trusted(self):
        phase, _ = classify_phase(24.0, 34.1, -15.2, 97, hist_days=MIN_HISTORY_DAYS)
        self.assertNotEqual(phase, "UNRELIABLE")


class TestOverreachNeedsConsecutiveDays(unittest.TestCase):
    def test_day_after_the_long_run_is_building(self):
        phase, advice = classify_phase(43.5, 61.9, -27.7, 63, hist_days=600,
                                       recent_tsb=[-18.4, -19.9, -27.7])
        self.assertEqual(phase, "BUILDING")
        self.assertIn("post-long-run", advice)

    def test_three_days_under_is_overreaching(self):
        phase, _ = classify_phase(19.3, 34.6, -20.7, 180, hist_days=500,
                                  recent_tsb=[-21.8, -20.3, -20.7])
        self.assertEqual(phase, "OVERREACHING")

    def test_two_of_three_is_still_building(self):
        phase, _ = classify_phase(30.0, 55.0, -25.0, 60, hist_days=500,
                                  recent_tsb=[-12.0, -22.0, -25.0])
        self.assertEqual(phase, "BUILDING")

    def test_short_window_cannot_declare_overreaching(self):
        phase, _ = classify_phase(30.0, 55.0, -25.0, 60, hist_days=500, recent_tsb=[-25.0])
        self.assertEqual(phase, "BUILDING")

    def test_history_guard_still_wins(self):
        phase, _ = classify_phase(13.4, 39.0, -31.8, 98, hist_days=21,
                                  recent_tsb=[-30.0, -31.0, -31.8])
        self.assertEqual(phase, "UNRELIABLE")


class TestStatusAndContext(unittest.TestCase):
    def test_current_status_and_tsb_context_on_a_short_cache(self):
        import tempfile
        from pathlib import Path
        import fitness_tracker
        from enrichment import enrich
        from tests.helpers import make_activity, temp_activity_data
        today = date.today()
        acts = []
        for d, miles in ((9, 5), (6, 6), (3, 5), (1, 10)):
            # midnight starts: history_days is measured against now(), so a 07:00 start
            # read as 8 days instead of 9 whenever the suite ran before 07:00
            start = datetime.combine(today - timedelta(days=d), datetime.min.time())
            acts.append(enrich(make_activity(start_date_local=start.isoformat(timespec="seconds"),
                                             start_date=start.isoformat(timespec="seconds") + "Z",
                                             distance=miles * 1609.34, moving_time=int(miles * 600),
                                             average_heartrate=145.0)))
        with tempfile.TemporaryDirectory() as tmp:
            with temp_activity_data(Path(tmp), cache_activities=acts):
                s = fitness_tracker.current_status()
                self.assertEqual(s["phase"], "UNRELIABLE")
                self.assertFalse(s["history_sufficient"])
                self.assertEqual(s["history_days"], 9)
                ctx = tsb_context(today - timedelta(days=1))
                for k in ("tss", "tsb_before", "tsb_after", "ctl", "history_days", "history_sufficient"):
                    self.assertIn(k, ctx)
                self.assertGreater(ctx["tss"], 0)
                self.assertLess(ctx["tsb_after"], ctx["tsb_before"])   # the long run costs form
