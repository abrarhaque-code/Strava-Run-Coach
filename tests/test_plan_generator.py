"""The personalised plan generator: schema, presets, preferences, entry from data."""

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import marathon_plan
import plan_generator as pg
from tests.helpers import temp_config

TODAY = date(2026, 6, 1)   # 22 weeks before the example config's City Marathon


def _runs(days):
    return [d for d in days if d["role"] in ("easy", "key", "long", "race", "shakeout")]


class TestGeneratedPlan(unittest.TestCase):
    def setUp(self):
        self.plan = pg.generate_plan_dict(25, weeks=16, today=TODAY)

    def test_passes_loader_validation(self):
        marathon_plan._validate(self.plan)

    def test_sixteen_weeks_contiguous_seven_days_apart(self):
        weeks = self.plan["weeks"]
        self.assertEqual([w["week_num"] for w in weeks], list(range(1, 17)))
        dates = [date.fromisoformat(w["start_date"]) for w in weeks]
        for prev, cur in zip(dates, dates[1:]):
            self.assertEqual((cur - prev).days, 7)

    def test_ends_on_race_week_with_a_race_day(self):
        race_date = date.fromisoformat(self.plan["race"]["date"])
        last = self.plan["weeks"][-1]
        last_start = date.fromisoformat(last["start_date"])
        self.assertLessEqual(last_start, race_date)
        self.assertLess((race_date - last_start).days, 7)
        roles = [d["role"] for d in last["days"]]
        self.assertIn("race", roles)
        self.assertNotIn("long", roles)
        self.assertEqual(last["long_run_target"], 0.0)

    def test_every_week_has_seven_days_and_structured_quality(self):
        for w in self.plan["weeks"]:
            self.assertEqual(len(w["days"]), 7)
            self.assertIn(w["quality"]["kind"], marathon_plan.QUALITY_KINDS)
            self.assertTrue(w["key_workout"])

    def test_phases_in_range_and_meta_inputs(self):
        n = len(self.plan["weeks"])
        for ph in self.plan["phases"]:
            self.assertGreaterEqual(ph["start_week"], 1)
            self.assertLessEqual(ph["end_week"], n)
        inp = self.plan["_meta"]["inputs"]
        self.assertEqual(inp["entry_mi"], 25)
        self.assertEqual(inp["days_per_week"], 5)
        self.assertEqual(self.plan["_meta"]["distance_preset"], "marathon")
        self.assertIn("compliance_thresholds", self.plan["_meta"])

    def test_decision_points_carry_min_runs(self):
        crit = [c for dp in self.plan["decision_points"] for c in dp["criteria"]
                if c["metric"] == "weeks_with_min_runs_of_4"]
        self.assertEqual(crit[0]["min_runs"], 4)
        peak = next(dp for dp in self.plan["decision_points"] if dp["id"] == "end_of_peak")
        self.assertTrue(any(c["metric"] == "mp_segment_completed_mi" for c in peak["criteria"]))

    def test_training_plan_has_lift_and_long(self):
        plan = pg.generate_plan_dict(25, weeks=16, today=TODAY, prefs={"lift_days": ["mon", "thu"]})
        tp = pg.to_training_plan(plan)
        self.assertEqual(len(tp.weeks), 16)
        first = tp.weeks[0]
        self.assertTrue(any(w.workout_type == "lift" for w in first.workouts))
        self.assertTrue(any(w.workout_type == "long" for w in first.workouts))
        self.assertEqual(first.lift_sessions, 2)


