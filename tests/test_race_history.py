"""Tests for race_history calibration: config validation + VDOT anchoring.

A real race result is the strongest fitness anchor available — it must beat
weak training-run inferences and kill the cold-start VDOT default, but stale
results (>180 days) age out.
"""

import json
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import config
import race_predictor

_HERE = Path(__file__).resolve().parent.parent


def _example_cfg() -> dict:
    return json.loads((_HERE / "config.example.json").read_text(encoding="utf-8"))


class TestConfigValidation(unittest.TestCase):
    def test_example_config_with_history_validates(self):
        config._validate(_example_cfg())  # must not raise

    def test_missing_history_key_is_fine(self):
        cfg = _example_cfg()
        cfg.pop("race_history", None)
        config._validate(cfg)  # optional key

    def test_bad_date_rejected(self):
        cfg = _example_cfg()
        cfg["race_history"] = [{"id": "x", "date": "not-a-date",
                                "distance_mi": 6.2, "result_time": "50:00"}]
        with self.assertRaises(ValueError):
            config._validate(cfg)

    def test_bad_result_time_rejected(self):
        cfg = _example_cfg()
        cfg["race_history"] = [{"id": "x", "date": "2025-10-12",
                                "distance_mi": 6.2, "result_time": "fast"}]
        with self.assertRaises(ValueError):
            config._validate(cfg)

    def test_zero_distance_rejected(self):
        cfg = _example_cfg()
        cfg["race_history"] = [{"id": "x", "date": "2025-10-12",
                                "distance_mi": 0, "result_time": "50:00"}]
        with self.assertRaises(ValueError):
            config._validate(cfg)


class TestVdotAnchoring(unittest.TestCase):
    # A far-future "today" isolates these tests from the repo's sample data:
    # nothing in the cache/CSV can fall inside the 30-day scan window.
    TODAY = datetime(2027, 6, 1)

    def test_recent_race_result_beats_cold_start_default(self):
        history = [{"id": "h1", "name": "Spring 10K", "date": "2027-05-01",
                    "distance_mi": 6.2, "result_time": "48:00"}]
        with mock.patch.object(config, "race_history", return_value=history):
            vdot, best = race_predictor.current_fitness_vdot([], today=self.TODAY)
        self.assertGreater(vdot, 35.0)  # not the cold-start default
        self.assertEqual(best["note"], "race result")
        self.assertEqual(best["name"], "Spring 10K")

    def test_stale_race_result_ages_out(self):
        history = [{"id": "h1", "name": "Old 10K", "date": "2026-05-01",
                    "distance_mi": 6.2, "result_time": "48:00"}]  # >180d before TODAY
        with mock.patch.object(config, "race_history", return_value=history):
            vdot, best = race_predictor.current_fitness_vdot([], today=self.TODAY)
        self.assertEqual(vdot, 35.0)
        self.assertNotEqual(best.get("note"), "race result")

    def test_future_dated_result_ignored(self):
        history = [{"id": "h1", "name": "Not run yet", "date": "2027-07-01",
                    "distance_mi": 6.2, "result_time": "48:00"}]
        with mock.patch.object(config, "race_history", return_value=history):
            vdot, _ = race_predictor.current_fitness_vdot([], today=self.TODAY)
        self.assertEqual(vdot, 35.0)

    def test_empty_history_keeps_default_behavior(self):
        with mock.patch.object(config, "race_history", return_value=[]):
            vdot, best = race_predictor.current_fitness_vdot([], today=self.TODAY)
        self.assertEqual(vdot, 35.0)
        self.assertIn("no recent qualifying efforts", best["note"])

    def test_scenario_endurance_anchor_prefers_long_race(self):
        import scenario
        from datetime import date
        history = [{"id": "h1", "name": "Recent Half", "date": "2027-05-10",
                    "distance_mi": 13.1, "result_time": "1:55:00"}]
        with mock.patch.object(config, "race_history", return_value=history):
            vdot, best = scenario.endurance_anchor_vdot(
                [], today=date(2027, 6, 1))
        self.assertGreater(vdot, 38.0)  # not the neutral default
        self.assertEqual(best["name"], "Recent Half")


if __name__ == "__main__":
    unittest.main()
