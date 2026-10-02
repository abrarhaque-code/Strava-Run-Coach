"""daily_brief: today's session from the plan layout, standing notes applied,
fatigue thresholds from the week's targets and the config caps."""

import contextlib
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import daily_brief as db
import units
from enrichment import enrich
from tests.helpers import make_activity, make_plan, temp_activity_data, temp_config, temp_plan

MI = 1609.34
TUE_WK2 = date(2026, 5, 26)


def _run_on(day: str, miles: float = 5.0, hr: float = 140.0):
    start = f"{day}T19:00:00"
    return enrich(make_activity(start_date_local=start, start_date=start + "Z",
                                distance=miles * MI, moving_time=int(miles * 600),
                                average_heartrate=hr))


def _load(n_runs, miles, avg_hr, longest):
    return {"recent": [{}] * n_runs, "days": 3, "total_mi": miles, "total_min": miles * 10,
            "avg_hr": avg_hr, "longest_mi": longest}


class TestFatigue(unittest.TestCase):
    def test_states(self):
        self.assertEqual(db._fatigue_status(_load(0, 0, 0, 0))[0], "FRESH")
        self.assertEqual(db._fatigue_status(_load(3, 14, 140, 6))[0], "FATIGUED")      # 14 >= 12 default
        self.assertEqual(db._fatigue_status(_load(2, 10, 160, 5))[0], "ACCUMULATING")  # > hard floor 155
        self.assertEqual(db._fatigue_status(_load(1, 10, 140, 10))[0], "RECOVERING")   # >= long_run_min 7
        self.assertEqual(db._fatigue_status(_load(2, 6, 138, 3))[0], "READY")          # < cap - 5
        self.assertEqual(db._fatigue_status(_load(2, 6, 146, 3))[0], "MODERATE")

    def test_thresholds_scale_with_the_week(self):
        big_week = {"target_miles": 60, "long_run_target": 20}
        self.assertEqual(db._fatigue_status(_load(3, 14, 140, 6), big_week)[0], "READY")  # 14 < 24, easy HR
        self.assertEqual(db._fatigue_status(_load(3, 25, 140, 6), big_week)[0], "FATIGUED")
        self.assertEqual(db._fatigue_status(_load(1, 10, 140, 10), big_week)[0], "READY")  # 10 < 12: not long
        status, note = db._fatigue_status(_load(3, 25, 140, 6), big_week)
        self.assertIn("60.0 mi week", note)

    def test_recent_window_is_three_days_inclusive_of_today(self):
        runs = [_run_on("2026-05-25", 4), _run_on("2026-05-23", 8), _run_on("2026-05-22", 5)]
        load = db._last_days_load(runs, today=date(2026, 5, 25))
        self.assertEqual(len(load["recent"]), 2)                 # the 22nd is out
        self.assertAlmostEqual(load["total_mi"], 12.0, places=1)
        self.assertAlmostEqual(load["longest_mi"], 8.0, places=1)
        self.assertEqual(load["recent"][0]["start_date_local"][:10], "2026-05-25")  # newest first


class TestCountdown(unittest.TestCase):
    def test_plan_phase_wins_then_generic_bands(self):
        with tempfile.TemporaryDirectory() as tmp:
            with temp_plan(Path(tmp), plan=make_plan(), state={}):
                days, phase, name = db._race_countdown(TUE_WK2, {"phase": "base"})
        self.assertEqual((days, phase, name), ((date(2026, 11, 1) - TUE_WK2).days, "Base", "City Marathon"))
        self.assertEqual(db._race_countdown(date(2026, 10, 1))[1], "Build")
        self.assertEqual(db._race_countdown(date(2026, 10, 10))[1], "Peak block: every session counts")
        self.assertEqual(db._race_countdown(date(2026, 10, 20))[1], "Taper: sharp, not tired")
        self.assertEqual(db._race_countdown(date(2026, 10, 28))[1], "Race week: protect freshness")
        self.assertEqual(db._race_countdown(date(2026, 11, 1))[1], "RACE DAY")


class TestBrief(unittest.TestCase):
    def _brief(self, today, acts=(), state=None, with_plan=True, overrides=None):
        with tempfile.TemporaryDirectory() as tmp:
            with contextlib.ExitStack() as st:
                if overrides:
                    st.enter_context(temp_config(Path(tmp), overrides))
                st.enter_context(temp_plan(Path(tmp), plan=make_plan() if with_plan else None,
                                           state=(state or {}) if with_plan else None))
                st.enter_context(temp_activity_data(Path(tmp), cache_activities=list(acts)))
                st.enter_context(mock.patch.object(db, "BRIEF_PATH", Path(tmp) / "brief.md"))
                text = db.build_brief(today)
                path = db.save_brief(text)
                return text, path.read_text(encoding="utf-8")

    def test_today_session_from_the_layout(self):
        acts = [_run_on("2026-05-25", 5), _run_on("2026-05-24", 8)]
        text, saved = self._brief(TUE_WK2, acts)
        self.assertIn("DAILY BRIEF  |  Tuesday, May 26, 2026", text)
        self.assertIn("Race:       City Marathon  |  159 days  |  Base", text)
        self.assertIn("Fatigue:    RECOVERING", text)
        self.assertIn("TODAY  |  Tue May 26  |  plan wk2 of 4", text)
        self.assertIn("Type:       ", text)
        self.assertIn("Session:    ", text)
        self.assertIn("This week:  20.0 mi, long run 8.0 mi", text)
        self.assertIn("LAST 3 DAYS", text)
        self.assertIn("2026-05-25 |   5.0 mi |   10:00/mi | HR 140  | easy", text)
        self.assertEqual(text, saved)

    def test_standing_note_turns_quality_off_and_prints(self):
        state = {"notes": {"2": [{"date": "2026-05-25", "text": "no speedwork, calf", "until": "2026-06-10"}]}}
        text, _ = self._brief(TUE_WK2, [], state=state)
        self.assertIn("Standing notes:", text)
        self.assertIn("- no speedwork, calf (until 2026-06-10)", text)
        self.assertNotIn("Type:       KEY", text)

    def test_no_plan_and_outside_window(self):
        text, _ = self._brief(TUE_WK2, [], with_plan=False)
        self.assertIn("No plan yet", text)
        self.assertIn("Fatigue:    FRESH", text)
        text, _ = self._brief(date(2026, 5, 10), [])
        self.assertIn("Outside the plan window (2026-05-18 to 2026-06-14)", text)

    def test_metric_units(self):
        acts = [_run_on("2026-05-25", 5)]
        text, _ = self._brief(TUE_WK2, acts, overrides={"athlete": {"units": "km"}})
        self.assertIn("/km", text)
        self.assertNotIn("/mi", text)
        self.assertIn("8.0 km", text)                              # 5 mi
        self.assertEqual(units.unit(), "mi")                       # restored after the block


class TestMain(unittest.TestCase):
    def test_date_and_save_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            with temp_plan(Path(tmp), plan=make_plan(), state={}), \
                    temp_activity_data(Path(tmp)), \
                    mock.patch.object(db, "BRIEF_PATH", Path(tmp) / "brief.md"):
                import io
                with contextlib.redirect_stdout(io.StringIO()):
                    rc = db.main(["--save", "--date", TUE_WK2.isoformat()])
                self.assertEqual(rc, 0)
                self.assertTrue((Path(tmp) / "brief.md").exists())


if __name__ == "__main__":
    unittest.main()