class TestPreferences(unittest.TestCase):
    def test_days_per_week_and_long_day(self):
        plan = pg.generate_plan_dict(25, weeks=12, today=TODAY,
                                     prefs={"days_per_week": 4, "long_run_day": "sun"})
        for w in plan["weeks"][:-1]:
            self.assertLessEqual(len(_runs(w["days"])), 4)
            self.assertEqual(next(d for d in w["days"] if d["role"] == "long")["dow"], "sun")
        self.assertEqual(plan["_meta"]["inputs"]["long_run_day"], "sun")

    def test_quality_none_means_no_key_sessions(self):
        plan = pg.generate_plan_dict(25, weeks=12, today=TODAY, prefs={"quality": "none"})
        for w in plan["weeks"]:
            self.assertEqual(w["quality"]["kind"], "none")
            self.assertFalse(any(d["role"] == "key" for d in w["days"]))
        crit = [c["metric"] for dp in plan["decision_points"] for c in dp["criteria"]]
        self.assertNotIn("mp_segment_completed_mi", crit)

    def test_max_long_run_caps_the_long_run(self):
        plan = pg.generate_plan_dict(40, weeks=16, today=TODAY, prefs={"max_long_run_mi": 16})
        self.assertLessEqual(max(w["long_run_target"] for w in plan["weeks"]), 16.0)

    def test_default_weeks_shortens_to_the_runway(self):
        _, preset = pg.preset_for(26.2)
        self.assertEqual(pg.default_weeks(date(2026, 11, 1), preset, today=date(2026, 6, 1)), 16)
        self.assertEqual(pg.default_weeks(date(2026, 11, 1), preset, today=date(2026, 10, 1)), 5)
        self.assertEqual(pg.default_weeks(date(2026, 11, 1), preset, today=date(2026, 10, 28)), pg.MIN_WEEKS)

    def test_past_race_is_an_error(self):
        # On 2027-06-01 every example race is behind us; the generator must say so.
        with self.assertRaises(ValueError):
            pg.generate_plan_dict(25, weeks=8, today=date(2027, 6, 1))


class TestPresetsAndHalf(unittest.TestCase):
    def test_preset_for(self):
        self.assertEqual(pg.preset_for(26.2)[0], "marathon")
        self.assertEqual(pg.preset_for(13.1)[0], "half")
        self.assertEqual(pg.preset_for(6.2)[0], "10k")
        self.assertEqual(pg.preset_for(3.1)[0], "5k")

    def test_half_plan_uses_the_half_preset(self):
        with tempfile.TemporaryDirectory() as tmp:
            with temp_config(Path(tmp), {"active_race": "spring_half"}):
                plan = pg.generate_plan_dict(25, today=date(2027, 1, 4))
                self.assertEqual(plan["_meta"]["distance_preset"], "half")
                self.assertEqual(len(plan["weeks"]), 12)
                self.assertLessEqual(max(w["long_run_target"] for w in plan["weeks"]), 14.0)
                self.assertNotIn("mp", [w["quality"]["kind"] for w in plan["weeks"]])
                marathon_plan._validate(plan)


class TestEntryAndWrite(unittest.TestCase):
    def test_entry_from_data_rounds_up_to_five_with_a_floor(self):
        with mock.patch.object(pg.scenario, "derive_inputs", return_value={"run_mpw": 29.3, "activities_loaded": 20}):
            self.assertEqual(pg.entry_from_data()["entry_mi"], 30.0)
        with mock.patch.object(pg.scenario, "derive_inputs", return_value={"run_mpw": 4.0, "activities_loaded": 2}):
            self.assertEqual(pg.entry_from_data()["entry_mi"], 10.0)
        with mock.patch.object(pg.scenario, "derive_inputs", return_value={"run_mpw": 0, "activities_loaded": 0}):
            self.assertEqual(pg.entry_from_data()["entry_mi"], 10.0)

    def test_write_plan_validates_and_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pg.write_plan(25, weeks=8, path=Path(tmp) / "x.generated.json", today=TODAY)
            plan = json.loads(path.read_text(encoding="utf-8"))
            marathon_plan._validate(plan)
            self.assertEqual(len(plan["weeks"]), 8)

    def test_cli_prefs(self):
        a = pg._parse_args(["--entry", "40km", "--days", "6", "--no-quality", "--lift-days", "mon,thu",
                            "--max-long", "30km"])
        prefs = pg._prefs_from_args(a)
        self.assertEqual(prefs["days_per_week"], 6)
        self.assertEqual(prefs["quality"], "none")
        self.assertEqual(prefs["lift_days"], ["mon", "thu"])
        self.assertAlmostEqual(prefs["max_long_run_mi"], 30 / 1.609344, places=4)


if __name__ == "__main__":
    unittest.main()
