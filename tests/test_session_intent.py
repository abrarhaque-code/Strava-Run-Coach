import tempfile
import unittest
from pathlib import Path

import session_intent as si
from tests.helpers import temp_config


class TestParseIntent(unittest.TestCase):
    def test_easy_hmp_easy(self):
        it = si.parse_intent("13 easy, 3 hmp, 2 easy", 18.02, unit="mi")
        kinds = [(s["kind"], s["miles"]) for s in it["segments"]]
        self.assertEqual(kinds, [("easy", 13.0), ("half", 3.0), ("easy", 2.0)])
        self.assertAlmostEqual(it["segments"][-1]["end_mi"], 18.02)
        self.assertEqual(si.summary(it), "13 easy, 3 half, 2 easy")
        self.assertEqual([s["kind"] for s in si.quality_segments(it)], ["half"])

    def test_embedded_block(self):
        it = si.parse_intent("20 w/ 10 @ MP", 20.1, unit="mi")
        self.assertEqual(it["segments"][0]["kind"], "mp")
        self.assertFalse(it["segments"][0]["placed"])
        last = si.parse_intent("Long run 20 with the last 10 @ MP", 20.0, unit="mi")
        self.assertEqual([s["kind"] for s in last["segments"]], ["easy", "mp"])
        self.assertEqual(last["segments"][1]["start_mi"], 10.0)

    def test_out_of_tolerance_and_reps_are_none(self):
        self.assertIsNone(si.parse_intent("13 easy, 3 hmp, 2 easy", 12.0, unit="mi"))
        self.assertIsNone(si.parse_intent("7 x 1k", 7.6, unit="mi"))
        self.assertIsNone(si.parse_intent("16 x 400 (or something like that I lost count)", 6.2, unit="mi"))
        self.assertIsNone(si.parse_intent("Eff it midweek long run", 18.0, unit="mi"))
        self.assertIsNone(si.parse_intent("", 5.0))

    def test_warmup_tempo_cooldown(self):
        it = si.parse_intent("3 wu, 4 tempo, 2 cd", 9.1, unit="mi")
        self.assertEqual([s["kind"] for s in it["segments"]], ["easy", "tempo", "easy"])

    def test_explicit_km_suffix_in_a_miles_world(self):
        it = si.parse_intent("3 mi easy, 5k tempo, 1 mile cd", 7.1, unit="mi")
        self.assertEqual([s["kind"] for s in it["segments"]], ["easy", "tempo", "easy"])
        self.assertAlmostEqual(it["segments"][1]["miles"], 5 / 1.609344, places=4)

    def test_bare_numbers_follow_the_stated_unit(self):
        run_mi = 20.1 / 1.609344
        it = si.parse_intent("15 easy, 5 tempo", run_mi, unit="km")
        self.assertIsNotNone(it)
        self.assertAlmostEqual(it["segments"][0]["miles"], 15 / 1.609344, places=4)
        self.assertEqual(si.summary(it), "15 easy, 5 tempo")
        # the same text read as miles is out of tolerance for a 20.1 km run
        self.assertIsNone(si.parse_intent("15 easy, 5 tempo", run_mi, unit="mi"))

    def test_default_unit_comes_from_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            with temp_config(Path(tmp), {"athlete": {"units": "km"}}):
                it = si.parse_intent("15 easy, 5 tempo", 20.1 / 1.609344)
                self.assertEqual(it["unit"], "km")


if __name__ == "__main__":
    unittest.main()
