"""plan_layout: a week's targets -> seven sessions the athlete can act on."""

import unittest
from datetime import date

import plan_layout as pl
import units


def _week(target=30.0, long=10.0, phase="build", quality=None, start="2026-06-01"):
    w = {"week_num": 1, "start_date": start, "phase": phase, "target_miles": target,
         "long_run_target": long, "key_workout": "", "notes": ""}
    if quality:
        w["quality"] = quality
    return w


def _layout(**kw):
    base = {"days_per_week": 5, "long_run_day": "sat", "quality_day": "wed", "rest_days": [],
            "lift_days": [], "quality": "full", "max_long_run_mi": None,
            "long_run_time_cap_min": None}
    base.update(kw)
    return base


def _runs(days):
    return [d for d in days if d["role"] in ("easy", "key", "long", "race", "shakeout")]


class TestDistributeWeek(unittest.TestCase):
    def test_sums_to_target_for_every_days_per_week(self):
        for n in (3, 4, 5, 6, 7):
            days = pl.distribute_week(_week(40, 14), _layout(days_per_week=n))
            self.assertEqual(len(days), 7)
            self.assertAlmostEqual(sum(d["distance_mi"] for d in days), 40.0, delta=0.5, msg=f"days={n}")
            self.assertLessEqual(len(_runs(days)), n)
            self.assertEqual([d["dow"] for d in days], list(pl.DOWS))
            self.assertEqual(days[0]["date"], "2026-06-01")
            self.assertEqual(days[6]["date"], "2026-06-07")

    def test_long_day_fixed_and_key_never_the_day_before(self):
        days = pl.distribute_week(_week(), _layout(long_run_day="sun", quality_day="sat"))
        by = {d["dow"]: d for d in days}
        self.assertEqual(by["sun"]["role"], "long")
        self.assertEqual(by["sun"]["distance_mi"], 10.0)
        self.assertNotEqual(by["sat"]["role"], "key")
        self.assertEqual(by["fri"]["role"], "key")

    def test_quality_none_has_no_key_and_no_strides(self):
        days = pl.distribute_week(_week(), _layout(quality="none"))
        self.assertFalse(any(d["role"] == "key" for d in days))
        self.assertFalse(any(d["strides"] for d in days))

    def test_strides_level_puts_strides_on_an_easy_day(self):
        days = pl.distribute_week(_week(), _layout(quality="strides"))
        self.assertFalse(any(d["role"] == "key" for d in days))
        self.assertEqual(sum(1 for d in days if d["strides"]), 1)

    def test_base_phase_has_strides_not_a_key_session(self):
        days = pl.distribute_week(_week(phase="base"), _layout())
        self.assertFalse(any(d["role"] == "key" for d in days))
        self.assertTrue(any(d["strides"] for d in days))

    def test_thin_pool_drops_a_day_rather_than_scraps(self):
        # 15 mi, 6 mi long, 6 days: key 3.0, pool 6 over 4 easy days = 1.5 -> drop days
        days = pl.distribute_week(_week(15, 6), _layout(days_per_week=6))
        easy = [d for d in days if d["role"] == "easy"]
        self.assertTrue(all(d["distance_mi"] >= pl.MIN_EASY_MI for d in easy))
        self.assertLess(len(_runs(days)), 6)
        self.assertAlmostEqual(sum(d["distance_mi"] for d in days), 15.0, delta=0.5)

    def test_rest_days_and_lifts_are_honored_and_lift_moves_off_the_eve(self):
        days = pl.distribute_week(_week(), _layout(rest_days=["mon"], lift_days=["fri", "mon"]))
        by = {d["dow"]: d for d in days}
        self.assertEqual(by["mon"]["role"], "rest")
        self.assertTrue(by["mon"]["strength"])
        self.assertFalse(by["fri"]["strength"])     # the day before the Saturday long run
        self.assertTrue(by["sun"]["strength"])      # moved to the day after

    def test_key_session_carries_pace_key_and_hr_cap(self):
        days = pl.distribute_week(_week(quality={"kind": "tempo", "miles": 4}), _layout())
        key = next(d for d in days if d["role"] == "key")
        self.assertEqual(key["pace"], "tempo")
        self.assertIsNotNone(key["hr_cap"])
        self.assertIn("tempo", key["description"])

    def test_race_week(self):
        w = _week(20, 0, phase="taper", start="2026-10-26")
        days = pl.distribute_week(w, _layout(), race_distance_mi=26.2, race_date=date(2026, 11, 1))
        by = {d["dow"]: d for d in days}
        self.assertEqual(by["sun"]["role"], "race")
        self.assertEqual(by["sun"]["distance_mi"], 26.2)
        self.assertEqual(by["sat"]["role"], "rest")
        self.assertFalse(any(d["role"] in ("long", "key") for d in days))
        self.assertTrue(all(d["distance_mi"] <= 6 for d in days if d["role"] == "shakeout"))


