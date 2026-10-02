"""race_predictor: real runs only, rep sessions out of the whole-run VDOT scan,
tips from the athlete's config and plan in their own unit."""

import contextlib
import io
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import config
import race_predictor as rp
from enrichment import enrich
from tests.helpers import make_activity, make_bike_equiv, temp_activity_data, temp_config, temp_plan

MI = 1609.34


def _run(day: str, miles: float, pace_min: float, name: str = "Run", **over) -> dict:
    start = f"{day}T07:00:00"
    kw = dict(name=name, start_date_local=start, start_date=start + "Z",
              distance=miles * MI, moving_time=int(miles * pace_min * 60),
              elapsed_time=int(miles * pace_min * 60) + 60)
    kw.update(over)
    return enrich(make_activity(**kw))


class TestLoader(unittest.TestCase):
    def test_real_runs_only_and_rep_sessions_flagged(self):
        acts = [_run("2026-09-20", 6, 10.0),
                enrich(make_bike_equiv(start_date_local="2026-09-21T07:00:00",
                                       start_date="2026-09-21T07:00:00Z")),
                _run("2026-09-22", 4.97, 7.5, name="8x800", elapsed_time=3600)]
        rows = rp.load_activities(acts)
        self.assertEqual([r["name"] for r in rows], ["Run", "8x800"])
        self.assertEqual([r["is_intervals"] for r in rows], [False, True])
        self.assertAlmostEqual(rows[0]["pace_min_mi"], 10.0, places=2)
        self.assertAlmostEqual(rows[0]["distance_mi"], 6.0, places=2)

    def test_rep_session_does_not_set_the_whole_run_vdot(self):
        acts = [_run("2026-09-20", 6, 10.0),
                _run("2026-09-22", 4.97, 7.0, name="8x800", elapsed_time=3600)]
        with tempfile.TemporaryDirectory() as tmp:
            with temp_config(Path(tmp), {"race_history": []}), temp_activity_data(Path(tmp)):
                v, src = rp.current_fitness_vdot(rp.load_activities(acts), today=datetime(2026, 9, 25))
        self.assertEqual(src["note"], "whole run")
        self.assertAlmostEqual(src["distance_mi"], 6.0, places=1)
        self.assertLess(v, rp.compute_vdot(4.97 * MI, 4.97 * 7 * 60))

    def test_loads_from_the_cache_when_no_list_is_given(self):
        acts = [_run("2026-09-20", 6, 10.0)]
        with tempfile.TemporaryDirectory() as tmp:
            with temp_activity_data(Path(tmp), cache_activities=acts):
                rows = rp.load_activities()
        self.assertEqual(len(rows), 1)


class TestTips(unittest.TestCase):
    RACE = {"distance_mi": 26.2, "goal_pace_min_per_mi": 8.583, "goal_time": "3:45:00"}

    def test_tips_read_config_and_the_plan_week(self):
        tips = "\n".join(rp._tips(-2.0, 10.0, self.RACE))
        self.assertIn(f"HR >= {config.threshold_hr()}", tips)
        self.assertIn(f"{config.plan_cfg()['days_per_week']}+ runs/week", tips)
        self.assertIn("Volume is light (10 mpw)", tips)
        self.assertIn("13.0 mi+ this weekend", tips)             # half the race, no plan
        week = {"long_run_target": 14, "target_miles": 40}
        tips2 = "\n".join(rp._tips(-0.5, 30.0, self.RACE, week=week))
        self.assertIn("Long run: 14.0 mi at 10:00-10:30/mi", tips2)
        self.assertIn("tempo session (3-4 mi @ 8:50-9:10/mi)", tips2)
        self.assertNotIn("Volume is light", tips2)               # 30 >= 0.6 * 40
        tips3 = "\n".join(rp._tips(1.0, 30.0, self.RACE))
        self.assertIn("3 x 1 mi @ 8:35/mi", tips3)

    def test_metric_tips(self):
        with tempfile.TemporaryDirectory() as tmp:
            with temp_config(Path(tmp), {"athlete": {"units": "km"}}):
                tips = "\n".join(rp._tips(-0.5, 10.0, self.RACE) + rp._tips(1.0, 10.0, self.RACE))
        self.assertIn("/km", tips)
        self.assertNotIn("/mi", tips)
        self.assertIn("3 x 1 km @ 5:20/km", tips)
        self.assertIn("km/wk", tips)
        self.assertIn("5-6 km @", tips)


class TestForecastOutput(unittest.TestCase):
    def test_metric_forecast_carries_no_mile_labels(self):
        acts = [_run(f"2026-09-{d:02d}", 5, 9.5) for d in (10, 13, 16, 19, 22, 25)]
        with tempfile.TemporaryDirectory() as tmp:
            with temp_config(Path(tmp), {"athlete": {"units": "km"}, "race_history": []}), \
                    temp_plan(Path(tmp)), temp_activity_data(Path(tmp), cache_activities=acts):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    rp.print_race_forecast()
        text = out.getvalue()
        self.assertIn("RACE FORECAST", text)
        self.assertIn("/km", text)
        self.assertIn("km/wk", text)
        self.assertNotIn("/mi", text)
        self.assertNotIn("mpw", text)


if __name__ == "__main__":
    unittest.main()
