"""strava_sync: a short backfill into a cold cache is raised to the CTL floor."""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import strava_sync
from tests.helpers import make_activity, write_cache_activity


class TestColdBackfillFloor(unittest.TestCase):
    def test_empty_cache_is_floored_then_a_warm_cache_is_honoured(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "acts"
            d.mkdir()
            with mock.patch.object(strava_sync, "ACTIVITIES_DIR", d):
                self.assertEqual(strava_sync._cached_history_days(), 0.0)
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(strava_sync._floor_cold_backfill(21), strava_sync.MIN_COLD_BACKFILL_DAYS)
                self.assertIn("Extending to 90d", out.getvalue())
                self.assertEqual(strava_sync._floor_cold_backfill(120), 120)
                for day in ("2026-05-01", "2026-09-01"):
                    write_cache_activity(d, make_activity(start_date_local=f"{day}T07:00:00"))
                self.assertAlmostEqual(strava_sync._cached_history_days(), 123.0)
                quiet = io.StringIO()
                with contextlib.redirect_stdout(quiet):
                    self.assertEqual(strava_sync._floor_cold_backfill(21), 21)
                self.assertEqual(quiet.getvalue(), "")

    def test_deleted_entries_do_not_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "acts"
            d.mkdir()
            write_cache_activity(d, make_activity(start_date_local="2026-01-01T07:00:00",
                                                  _deleted_at="2026-02-01"))
            write_cache_activity(d, make_activity(start_date_local="2026-09-01T07:00:00"))
            with mock.patch.object(strava_sync, "ACTIVITIES_DIR", d):
                self.assertEqual(strava_sync._cached_history_days(), 0.0)


if __name__ == "__main__":
    unittest.main()
