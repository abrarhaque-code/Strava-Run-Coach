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
