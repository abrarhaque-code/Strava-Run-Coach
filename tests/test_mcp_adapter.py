"""Tests for the Strava MCP -> cache adapter, incl. cross-train classification."""

import contextlib
import io
import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

import mcp_adapter
import strava_sync
from enrichment import enrich
from mcp_adapter import (
    classify_type, convert, ingest_mcp_file, merge_performance,
    mcp_to_cache_activity, write_to_cache,
)
from tests.helpers import make_activity


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


# ---------------------------------------------------------------------------
# The merge layer: list + perf/<id>.json + streams/<id>.json in one command
# ---------------------------------------------------------------------------


MINIMAL_ACTIVITY = {
    "id": 88000001,
    "type": "Run",
    "start_date_local": "2026-07-13T20:30:00",
    "distance": 8046.7,
    "moving_time": 3000,
    "average_heartrate": 140.0,
    "max_speed": 3.9,
}


@contextlib.contextmanager
def _isolated_cache(tmp_path: Path):
    """Point every path the ingest touches at tmp, so a test can never write
    into the real cache or activities.csv."""
    cache_dir = tmp_path / "cache"
    activities_dir = cache_dir / "activities"
    csv_path = tmp_path / "activities.csv"
    saved = (strava_sync.CACHE_DIR, strava_sync.ACTIVITIES_DIR, strava_sync.CSV_PATH,
             mcp_adapter.CACHE_DIR, mcp_adapter.STREAMS_DIR, mcp_adapter.MCP_DIR)
    strava_sync.CACHE_DIR = cache_dir
    strava_sync.ACTIVITIES_DIR = activities_dir
    strava_sync.CSV_PATH = csv_path
    mcp_adapter.CACHE_DIR = activities_dir
    mcp_adapter.STREAMS_DIR = cache_dir / "streams"
    mcp_adapter.MCP_DIR = tmp_path / "mcp"
    try:
        yield activities_dir
    finally:
        (strava_sync.CACHE_DIR, strava_sync.ACTIVITIES_DIR, strava_sync.CSV_PATH,
         mcp_adapter.CACHE_DIR, mcp_adapter.STREAMS_DIR, mcp_adapter.MCP_DIR) = saved


def _mcp_list_item(aid, start="2026-09-05T07:53:37", sport="Run", **extra):
    item = {"id": str(aid), "name": "Morning Run", "sport_type": sport, "start_local": start,
            "is_trainer": False,
            "summary": {"distance": 23318.2, "moving_time": 8071, "elapsed_time": 10188,
                        "elevation_gain": 180, "avg_speed": 2.889, "max_speed": 6.18,
                        "relative_effort": 190}}
    item.update(extra)
    return item


def _mcp_perf(hr=148.8):
    return {"has_heartrate": True, "average_heartrate": hr, "max_heartrate": 172,
            "average_watts": 325.8,
            "laps": [{"elapsed_time": 600, "moving_time": 580, "distance": 1609.34,
                      "avg_hr": 131.2, "max_hr": 144, "avg_watts": 312.8}],
            "best_efforts": [{"name": "1 mile", "elapsed_time": 500, "distance": 1609.34}],
            "segment_efforts": [{"name": "big hill", "elapsed_time": 300}]}


def _mcp_streams():
    return {"heart_rate": [130, 140, 150], "time": [0, 5, 10], "distance": [0, 15, 30],
            "velocity_smooth": [0, 3, 3], "moving": [False, True, True]}


