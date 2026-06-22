"""Tests for the parametric marathon plan generator."""

import unittest
from datetime import date

import marathon_plan
from plan_generator import generate_plan_dict, to_training_plan


class TestGeneratedPlan(unittest.TestCase):
    def setUp(self):
        self.plan = generate_plan_dict(25, weeks=16)

    def test_passes_loader_validation(self):
        # The same _validate the loader runs must accept a generated plan.
        marathon_plan._validate(self.plan)

    def test_sixteen_weeks_contiguous(self):
        weeks = self.plan["weeks"]
        self.assertEqual(len(weeks), 16)
        self.assertEqual([w["week_num"] for w in weeks], list(range(1, 17)))

    def test_week_starts_seven_days_apart(self):
        dates = [date.fromisoformat(w["start_date"]) for w in self.plan["weeks"]]
        for prev, cur in zip(dates, dates[1:]):
            self.assertEqual((cur - prev).days, 7)

    def test_ends_on_race_week(self):
        race_date = date.fromisoformat(self.plan["race"]["date"])
        last_start = date.fromisoformat(self.plan["weeks"][-1]["start_date"])
        self.assertLessEqual(last_start, race_date)
        self.assertEqual((race_date - last_start).days // 7, 0)  # race in last week

    def test_phases_in_range(self):
        n = len(self.plan["weeks"])
        for ph in self.plan["phases"]:
            self.assertGreaterEqual(ph["start_week"], 1)
            self.assertLessEqual(ph["end_week"], n)

    def test_training_plan_has_lift_days(self):
        tp = to_training_plan(self.plan)
        self.assertEqual(len(tp.weeks), 16)
        first = tp.weeks[0]
        self.assertTrue(any(w.workout_type == "lift" for w in first.workouts))
        self.assertTrue(any(w.workout_type == "long" for w in first.workouts))


if __name__ == "__main__":
    unittest.main()
