"""Tests for the Strava MCP -> cache adapter, incl. cross-train classification."""

import json
import tempfile
import unittest
from pathlib import Path

from mcp_adapter import (
    classify_type, convert, ingest_mcp_file, merge_performance,
    mcp_to_cache_activity, write_to_cache,
)


class TestClassify(unittest.TestCase):
    def test_plain_run_is_run(self):
        self.assertEqual(classify_type("Run", "Evening Run", "", 5000), "Run")

    def test_bike_logged_as_run_is_crosstrain(self):
        # Zone-2 bikes logged as "Run"; the description betrays them.
        self.assertEqual(
            classify_type("Run", "Zone 2", "121 bpm - 30 min bike", 4800), "CrossTrain")

    def test_ride_is_crosstrain(self):
        self.assertEqual(classify_type("Ride", "Evening Ride", "", 0), "CrossTrain")

    def test_bike_signature_without_keywords_is_crosstrain(self):
        # Live-sampled shape: manual entry (max_speed 0) at exactly the
        # configured bike-equivalence speed, but a name with no bike keyword.
        summary = {"distance": 4023.4, "moving_time": 1500, "elapsed_time": 1500,
                   "avg_speed": 2.6822666666666666, "max_speed": 0}
        self.assertEqual(
            classify_type("Run", "Zone 2 - 124 bpm", "", 4023.4, summary),
            "CrossTrain")

    def test_real_run_summary_stays_run(self):
        # Live-sampled real run: max_speed present, avg speed off-signature.
        summary = {"distance": 8616.78, "moving_time": 2716,
                   "avg_speed": 3.1726, "max_speed": 3.86}
        self.assertEqual(
            classify_type("Run", "Evening Run", "", 8616.78, summary), "Run")

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

    def test_write_to_cache_enriches(self):
        # MCP-ingested activities must carry the same precomputed fields the
        # REST sync writes, or downstream consumers read stale/naive values.
        with tempfile.TemporaryDirectory() as d:
            cache = Path(d)
            write_to_cache(convert([self._act(id="7")]), cache)
            a = json.loads((cache / "7.json").read_text())
            self.assertEqual(a["_activity_class"], "run")
            self.assertIn("_run_tss", a)
            self.assertGreater(a["_enriched_v"], 0)


class TestAdapterV2(unittest.TestCase):
    """Pagination, performance merge, multi-file ingest."""

    def _act(self, aid, dist=8616.78, mt=2716, name="Evening Run"):
        return {"id": aid, "name": name, "sport_type": "Run",
                "start_local": "2026-07-15T19:33:27",
                "summary": {"distance": dist, "moving_time": mt,
                            "elapsed_time": mt, "elevation_gain": 0,
                            "avg_speed": dist / mt, "max_speed": 3.86,
                            "avg_cadence": 79.9}}

    # Live-sampled get_activity_performance shape (laps use avg_hr/max_hr).
    _PERF = {
        "has_heartrate": True, "has_device_watts": True,
        "average_heartrate": 132.041, "max_heartrate": 161,
        "average_watts": 343.161, "average_cadence": 79.9034, "calories": 473,
        "laps": [
            {"elapsed_time": 539, "moving_time": 539, "start_index": 0,
             "end_index": 540, "distance": 1609.34, "elevation_gain": 0,
             "avg_watts": 324.283, "max_speed": 3.7, "avg_hr": 119.787,
             "max_hr": 131, "avg_grade": 0, "avg_cadence": 78.7384},
            {"elapsed_time": 512, "moving_time": 512, "start_index": 541,
             "end_index": 1052, "distance": 1609.34, "elevation_gain": 0,
             "avg_watts": 340.092, "max_speed": 3.68, "avg_hr": 126.1,
             "max_hr": 134, "avg_grade": 0, "avg_cadence": 80.1859},
        ],
        "best_efforts": [
            {"name": "1 mile", "elapsed_time": 500, "distance": 1609.34},
            {"name": "", "elapsed_time": 0},  # malformed: must be dropped
        ],
    }

    def test_multi_page_array_convert(self):
        pages = [
            {"activities": [self._act("1")], "has_next_page": True,
             "end_cursor": "abc"},
            {"activities": [self._act("2")], "has_next_page": False,
             "end_cursor": "def"},
        ]
        out = convert(pages)
        self.assertEqual(sorted(o["id"] for o in out), ["1", "2"])

    def test_performance_merge_flips_tss_and_maps_laps(self):
        with tempfile.TemporaryDirectory() as d:
            cache = Path(d)
            write_to_cache(convert([self._act("19330270757")]), cache)
            before = json.loads((cache / "19330270757.json").read_text())
            self.assertGreater(before["_run_tss"], 0)  # pace-based

            n = merge_performance({"19330270757": dict(self._PERF)}, cache)
            self.assertEqual(n, 1)
            a = json.loads((cache / "19330270757.json").read_text())
            self.assertEqual(a["average_heartrate"], 132.041)
            # HR-based TSS: (132.041/165)^2 * (2716/3600) * 100 ~= 48.3
            self.assertAlmostEqual(a["_run_tss"], 48.3, delta=1.5)
            self.assertNotAlmostEqual(a["_run_tss"], before["_run_tss"], delta=5)
            # Laps renamed to REST fields + ordinal lap_index
            self.assertEqual(a["laps"][0]["average_heartrate"], 119.787)
            self.assertEqual(a["laps"][0]["max_heartrate"], 131)
            self.assertEqual(a["laps"][0]["lap_index"], 1)
            self.assertNotIn("avg_hr", a["laps"][0])
            # Malformed best_efforts entry dropped, valid one kept
            self.assertEqual(len(a["best_efforts"]), 1)
            self.assertEqual(a["best_efforts"][0]["name"], "1 mile")

    def test_merge_skips_unknown_ids(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(
                merge_performance({"nope": dict(self._PERF)}, Path(d)), 0)

    def test_ingest_multiple_files(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            p1 = d / "page1.json"
            p2 = d / "page2.json"
            p1.write_text(json.dumps({"activities": [self._act("11")]}))
            p2.write_text(json.dumps({"activities": [self._act("22")]}))
            cache = d / "cache"
            summary = ingest_mcp_file([str(p1), str(p2)], cache_dir=cache)
            self.assertEqual(summary["written"], 2)
            # CSV rebuild auto-skips when cache_dir is overridden (tests)
            self.assertEqual(summary["csv_rows"], 0)


if __name__ == "__main__":
    unittest.main()
