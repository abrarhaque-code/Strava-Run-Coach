"""metrics.py is the only module that reads the activity cache, the CSV and
the streams directory. These pin the lookups the other modules delegate to."""

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import metrics
from enrichment import enrich
from tests.helpers import make_activity, make_bike_equiv, temp_activity_data


def _run(start: str, miles: float = 5.0, **kw):
    return enrich(make_activity(start_date_local=start, start_date=start + "Z",
                                distance=miles * 1609.34, moving_time=int(miles * 600), **kw))


class TestStreams(unittest.TestCase):
    def test_load_streams_present_missing_corrupt(self):
        with tempfile.TemporaryDirectory() as tmp:
            with temp_activity_data(Path(tmp), streams={1001: {"heart_rate": [1, 2, 3],
                                                             "time": [0, 1, 2]}}) as paths:
                self.assertEqual(metrics.load_streams(1001)["heart_rate"], [1, 2, 3])
                self.assertTrue(metrics.has_streams(1001))
                self.assertIsNone(metrics.load_streams(1002))
                self.assertFalse(metrics.has_streams(1002))
                (paths["streams_dir"] / "1003.json").write_text("not json", encoding="utf-8")
                self.assertIsNone(metrics.load_streams(1003))


class TestLookups(unittest.TestCase):
    def test_latest_run_skips_bike_equiv_and_deleted(self):
        older = _run("2026-07-13T20:30:00")
        newer = _run("2026-07-15T20:30:00")
        fake = enrich(make_bike_equiv(start_date_local="2026-07-16T20:00:00",
                                      start_date="2026-07-16T20:00:00Z"))
        deleted = _run("2026-07-17T20:30:00", _deleted_at="2026-07-18T00:00:00Z")
        with tempfile.TemporaryDirectory() as tmp:
            with temp_activity_data(Path(tmp), cache_activities=[older, newer, fake, deleted]):
                self.assertEqual(metrics.latest_run()["id"], newer["id"])

    def test_latest_run_none_on_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            with temp_activity_data(Path(tmp)):
                self.assertIsNone(metrics.latest_run())

    def test_runs_since_uses_datetime_cutoff(self):
        ref = datetime(2026, 9, 12, 12, 0, 0)
        inside = _run((ref - timedelta(days=2, hours=23)).isoformat(timespec="seconds"))
        outside = _run((ref - timedelta(days=3, hours=1)).isoformat(timespec="seconds"))
        with tempfile.TemporaryDirectory() as tmp:
            with temp_activity_data(Path(tmp), cache_activities=[inside, outside]):
                got = metrics.runs_since(3, ref=ref)
                self.assertEqual([r["id"] for r in got], [inside["id"]])
                self.assertEqual(metrics.runs_since(3, ref=ref, runs=[inside, outside]), [inside])

    def test_activity_by_id_cache_then_csv(self):
        cached = _run("2026-07-13T20:30:00")
        csv_only = _run("2026-06-01T08:00:00")
        with tempfile.TemporaryDirectory() as tmp:
            with temp_activity_data(Path(tmp), cache_activities=[cached], csv_activities=[csv_only]):
                self.assertEqual(metrics.activity_by_id(cached["id"])["id"], cached["id"])
                row = metrics.activity_by_id(csv_only["id"])
                self.assertEqual(str(row["id"]), str(csv_only["id"]))
                self.assertTrue(row.get("_from_csv"))
                self.assertIsNone(metrics.activity_by_id(424242))

    def test_csv_rows_carry_from_csv_and_elapsed(self):
        a = _run("2026-06-01T08:00:00")
        with tempfile.TemporaryDirectory() as tmp:
            with temp_activity_data(Path(tmp), csv_activities=[a]):
                rows = metrics.load_activities(activity_type="Run")
                self.assertEqual(len(rows), 1)
                self.assertTrue(rows[0]["_from_csv"])
                self.assertEqual(rows[0]["elapsed_time"], a["elapsed_time"])


if __name__ == "__main__":
    unittest.main()
