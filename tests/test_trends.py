"""Tests for trends.py — every lens is a pure function on synthetic runs."""

import unittest
from datetime import datetime

import trends


def _run(day, dist=5.0, pace=10.0, avg_hr=145, max_hr=None, elev=10,
         rel_effort=None, hour=19):
    return {
        "date": datetime.fromisoformat(f"{day}T{hour:02d}:00:00"),
        "dist_mi": dist,
        "moving_min": dist * pace,
        "pace": pace,
        "avg_hr": avg_hr,
        "max_hr": max_hr if max_hr is not None else (avg_hr + 12 if avg_hr else None),
        "elev": elev,
        "rel_effort": rel_effort,
    }


class TestDriftHistory(unittest.TestCase):
    def test_drift_math_and_min_distance(self):
        runs = [
            _run("2026-06-01", dist=10, avg_hr=150, max_hr=165, rel_effort=120),
            _run("2026-06-03", dist=3, avg_hr=140, max_hr=150),  # below min_mi
        ]
        out = trends.drift_history(runs, min_mi=6)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["drift_bpm"], 15)
        self.assertEqual(out[0]["effort_per_mi"], 12.0)

    def test_runs_without_hr_skipped(self):
        out = trends.drift_history([_run("2026-06-01", dist=10, avg_hr=None)],
                                   min_mi=6)
        self.assertEqual(out, [])


class TestEfficiencyTrend(unittest.TestCase):
    def test_quarter_bucketing_and_band_filter(self):
        runs = [
            _run("2026-01-10", pace=10.5, avg_hr=150),
            _run("2026-02-10", pace=10.3, avg_hr=150),
            _run("2026-04-10", pace=10.0, avg_hr=150),
            _run("2026-05-10", pace=9.8, avg_hr=150),
            _run("2026-05-12", pace=8.0, avg_hr=170),  # outside band: ignored
        ]
        out = trends.efficiency_trend(runs, hr_band=(145, 155))
        self.assertEqual([e["quarter"] for e in out], ["2026 Q1", "2026 Q2"])
        self.assertAlmostEqual(out[0]["avg_pace"], 10.4, places=2)
        self.assertAlmostEqual(out[1]["avg_pace"], 9.9, places=2)

    def test_single_run_quarters_dropped(self):
        out = trends.efficiency_trend([_run("2026-01-10", avg_hr=150)],
                                      hr_band=(145, 155))
        self.assertEqual(out, [])


class TestConsistency(unittest.TestCase):
    def test_gap_detection_and_week_histogram(self):
        runs = [
            _run("2026-06-01"), _run("2026-06-02"), _run("2026-06-04"),  # wk: 3 runs
            _run("2026-06-20"),                                          # 16-day gap
        ]
        c = trends.consistency(runs, window_weeks=52,
                               today=datetime(2026, 6, 25))
        self.assertEqual(c["longest_gaps"][0]["days"], 16)
        self.assertEqual(c["weeks_3plus"], 1)
        self.assertEqual(c["weeks_1"], 1)

    def test_window_excludes_old_runs(self):
        runs = [_run("2020-01-01"), _run("2026-06-01")]
        c = trends.consistency(runs, window_weeks=4,
                               today=datetime(2026, 6, 25))
        self.assertEqual(c["weeks_with_runs"], 1)


class TestRecoveryPattern(unittest.TestCase):
    def test_post_hard_split(self):
        runs = [
            _run("2026-06-01", dist=8, avg_hr=160),   # hard (HR + distance)
            _run("2026-06-02", dist=3, avg_hr=150),   # day after hard
            _run("2026-06-10", dist=3, avg_hr=140),   # normal
            _run("2026-06-14", dist=3, avg_hr=142),   # normal
        ]
        rp = trends.recovery_pattern(runs, hard_hr_floor=155, hard_dist_mi=6)
        self.assertEqual(rp["post_hard_runs"], 1)
        self.assertEqual(rp["normal_runs"], 2)
        self.assertAlmostEqual(rp["hr_elevation_bpm"], 9.0, places=1)

    def test_insufficient_data_note(self):
        rp = trends.recovery_pattern([_run("2026-06-01", avg_hr=140)],
                                     hard_hr_floor=155, hard_dist_mi=6)
        self.assertIn("note", rp)


class TestElevationCost(unittest.TestCase):
    def test_flat_vs_hilly_deltas(self):
        runs = [
            _run("2026-06-01", pace=9.5, avg_hr=145, elev=5),
            _run("2026-06-03", pace=9.5, avg_hr=145, elev=10),
            _run("2026-06-05", pace=10.0, avg_hr=150, elev=80),
            _run("2026-06-07", pace=10.2, avg_hr=152, elev=90),
        ]
        ec = trends.elevation_cost(runs, flat_max_m=20, hilly_min_m=50)
        self.assertEqual(ec["flat_runs"], 2)
        self.assertEqual(ec["hilly_runs"], 2)
        self.assertEqual(ec["cost_sec_per_mi"], 36)  # (10.1 - 9.5) * 60
        self.assertAlmostEqual(ec["cost_bpm"], 6.0, places=1)


class TestTreadmillVsOutdoor(unittest.TestCase):
    def test_split_on_elevation(self):
        runs = [
            _run("2026-06-01", pace=9.0, avg_hr=131, elev=0),
            _run("2026-06-03", pace=9.9, avg_hr=152, elev=15),
        ]
        t = trends.treadmill_vs_outdoor(runs)
        self.assertEqual(t["indoor_runs"], 1)
        self.assertEqual(t["outdoor_runs"], 1)
        self.assertLess(t["indoor_pace"], t["outdoor_pace"])


class TestEffortEfficiency(unittest.TestCase):
    def test_best_and_worst_ordering(self):
        runs = [
            _run("2026-06-01", dist=5, pace=9.0, rel_effort=40),   # 8/mi
            _run("2026-06-03", dist=5, pace=9.0, rel_effort=100),  # 20/mi
            _run("2026-06-05", dist=5, pace=11.0, rel_effort=10),  # too slow: out
        ]
        ee = trends.effort_efficiency(runs, max_pace=9.5, top_n=1)
        self.assertEqual(ee["most_efficient"][0]["eff_per_mi"], 8.0)
        self.assertEqual(ee["least_efficient"][0]["eff_per_mi"], 20.0)


if __name__ == "__main__":
    unittest.main()