class TestHelpers(unittest.TestCase):
    def test_sessions_for_week_prefers_stored_days(self):
        w = _week()
        w["days"] = [{"dow": d, "role": "rest", "distance_mi": 0} for d in pl.DOWS]
        self.assertIs(pl.sessions_for_week(w, plan={"race": {"date": "2026-11-01", "distance_mi": 26.2}}),
                      w["days"])

    def test_sessions_for_week_computes_when_absent(self):
        w = _week()
        days = pl.sessions_for_week(w, plan={"race": {"date": "2026-11-01", "distance_mi": 26.2}},
                                    layout=_layout())
        self.assertEqual(len(days), 7)
        self.assertEqual(pl.role_for_date(w, date(2026, 6, 6)), "long")   # Saturday

    def test_format_session_shows_pace_range_and_cap(self):
        paces = {"easy": {"min": 10.0, "max": 10.5}, "marathon_pace": 8.583}
        day = {"dow": "tue", "role": "easy", "distance_mi": 5.0, "pace": "easy", "hr_cap": 148,
               "strides": 0, "strength": True, "description": "Easy 5.0 mi"}
        s = pl.format_session(day, paces)
        self.assertIn("Easy 5.0 mi", s)
        self.assertIn("10:00-10:30", s)
        self.assertIn("HR < 148", s)
        self.assertTrue(s.endswith("[lift]"))
        race = {"dow": "sun", "role": "race", "distance_mi": 26.2, "pace": "marathon_pace",
                "hr_cap": None, "strides": 0, "strength": False, "description": "RACE DAY"}
        self.assertIn(units.fmt_pace(8.583, label=False), pl.format_session(race, paces))

    def test_apply_notes_turns_key_days_easy(self):
        days = pl.distribute_week(_week(quality={"kind": "tempo", "miles": 4}), _layout())
        notes = [{"date": "2026-06-01", "text": "calf tight, no speedwork", "until": "2026-06-30"}]
        out = pl.apply_notes(days, notes)
        key = [d for d in out if d["role"] == "key"]
        self.assertEqual(key, [])
        turned = next(d for d, o in zip(days, out) if d["role"] == "key" and o["role"] == "easy")
        self.assertIn("per your note", next(o for o in out if "per your note" in o["description"])["description"])
        self.assertEqual(turned["distance_mi"], next(o for o in out if "per your note" in o["description"])["distance_mi"])
        # an unrelated note changes nothing
        self.assertEqual(pl.apply_notes(days, [{"text": "moved long run to Sunday"}]), list(days))

    def test_to_planned_workouts_emits_lift_separately(self):
        plan = {"paces": {"easy": {"min": 10.0, "max": 10.5}}}
        day = {"dow": "tue", "date": "2026-06-02", "role": "easy", "distance_mi": 5.0, "pace": "easy",
               "hr_cap": 148, "strides": 0, "strength": True, "description": "Easy 5.0 mi"}
        ws = pl.to_planned_workouts(day, plan)
        self.assertEqual([w.workout_type for w in ws], ["easy", "lift"])
        self.assertAlmostEqual(ws[0].pace_min_per_mi, 10.5)


if __name__ == "__main__":
    unittest.main()
