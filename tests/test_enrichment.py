"""Tests for activity classification and TSS routing (enrichment v3).

The classification is the single source of truth for "is this a real run?" —
bike sessions manually logged as Run entries, zero-distance entries, and
soft-deleted activities must never reach run mileage, VDOT, or run TSS.
"""

import tempfile
import unittest
from pathlib import Path

from enrichment import (
    ENRICHMENT_VERSION, bike_equiv_mps, classify_activity, enrich, is_real_run,
    needs_enrichment,
)
from tests.helpers import make_activity, make_bike_equiv, temp_activity_data


class TestClassifyActivity(unittest.TestCase):
    def test_outdoor_run(self):
        self.assertEqual(classify_activity(make_activity()), "run")

    def test_treadmill_run_is_real(self):
        a = make_activity(trainer=True, max_speed=3.9, total_elevation_gain=0)
        self.assertEqual(classify_activity(a), "treadmill_run")
        self.assertTrue(is_real_run(a))

    def test_signature_derived_from_config(self):
        # 10 min/mi convention -> exactly 6.0 mph in m/s.
        self.assertAlmostEqual(bike_equiv_mps(), 2.68223, places=4)

    def test_fake_bike_by_average_speed_field(self):
        a = make_bike_equiv()
        self.assertEqual(classify_activity(a), "bike_equiv")
        self.assertFalse(is_real_run(a))

    def test_fake_bike_by_computed_speed_only(self):
        # CSV path: average_speed column may be missing; computed dist/time
        # still betrays the exact-signature speed.
        a = make_bike_equiv(average_speed=None)
        self.assertEqual(classify_activity(a), "bike_equiv")

    def test_zero_distance_run_is_invalid(self):
        a = make_activity(distance=0, average_speed=0, max_speed=0)
        self.assertEqual(classify_activity(a), "invalid")
        self.assertFalse(is_real_run(a))

    def test_manual_run_off_signature_stays_run(self):
        # Manual entry (no max_speed) at 2.75 m/s is NOT the bike signature.
        a = make_activity(max_speed=0, average_speed=2.75,
                          distance=4125, moving_time=1500)
        self.assertEqual(classify_activity(a), "run")

    def test_ride(self):
        a = make_activity(type="Ride")
        self.assertEqual(classify_activity(a), "ride")

    def test_mcp_crosstrain_type_is_ride_bucket(self):
        # mcp_adapter rewrites bike-as-run to type CrossTrain at ingest; the
        # classifier must land it in the same cross-training TSS bucket.
        a = make_bike_equiv(type="CrossTrain")
        self.assertEqual(classify_activity(a), "ride")
        self.assertFalse(is_real_run(a))

    def test_soft_deleted_never_real(self):
        a = make_activity(_deleted_at="2026-07-01T00:00:00Z")
        self.assertFalse(is_real_run(a))

    def test_csv_style_string_fields(self):
        # CSV rows arrive with string speeds and "" for absent max_speed.
        a = make_bike_equiv(max_speed=None, average_speed="2.682244444")
        self.assertEqual(classify_activity(a), "bike_equiv")


class TestTssRouting(unittest.TestCase):
    def test_real_run_gets_run_tss(self):
        a = enrich(make_activity())
        self.assertGreater(a["_run_tss"], 0)
        self.assertEqual(a["_activity_class"], "run")
        self.assertEqual(a["_enriched_v"], ENRICHMENT_VERSION)

    def test_bike_equiv_gets_cross_tss_never_run_tss(self):
        a = enrich(make_bike_equiv(average_heartrate=124.0))
        self.assertEqual(a["_run_tss"], 0.0)
        self.assertGreater(a["_tss"], 0)
        self.assertEqual(a["_pace_zone"], "n/a")
        self.assertEqual(a["_workout_type"], "n/a")
        self.assertAlmostEqual(a["_bike_equiv_mi"], 2.5, places=1)

    def test_bike_equiv_without_hr_uses_duration_estimate(self):
        a = enrich(make_bike_equiv())
        self.assertEqual(a["_run_tss"], 0.0)
        # 25 min at 40 TSS/hr ≈ 16.7 — pace fallback would have given ~35+
        self.assertAlmostEqual(a["_tss"], 16.7, delta=0.5)

    def test_ride_with_hr(self):
        a = enrich(make_activity(type="Ride", distance=0, average_speed=0,
                                 max_speed=0, moving_time=1800,
                                 average_heartrate=120.0))
        self.assertGreater(a["_tss"], 0)
        self.assertEqual(a.get("_run_tss", 0), 0.0)

    def test_invalid_without_hr_gets_zero(self):
        a = enrich(make_activity(distance=0, average_speed=0, max_speed=0,
                                 average_heartrate=None))
        self.assertEqual(a["_run_tss"], 0.0)
        self.assertEqual(a["_tss"], 0.0)

    def test_version_bump_triggers_reenrichment(self):
        self.assertTrue(needs_enrichment({"_enriched_v": 2}))
        self.assertFalse(needs_enrichment({"_enriched_v": ENRICHMENT_VERSION}))


