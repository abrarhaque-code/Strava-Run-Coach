"""Tests for marathon_plan loading and validation."""

import unittest

import marathon_plan


def _plan_with_weeks(weeks):
    """Minimal but structurally valid plan wrapper around a weeks list."""
    return {
        "race": {"name": "Test Marathon", "date": "2026-11-01",
                 "goal_time": "3:45:00", "goal_pace_min_per_mi": 8.583},
        "paces": {},
        "phases": [{"id": "base", "name": "Base", "start_week": 1,
                    "end_week": len(weeks)}],
        "weeks": weeks,
        "decision_points": [],
    }


def _week(num, start_date):
    return {"week_num": num, "start_date": start_date, "phase": "base",
            "target_miles": 20, "long_run_target": 8}


class TestLoadPlan(unittest.TestCase):
    def test_shipped_plan_loads(self):
        plan = marathon_plan.load_plan()
        self.assertEqual(len(plan["weeks"]), 24)
        for key in ("race", "paces", "phases", "weeks", "decision_points"):
            self.assertIn(key, plan)


class TestValidate(unittest.TestCase):
    def test_valid_plan_passes(self):
        weeks = [
            _week(1, "2026-06-01"),
            _week(2, "2026-06-08"),
            _week(3, "2026-06-15"),
        ]
        marathon_plan._validate(_plan_with_weeks(weeks))  # no raise

    def test_noncontiguous_week_numbers_raise(self):
        weeks = [
            _week(1, "2026-06-01"),
            _week(3, "2026-06-08"),  # gap: 1, 3
        ]
        with self.assertRaises(ValueError):
            marathon_plan._validate(_plan_with_weeks(weeks))

    def test_weeks_not_seven_days_apart_raise(self):
        weeks = [
            _week(1, "2026-06-01"),
            _week(2, "2026-06-10"),  # 9 days, not 7
        ]
        with self.assertRaises(ValueError):
            marathon_plan._validate(_plan_with_weeks(weeks))

    def test_missing_top_key_raises(self):
        plan = _plan_with_weeks([_week(1, "2026-06-01")])
        del plan["decision_points"]
        with self.assertRaises(ValueError):
            marathon_plan._validate(plan)


if __name__ == "__main__":
    unittest.main()
