"""Tests for config loading, validation, and active-race resolution.

Runs with no config.json (falls back to config.example.json) and no network.
"""

import copy
import unittest
from datetime import date, timedelta

import config


class TestConfigLoad(unittest.TestCase):
    def test_load_returns_required_keys(self):
        cfg = config.reload()
        for key in ("athlete", "pace_zones", "races", "theme"):
            self.assertIn(key, cfg)
        self.assertIsInstance(cfg["races"], list)
        self.assertTrue(cfg["races"])

    def test_getters(self):
        config.reload()
        self.assertIsInstance(config.max_hr(), int)
        self.assertIsInstance(config.threshold_hr(), int)
        self.assertIsInstance(config.easy_hr_cap(), int)
        self.assertIsInstance(config.threshold_pace(), float)
        self.assertIsInstance(config.theme(), dict)
        self.assertTrue(config.athlete_name())


class TestValidate(unittest.TestCase):
    def setUp(self):
        # A minimally valid config to mutate per test.
        self.base = {
            "athlete": {"name": "T", "max_hr": 189, "threshold_hr": 165,
                        "easy_hr_cap": 148, "threshold_pace_min_per_mi": 9.0},
            "pace_zones": {},
            "theme": {},
            "races": [
                {"id": "r1", "name": "Race 1", "date": "2026-05-01",
                 "distance_mi": 13.1, "goal_pace_min_per_mi": 9.0},
            ],
            "active_race": "auto",
        }

    def test_valid_passes(self):
        config._validate(self.base)  # should not raise

    def test_missing_races_raises(self):
        bad = copy.deepcopy(self.base)
        del bad["races"]
        with self.assertRaises(ValueError):
            config._validate(bad)

    def test_empty_races_raises(self):
        bad = copy.deepcopy(self.base)
        bad["races"] = []
        with self.assertRaises(ValueError):
            config._validate(bad)

    def test_bad_date_raises(self):
        bad = copy.deepcopy(self.base)
        bad["races"][0]["date"] = "not-a-date"
        with self.assertRaises(ValueError):
            config._validate(bad)

    def test_unknown_active_race_raises(self):
        bad = copy.deepcopy(self.base)
        bad["active_race"] = "does_not_exist"
        with self.assertRaises(ValueError):
            config._validate(bad)

    def test_duplicate_race_id_raises(self):
        bad = copy.deepcopy(self.base)
        bad["races"].append(dict(bad["races"][0]))
        with self.assertRaises(ValueError):
            config._validate(bad)


class TestActiveRace(unittest.TestCase):
    def setUp(self):
        config.reload()

    def test_active_race_has_required_keys(self):
        race = config.active_race()
        for key in config._REQUIRED_RACE:
            self.assertIn(key, race)

    def test_active_race_is_future_or_last(self):
        today = date.today()
        race = config.active_race(today)
        ordered = sorted(config.races(), key=lambda r: r["date"])
        last_id = ordered[-1]["id"]
        race_d = date.fromisoformat(race["date"])
        # Either the race is today/future, or every race is past and we got the
        # last one by date.
        self.assertTrue(race_d >= today or race["id"] == last_id)

    def test_active_race_picks_earliest_future(self):
        # With a far-past "today", the earliest race by date should win.
        ordered = sorted(config.races(), key=lambda r: r["date"])
        long_ago = date.fromisoformat(ordered[0]["date"]) - timedelta(days=365)
        race = config.active_race(long_ago)
        self.assertEqual(race["id"], ordered[0]["id"])

    def test_active_race_all_past_returns_last(self):
        ordered = sorted(config.races(), key=lambda r: r["date"])
        far_future = date.fromisoformat(ordered[-1]["date"]) + timedelta(days=365)
        race = config.active_race(far_future)
        self.assertEqual(race["id"], ordered[-1]["id"])

    def test_race_by_id_roundtrip(self):
        rid = config.races()[0]["id"]
        self.assertEqual(config.race_by_id(rid)["id"], rid)
        self.assertIsNone(config.race_by_id("nope-not-real"))


class TestHelpers(unittest.TestCase):
    def test_has_structured_plan_means_the_file_exists(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "plan.json"
            json_race = {"id": "r", "plan": str(f)}
            self.assertFalse(config.has_structured_plan(json_race))
            f.write_text("{}", encoding="utf-8")
            self.assertTrue(config.has_structured_plan(json_race))
        # Legacy sentinel and no plan key both resolve to the (absent)
        # generated file for the race id.
        self.assertFalse(config.has_structured_plan({"id": "zz", "plan": "generated_half"}))
        self.assertFalse(config.has_structured_plan({}))

    def test_zone_edges_monotonic_and_from_config(self):
        config.reload()
        edges = config.zone_edges()
        names = [e[0] for e in edges]
        self.assertEqual(names, ["recovery", "easy", "steady", "mp", "threshold", "vo2"])
        for (_, lo, hi), (_, nlo, _nhi) in zip(edges, edges[1:]):
            self.assertLessEqual(lo, hi)
            self.assertEqual(hi, nlo)
        self.assertEqual(edges[1][2], config.easy_hr_cap())
        self.assertEqual(list(edges[3][1:]), config.race_pace_hr_range())

    def test_optional_getters_have_defaults(self):
        config.reload()
        self.assertAlmostEqual(config.vo2_pct_max(), 0.90)
        self.assertEqual(config.hr_source(), "wrist")
        self.assertEqual(config.plan_cfg()["days_per_week"], 5)
        self.assertEqual(config.plan_cfg()["quality"], "full")
        self.assertGreater(config.long_run_min_mi(), 0)

    def test_goal_time_to_sec(self):
        self.assertEqual(config.goal_time_to_sec("3:45:00"), 13500)
        self.assertEqual(config.goal_time_to_sec("1:59:59"), 7199)
        # MM:SS form
        self.assertEqual(config.goal_time_to_sec("20:00"), 1200)


if __name__ == "__main__":
    unittest.main()