class TestImportActivities(unittest.TestCase):
    def test_minimal_activity_gets_start_date_backfilled(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _isolated_cache(Path(tmp)) as activities_dir:
                mcp_adapter.import_activities([dict(MINIMAL_ACTIVITY)], verbose=False)
                cached = json.loads((activities_dir / "88000001.json").read_text())
                self.assertEqual(cached["start_date"], MINIMAL_ACTIVITY["start_date_local"])

    def test_skips_activities_missing_required_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _isolated_cache(Path(tmp)):
                result = mcp_adapter.import_activities(
                    [{"id": 1, "type": "Run"}, dict(MINIMAL_ACTIVITY)], verbose=False)
                self.assertEqual(result["skipped"], 1)
                self.assertEqual(result["new"], 1)

    def test_mcp_shaped_activity_is_normalized(self):
        mcp = {
            "id": 88000010, "name": "Summer streets!", "sport_type": "Run",
            "start_local": "2026-08-08T07:31:28",
            "summary": {"distance": 13770.6, "moving_time": 4684, "elapsed_time": 5885,
                        "elevation_gain": 69, "avg_speed": 2.9399, "max_speed": 6.51616,
                        "avg_cadence": 80.1551, "total_calories": 1032, "avg_hr": 150.456,
                        "max_hr": 181},
            "laps": [{"moving_time": 527, "distance": 1609.34, "avg_hr": 130.9, "max_hr": 148},
                     {"moving_time": 554, "distance": 1609.34, "avg_hr": 145.6, "max_hr": 153}],
        }
        with tempfile.TemporaryDirectory() as tmp:
            with _isolated_cache(Path(tmp)) as activities_dir:
                res = mcp_adapter.import_activities([mcp], verbose=False)
                self.assertEqual((res["new"], res["with_laps"], res["with_hr"]), (1, 1, 1))
                c = json.loads((activities_dir / "88000010.json").read_text())
        self.assertEqual(c["type"], "Run")
        self.assertEqual(c["start_date_local"], "2026-08-08T07:31:28")
        self.assertEqual(c["distance"], 13770.6)
        self.assertEqual(c["total_elevation_gain"], 69)
        self.assertEqual(c["average_heartrate"], 150.456)    # summary.avg_hr mapped
        self.assertEqual(c["calories"], 1032)
        self.assertNotIn("summary", c)
        self.assertEqual(c["laps"][0]["average_heartrate"], 130.9)
        self.assertNotIn("avg_hr", c["laps"][0])
        self.assertEqual([lp["lap_index"] for lp in c["laps"]], [1, 2])
        self.assertTrue(c["_is_real_run"])
        self.assertGreater(c["_run_tss"], 0)

    def test_normalize_is_idempotent_and_rest_shape_untouched(self):
        rest = dict(MINIMAL_ACTIVITY)
        self.assertEqual(mcp_adapter.normalize_mcp_activity(rest), rest)
        once = mcp_adapter.normalize_mcp_activity(_mcp_list_item(5))
        self.assertEqual(mcp_adapter.normalize_mcp_activity(once), once)

    def test_trailrun_and_treadmill_tags(self):
        trail = _mcp_list_item(88000020, sport="TrailRun")
        mill = _mcp_list_item(88000021, is_trainer=True)
        with tempfile.TemporaryDirectory() as tmp:
            with _isolated_cache(Path(tmp)) as activities_dir:
                mcp_adapter.import_activities([trail, mill], verbose=False)
                t = json.loads((activities_dir / "88000020.json").read_text())
                m = json.loads((activities_dir / "88000021.json").read_text())
        self.assertEqual(t["type"], "Run")
        self.assertEqual(t["_activity_class"], "run")
        self.assertTrue(m["trainer"])
        self.assertNotIn("is_trainer", m)
        self.assertEqual(m["_activity_class"], "treadmill_run")

    def test_crosstrain_with_hr_is_still_a_ride(self):
        bike = _mcp_list_item(88000030, name="Zone 2 bike")
        merged = {**bike, **_mcp_perf(hr=121.0)}
        with tempfile.TemporaryDirectory() as tmp:
            with _isolated_cache(Path(tmp)) as activities_dir:
                mcp_adapter.import_activities([merged], verbose=False)
                c = json.loads((activities_dir / "88000030.json").read_text())
        self.assertEqual(c["type"], "CrossTrain")
        self.assertEqual(c["_activity_class"], "ride")
        self.assertEqual(c["average_heartrate"], 121.0)
        self.assertNotIn("segment_efforts", c)

    def test_relative_effort_reaches_the_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _isolated_cache(Path(tmp)):
                mcp_adapter.import_activities([_mcp_list_item(88000040)], csv=True, verbose=False)
                rows = strava_sync.CSV_PATH.read_text(encoding="utf-8").splitlines()
        import csv as _csv
        row = next(_csv.reader([rows[1]]))
        self.assertEqual(row[0], "88000040")
        self.assertEqual(row[8], "190")

    def test_reimport_without_perf_preserves_hr_laps_and_best_efforts(self):
        full = {**_mcp_list_item(88000050), **_mcp_perf()}
        summary_only = _mcp_list_item(88000050)
        with tempfile.TemporaryDirectory() as tmp:
            with _isolated_cache(Path(tmp)) as activities_dir:
                mcp_adapter.import_activities([full], verbose=False)
                mcp_adapter.import_activities([summary_only], verbose=False)
                c = json.loads((activities_dir / "88000050.json").read_text())
        self.assertEqual(len(c["laps"]), 1)
        self.assertAlmostEqual(c["average_heartrate"], 148.8)
        self.assertEqual(c["best_efforts"][0]["name"], "1 mile")


class TestMergeImport(unittest.TestCase):
    def _write(self, base, rel, obj):
        p = base / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(obj), encoding="utf-8")
        return p

    def test_id_from_path_leading_digits_and_none(self):
        self.assertEqual(mcp_adapter.id_from_path("perf/20145951475.json"), 20145951475)
        self.assertEqual(mcp_adapter.id_from_path(Path("x/20145951475_streams.json")), 20145951475)
        self.assertIsNone(mcp_adapter.id_from_path("perf/list.json"))
        self.assertIsNone(mcp_adapter.id_from_path("mcp/2026.json"))
        self.assertIsNone(mcp_adapter.id_from_path(None))

    def test_classify_payload_each_shape(self):
        cp = mcp_adapter.classify_payload
        self.assertEqual(cp([_mcp_list_item(1)])[0], "summaries")
        self.assertEqual(cp({"activities": []})[0], "summaries")
        self.assertEqual(cp(_mcp_perf(), "perf/88000077.json"), ("performance", 88000077))
        self.assertEqual(cp(_mcp_streams(), "streams/88000077.json"), ("streams", 88000077))
        rest_streams = {"heartrate": {"data": [1, 2]}, "time": {"data": [0, 1]}}
        self.assertEqual(cp(rest_streams, "s/88000078.json"), ("streams", 88000078))
        self.assertEqual(cp({"88000077": _mcp_perf()})[0], "idmap")
        self.assertEqual(cp(_mcp_list_item(88000005)), ("activity", 88000005))
        self.assertEqual(cp({"foo": 1}, "x.json"), ("unknown", None))
        self.assertEqual(cp("nope")[0], "unknown")
        self.assertEqual(cp({"first_name": "A"}, "data/mcp/profile.json")[0], "ignored")
        self.assertEqual(cp({"heart_rate_zones": []}, "zones.json")[0], "ignored")

    def test_merge_joins_perf_and_streams_by_filename_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            with _isolated_cache(tmp) as activities_dir:
                inputs = tmp / "mcp"
                self._write(inputs, "list/page-1.json",
                            [_mcp_list_item(88000101), _mcp_list_item(88000102, "2026-09-06T08:00:00")])
                self._write(inputs, "perf/88000101.json", _mcp_perf())
                self._write(inputs, "streams/88000101.json", _mcp_streams())
                self._write(inputs, "profile.json", {"first_name": "A"})
                with contextlib.redirect_stdout(io.StringIO()):
                    result = mcp_adapter.run_merge([str(inputs)])
                st = result["stats"]
                self.assertEqual((st["perf_matched"], st["streams_matched"]), (1, 1))
                self.assertEqual(result["warnings"], [])
                self.assertEqual(result["import"]["new"], 2)
                self.assertEqual(result["import"]["with_hr"], 1)
                cached = json.loads((activities_dir / "88000101.json").read_text())
                self.assertEqual(cached["laps"][0]["average_heartrate"], 131.2)
                self.assertAlmostEqual(cached["average_heartrate"], 148.8)
                self.assertTrue((mcp_adapter.STREAMS_DIR / "88000101.json").exists())
                self.assertFalse((mcp_adapter.STREAMS_DIR / "88000102.json").exists())
                self.assertNotIn("streams", cached)

    def test_pages_in_one_file(self):
        pages = [{"activities": [_mcp_list_item(88000111)], "has_next_page": True},
                 {"activities": [_mcp_list_item(88000112)], "has_next_page": False}]
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            with _isolated_cache(tmp):
                self._write(tmp / "mcp", "list/all.json", pages)
                with contextlib.redirect_stdout(io.StringIO()):
                    result = mcp_adapter.run_merge([str(tmp / "mcp")], dry_run=True)
                self.assertEqual(result["stats"]["summaries"], 2)

    def test_merge_prefers_embedded_id_over_filename(self):
        perf = dict(_mcp_perf())
        perf["id"] = 88000201
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            with _isolated_cache(tmp):
                inputs = tmp / "mcp"
                self._write(inputs, "list.json", [_mcp_list_item(88000201)])
                self._write(inputs, "perf/99999999.json", perf)
                with contextlib.redirect_stdout(io.StringIO()):
                    result = mcp_adapter.run_merge([str(inputs)], dry_run=True)
                self.assertEqual(result["stats"]["perf_matched"], 1)
                self.assertEqual(result["stats"]["perf_unmatched"], [])

    def test_merge_accepts_idmap_file_and_tool_result_wrapper(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            with _isolated_cache(tmp) as activities_dir:
                inputs = tmp / "mcp"
                wrapped = [{"type": "text",
                            "text": json.dumps({"activities": [_mcp_list_item(88000301)]})}]
                self._write(inputs, "list.json", wrapped)
                self._write(inputs, "perf.json", {"88000301": _mcp_perf(hr=150.0)})
                self._write(inputs, "streams.json", {"88000301": _mcp_streams()})
                with contextlib.redirect_stdout(io.StringIO()):
                    result = mcp_adapter.run_merge([str(inputs)])
                self.assertEqual(result["stats"]["perf_matched"], 1)
                self.assertEqual(result["stats"]["streams_matched"], 1)
                cached = json.loads((activities_dir / "88000301.json").read_text())
                self.assertEqual(cached["average_heartrate"], 150.0)

    def test_merge_reports_unmatched_without_crashing(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            with _isolated_cache(tmp):
                inputs = tmp / "mcp"
                self._write(inputs, "list.json", [_mcp_list_item(88000401)])
                self._write(inputs, "perf/88000499.json", _mcp_perf())   # no summary, not cached
                self._write(inputs, "perf/noid.json", _mcp_perf())       # no id anywhere
                self._write(inputs, "junk.json", {"foo": 1})
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    result = mcp_adapter.run_merge([str(inputs)], dry_run=True)
                self.assertEqual(result["stats"]["perf_unmatched"], [88000499])
                self.assertEqual(len(result["warnings"]), 2)
                self.assertIn("unmatched", buf.getvalue())
                self.assertIn("dry run", buf.getvalue())

    def test_merge_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            with _isolated_cache(tmp) as activities_dir:
                inputs = tmp / "mcp"
                self._write(inputs, "list.json", [_mcp_list_item(88000501)])
                self._write(inputs, "streams/88000501.json", _mcp_streams())
                with contextlib.redirect_stdout(io.StringIO()):
                    result = mcp_adapter.run_merge([str(inputs)], dry_run=True)
                self.assertIsNone(result["import"])
                self.assertFalse(activities_dir.exists() and any(activities_dir.iterdir()))
                self.assertFalse(mcp_adapter.STREAMS_DIR.exists())

    def test_merge_streams_for_already_cached_activity(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            with _isolated_cache(tmp) as activities_dir:
                mcp_adapter.import_activities([_mcp_list_item(88000601)], verbose=False)
                self.assertTrue((activities_dir / "88000601.json").exists())
                inputs = tmp / "later"
                self._write(inputs, "streams/88000601.json", _mcp_streams())
                with contextlib.redirect_stdout(io.StringIO()):
                    result = mcp_adapter.run_merge([str(inputs)])
                self.assertEqual(result["stats"]["cache_backed"], 1)
                self.assertEqual(result["stats"]["streams_unmatched"], [])
                self.assertTrue((mcp_adapter.STREAMS_DIR / "88000601.json").exists())
                self.assertEqual(result["import"]["updated"], 1)

    def test_parse_cli_including_legacy_tokens(self):
        o = mcp_adapter.parse_cli(["list.json", "perf/1.json", "--dry-run"])
        self.assertEqual((o["dry_run"], o["paths"]), (True, ["list.json", "perf/1.json"]))
        o = mcp_adapter.parse_cli(["--from-mcp", "a.json", "--performance", "p/"])
        self.assertEqual(o["paths"], ["a.json", "p/"])
        with self.assertRaises(ValueError):
            mcp_adapter.parse_cli(["--bogus", "x"])


class TestFetchPlan(unittest.TestCase):
    def _run(self, aid, days_ago, today, miles=5.0, hr=None, name="Run"):
        d = (today - timedelta(days=days_ago)).isoformat() + "T07:00:00"
        return enrich(make_activity(id=aid, name=name, start_date_local=d, start_date=d + "Z",
                                    distance=miles * 1609.34, moving_time=int(miles * 600),
                                    average_heartrate=hr))

    def test_selection_and_inbox_skip(self):
        today = date(2026, 10, 1)
        acts = [
            self._run(1, 3, today),                          # recent easy, no HR -> perf
            self._run(2, 40, today),                         # old easy, no HR -> nothing
            self._run(3, 40, today, miles=10),               # old long, no HR -> perf, no streams
            self._run(4, 5, today, miles=12, hr=150.0),      # recent long with HR -> streams only
            self._run(5, 6, today, hr=140.0, name="6 x 800"),  # named workout with HR -> streams
            self._run(6, 100, today, miles=12),              # outside window
            self._run(7, 2, today),                          # perf already in inbox
        ]
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            inbox = tmp / "mcp"
            (inbox / "perf").mkdir(parents=True)
            (inbox / "perf" / "7.json").write_text("{}", encoding="utf-8")
            sdir = tmp / "streams"
            plan = mcp_adapter.fetch_plan(acts, today=today, inbox=inbox, streams_dir=sdir)
        self.assertEqual(sorted(plan["perf_needed"]), [1, 3])
        self.assertEqual(sorted(plan["streams_needed"]), [4, 5])
        self.assertEqual(plan["range_start"], "2026-07-03T00:00:00")
        self.assertEqual(plan["history_days"], 100)

    def test_history_days_empty(self):
        self.assertEqual(mcp_adapter.history_days([]), 0)


if __name__ == "__main__":
    unittest.main()
