"""Tests for the two-stream load model: running mileage vs aerobic load.

Cross-training and strength must feed the aerobic-load stream (CTL/ATL/TSB)
without entering running mileage.
"""

import json
import tempfile
import unittest
from pathlib import Path

import config
import fitness_tracker as ft


class TestLoadHelpers(unittest.TestCase):
    def test_crosstrain_tss_positive_and_capped(self):
        self.assertGreater(ft.crosstrain_tss({"moving_time": 1800}), 0.0)
        self.assertLessEqual(ft.crosstrain_tss({"moving_time": 100000}), 150.0)
        self.assertEqual(ft.crosstrain_tss({"moving_time": 0}), 0.0)

    def test_strength_tss_from_config(self):
        expected = float(config.strength_cfg().get("tss_per_session", 30))
        self.assertEqual(ft.strength_tss(), expected)


class TestStreams(unittest.TestCase):
    def setUp(self):
        self._cache = ft.CACHE_DIR
        self._csv = ft.CSV_PATH
        self._tmp = tempfile.TemporaryDirectory()
        ft.CACHE_DIR = Path(self._tmp.name)
        ft.CSV_PATH = Path(self._tmp.name) / "nonexistent.csv"

        def write(aid, atype, **summary):
            doc = {"id": aid, "type": atype,
                   "start_date_local": "2026-06-10T07:00:00"}
            doc.update(summary)
            (ft.CACHE_DIR / f"{aid}.json").write_text(json.dumps(doc))

        write("r1", "Run", distance=8046.7, moving_time=2700, average_heartrate=150)
        write("x1", "CrossTrain", distance=0, moving_time=1800)
        write("s1", "WeightTraining", distance=0, moving_time=2400)

    def tearDown(self):
        ft.CACHE_DIR = self._cache
        ft.CSV_PATH = self._csv
        self._tmp.cleanup()

    def test_sessions_cover_all_three_streams(self):
        kinds = sorted(s["kind"] for s in ft.load_sessions())
        self.assertEqual(kinds, ["crosstrain", "run", "strength"])

    def test_run_mileage_excludes_crosstrain(self):
        # load_runs is the running-mileage stream: only the Run shows up.
        runs = ft.load_runs()
        self.assertEqual(len(runs), 1)
        self.assertAlmostEqual(runs[0]["dist_mi"], 5.0, delta=0.1)

    def test_aerobic_load_includes_crosstrain_and_strength(self):
        # Total daily TSS from sessions should exceed the run-only TSS.
        sessions = ft.load_sessions()
        total = sum(s["tss"] for s in sessions)
        run_only = sum(s["tss"] for s in sessions if s["kind"] == "run")
        self.assertGreater(total, run_only)


if __name__ == "__main__":
    unittest.main()
