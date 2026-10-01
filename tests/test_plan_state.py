"""plan_state.json: defaults, schema upgrade, and notes (plain or standing)."""

import tempfile
import unittest
from datetime import date
from pathlib import Path

from tests.helpers import make_plan, temp_plan


class TestLoadState(unittest.TestCase):
    def test_defaults_when_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            with temp_plan(Path(tmp), plan=make_plan()) as mp:
                state = mp.load_state()
                self.assertEqual(state["slide_offset_weeks"], 0)
                self.assertEqual(state["active_race"], "auto")
                self.assertEqual(state["weeks_status"], {})
                self.assertEqual(state["schema_version"], mp.STATE_SCHEMA_VERSION)

    def test_defaults_on_corrupt_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            with temp_plan(Path(tmp), plan=make_plan()) as mp:
                mp.STATE_PATH.write_text("{not json", encoding="utf-8")
                self.assertEqual(mp.load_state()["slide_offset_weeks"], 0)

    def test_save_then_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            with temp_plan(Path(tmp), plan=make_plan()) as mp:
                mp.save_state({"active_race": "auto", "slide_offset_weeks": 2,
                               "weeks_status": {"1": "complete"}})
                state = mp.load_state()
                self.assertEqual(state["slide_offset_weeks"], 2)
                self.assertEqual(state["weeks_status"]["1"], "complete")

    def test_fresh_containers_each_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            with temp_plan(Path(tmp), plan=make_plan()) as mp:
                s1 = mp.load_state()
                s1["notes"]["1"] = ["n"]
                s1["weeks_actuals"]["1"] = {"x": 1}
                s2 = mp.load_state()
                self.assertEqual(s2["notes"], {})
                self.assertEqual(s2["weeks_actuals"], {})

    def test_v2_file_reads_as_v3_with_notes_intact(self):
        v2 = {"schema_version": 2, "active_race": "auto", "slide_offset_weeks": 0,
              "weeks_status": {}, "weeks_status_source": {}, "weeks_actuals": {},
              "notes": {"1": ["2026-05-19: plain note"]}, "last_reconciled": None}
        with tempfile.TemporaryDirectory() as tmp:
            with temp_plan(Path(tmp), plan=make_plan(), state=v2) as mp:
                s = mp.load_state()
                self.assertEqual(s["schema_version"], 3)
                self.assertEqual(s["notes"], v2["notes"])
                self.assertEqual(mp.notes_for_week(1, s)[0]["text"], "plain note")
                self.assertEqual(mp.notes_for_week(1, s)[0]["date"], "2026-05-19")


class TestNotes(unittest.TestCase):
    def test_add_note_plain_and_standing(self):
        import reconcile
        with tempfile.TemporaryDirectory() as tmp:
            with temp_plan(Path(tmp), plan=make_plan()) as mp:
                reconcile.add_note("moved long run to Sunday", today=date(2026, 5, 20))
                reconcile.add_note("no speedwork, calf", today=date(2026, 5, 20),
                                   until="2026-06-05")
                s = mp.load_state()
                wk1 = mp.notes_for_week(1, s)
                self.assertEqual([n["text"] for n in wk1],
                                 ["moved long run to Sunday", "no speedwork, calf"])
                self.assertIsNone(wk1[0]["until"])
                self.assertEqual(wk1[1]["until"], "2026-06-05")
                standing = mp.standing_notes(today=date(2026, 6, 1), state=s)
                self.assertEqual([n["text"] for n in standing], ["no speedwork, calf"])
                self.assertEqual(standing[0]["week"], "1")
                self.assertEqual(mp.standing_notes(today=date(2026, 6, 6), state=s), [])
                self.assertIn("(until 2026-06-05)", mp.format_note(wk1[1]))

    def test_note_outside_the_plan_goes_to_general(self):
        import reconcile
        with tempfile.TemporaryDirectory() as tmp:
            with temp_plan(Path(tmp), plan=make_plan()) as mp:
                reconcile.add_note("travel week", today=date(2026, 1, 5))
                self.assertEqual(mp.general_notes()[0]["text"], "travel week")

    def test_bad_until_raises(self):
        import reconcile
        with tempfile.TemporaryDirectory() as tmp:
            with temp_plan(Path(tmp), plan=make_plan()):
                with self.assertRaises(ValueError):
                    reconcile.add_note("x", today=date(2026, 5, 20), until="soon")


if __name__ == "__main__":
    unittest.main()
