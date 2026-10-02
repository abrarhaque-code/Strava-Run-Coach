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
    def test_example_plan_is_valid(self):
        import json
        from pathlib import Path
        example = Path(__file__).resolve().parent.parent / "docs" / "examples" / "plan.example.json"
        plan = json.loads(example.read_text(encoding="utf-8"))
        marathon_plan._validate(plan)
        self.assertGreaterEqual(len(plan["weeks"]), 12)
        for key in ("race", "paces", "phases", "weeks", "decision_points"):
            self.assertIn(key, plan)

    def test_has_plan_false_and_load_raises_with_hint_when_missing(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            old = marathon_plan.PLAN_PATH
            marathon_plan.PLAN_PATH = Path(tmp) / "nope.json"
            marathon_plan.load_plan.cache_clear()
            try:
                self.assertFalse(marathon_plan.has_plan())
                with self.assertRaises(FileNotFoundError) as cm:
                    marathon_plan.load_plan()
                self.assertIn("coach.py plan", str(cm.exception))
            finally:
                marathon_plan.PLAN_PATH = old
                marathon_plan.load_plan.cache_clear()


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


def _days(start="2026-06-01", role_overrides=None, pace="easy"):
    from datetime import date, timedelta
    d0 = date.fromisoformat(start)
    roles = ["rest", "easy", "key", "easy", "rest", "long", "easy"]
    for i, r in (role_overrides or {}).items():
        roles[i] = r
    return [{"dow": dow, "date": (d0 + timedelta(days=i)).isoformat(), "role": roles[i],
             "distance_mi": 0 if roles[i] == "rest" else 5.0,
             "pace": None if roles[i] == "rest" else pace}
            for i, dow in enumerate(marathon_plan.DOWS)]


class TestWeekExtras(unittest.TestCase):
    def _plan(self, **week_extra):
        w = _week(1, "2026-06-01")
        w.update(week_extra)
        plan = _plan_with_weeks([w])
        plan["paces"] = {"easy": {"min": 10.0, "max": 10.5}, "marathon_pace": 8.583}
        return plan

    def test_valid_days_quality_and_cap_pass(self):
        plan = self._plan(days=_days(), quality={"kind": "tempo", "miles": 4},
                          long_run_time_cap_min=180)
        marathon_plan._validate(plan)

    def test_days_must_be_seven(self):
        with self.assertRaises(ValueError):
            marathon_plan._validate(self._plan(days=_days()[:6]))

    def test_bad_role_raises(self):
        with self.assertRaises(ValueError):
            marathon_plan._validate(self._plan(days=_days(role_overrides={2: "sprint"})))

    def test_pace_must_be_a_paces_key(self):
        with self.assertRaises(ValueError):
            marathon_plan._validate(self._plan(days=_days(pace="warp")))

    def test_day_date_must_sit_on_the_week(self):
        days = _days()
        days[3]["date"] = "2026-06-10"
        with self.assertRaises(ValueError):
            marathon_plan._validate(self._plan(days=days))

    def test_bad_quality_kind_and_cap_raise(self):
        with self.assertRaises(ValueError):
            marathon_plan._validate(self._plan(quality={"kind": "sprint"}))
        with self.assertRaises(ValueError):
            marathon_plan._validate(self._plan(long_run_time_cap_min=0))


if __name__ == "__main__":
    unittest.main()
