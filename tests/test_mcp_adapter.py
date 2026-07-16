"""Tests for the Strava MCP -> cache adapter, incl. cross-train classification."""

import json
import tempfile
import unittest
from pathlib import Path

from mcp_adapter import classify_type, mcp_to_cache_activity, convert, write_to_cache


class TestClassify(unittest.TestCase):
    def test_plain_run_is_run(self):
        self.assertEqual(classify_type("Run", "Evening Run", "", 5000), "Run")

    def test_bike_logged_as_run_is_crosstrain(self):
        # Zone-2 bikes logged as "Run"; the description betrays them.
        self.assertEqual(
            classify_type("Run", "Zone 2", "121 bpm - 30 min bike", 4800), "CrossTrain")

    def test_ride_is_crosstrain(self):
        self.assertEqual(classify_type("Ride", "Evening Ride", "", 0), "CrossTrain")

    def test_weight_training_is_strength(self):
        self.assertEqual(classify_type("WeightTraining", "Full Body", "Gym", 0), "WeightTraining")


class TestConvert(unittest.TestCase):
    def _act(self, **kw):
        base = {"id": "1", "name": "Run", "sport_type": "Run",
                "start_local": "2026-05-16T07:00:00",
                "summary": {"distance": 21000, "moving_time": 6900, "elapsed_time": 6900}}
        base.update(kw)
        return base

    def test_field_mapping(self):
        out = mcp_to_cache_activity(self._act())
        self.assertEqual(out["type"], "Run")
        self.assertEqual(out["distance"], 21000)
        self.assertEqual(out["moving_time"], 6900)
        self.assertEqual(out["start_date_local"], "2026-05-16T07:00:00")

    def test_crosstrain_tagged(self):
        out = mcp_to_cache_activity(
            self._act(name="Zone 2 bike", description="25 pre lift, 15 post"))
        self.assertEqual(out["type"], "CrossTrain")
        self.assertTrue(out["_crosstrain"])

    def test_convert_accepts_wrapper_and_list(self):
        acts = [self._act(id="1"), self._act(id="2")]
        self.assertEqual(len(convert({"activities": acts})), 2)
        self.assertEqual(len(convert(acts)), 2)

    def test_write_to_cache_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            cache = Path(d)
            dicts = convert([self._act(id="42")])
            n1 = write_to_cache(dicts, cache)
            n2 = write_to_cache(dicts, cache)  # rewrite same id
            self.assertEqual(n1, 1)
            self.assertEqual(n2, 1)
            self.assertEqual(len(list(cache.glob("*.json"))), 1)
            written = json.loads((cache / "42.json").read_text())
            self.assertEqual(written["id"], "42")


if __name__ == "__main__":
    unittest.main()