class TestMetricsLoader(unittest.TestCase):
    def test_bike_equiv_excluded_from_run_load(self):
        import metrics
        acts = [
            enrich(make_activity(start_date_local="2026-07-13T20:30:00")),
            enrich(make_activity(start_date_local="2026-07-11T06:49:00")),
            enrich(make_bike_equiv(start_date_local="2026-07-10T14:47:00")),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            with temp_activity_data(Path(tmp), cache_activities=acts):
                runs = metrics.load_activities(activity_type="Run")
                self.assertEqual(len(runs), 2)
                all_runs = metrics.load_activities(activity_type="Run",
                                                   include_bike_equiv=True)
                self.assertEqual(len(all_runs), 3)

    def test_csv_only_rows_are_classified(self):
        import metrics
        acts = [
            make_activity(start_date_local="2026-06-14T12:43:00"),
            make_bike_equiv(start_date_local="2026-06-09T21:22:00"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            with temp_activity_data(Path(tmp), csv_activities=acts):
                runs = metrics.load_activities(activity_type="Run")
                self.assertEqual(len(runs), 1)
                self.assertEqual(runs[0]["_activity_class"], "run")


class TestFitnessSessions(unittest.TestCase):
    def test_fake_run_feeds_cross_stream_not_mileage(self):
        import fitness_tracker
        acts = [
            enrich(make_activity(start_date_local="2026-07-13T20:30:00")),
            enrich(make_bike_equiv(start_date_local="2026-07-10T19:38:00")),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            with temp_activity_data(Path(tmp), cache_activities=acts):
                self.assertEqual(len(fitness_tracker.load_runs()), 1)
                sessions = fitness_tracker.load_sessions()
                kinds = sorted(s["kind"] for s in sessions)
                self.assertEqual(kinds, ["crosstrain", "run"])

    def test_paired_ride_and_bike_equiv_deduped(self):
        import fitness_tracker
        # Same evening: device Ride (with HR) + manual bike-equiv Run.
        ride = make_activity(type="Ride", distance=0, average_speed=0,
                             max_speed=0, moving_time=1638,
                             average_heartrate=121.0,
                             start_date_local="2026-07-14T19:32:51")
        fake = make_bike_equiv(start_date_local="2026-07-14T19:38:21")
        run = make_activity(start_date_local="2026-07-13T20:30:00")
        with tempfile.TemporaryDirectory() as tmp:
            with temp_activity_data(Path(tmp),
                                    cache_activities=[enrich(ride), enrich(fake), enrich(run)]):
                sessions = fitness_tracker.load_sessions()
                cross = [s for s in sessions if s["kind"] == "crosstrain"]
                runs = [s for s in sessions if s["kind"] == "run"]
                self.assertEqual(len(cross), 1)   # pair collapsed
                self.assertEqual(len(runs), 1)
                # The kept record is the one with HR (the 27.3-min device Ride)
                self.assertAlmostEqual(cross[0]["moving_min"], 1638 / 60, places=1)

    def test_separate_same_day_cross_sessions_survive_dedupe(self):
        import fitness_tracker
        morning = make_activity(type="Ride", distance=0, average_speed=0,
                                max_speed=0, moving_time=1800,
                                average_heartrate=110.0,
                                start_date_local="2026-07-10T07:00:00")
        evening = make_activity(type="Ride", distance=0, average_speed=0,
                                max_speed=0, moving_time=2400,
                                average_heartrate=130.0,
                                start_date_local="2026-07-10T21:00:00")
        with tempfile.TemporaryDirectory() as tmp:
            with temp_activity_data(Path(tmp),
                                    cache_activities=[enrich(morning), enrich(evening)]):
                sessions = fitness_tracker.load_sessions()
                cross = [s for s in sessions if s["kind"] == "crosstrain"]
                self.assertEqual(len(cross), 2)  # 14h apart: both kept

    def test_days_since_run_ignores_bike_days(self):
        import fitness_tracker
        from datetime import datetime, timedelta
        # Offset from datetime.now() (not a fixed clock hour) so the exact
        # day count doesn't depend on what time of day the suite happens to
        # run — days_since_run truncates (now - run_dt).days.
        now = datetime.now()
        run_dt = (now - timedelta(days=4)).strftime("%Y-%m-%dT%H:%M:%S")
        bike_dt = (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")
        acts = [
            enrich(make_activity(start_date_local=run_dt, start_date=run_dt + "Z")),
            enrich(make_bike_equiv(start_date_local=bike_dt, start_date=bike_dt + "Z",
                                   average_heartrate=120.0)),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            with temp_activity_data(Path(tmp), cache_activities=acts):
                status = fitness_tracker.current_status()
                self.assertEqual(status["days_since_run"], 4)


if __name__ == "__main__":
    unittest.main()
