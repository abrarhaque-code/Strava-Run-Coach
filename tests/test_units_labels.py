"""Every printed distance, pace and volume follows athlete.units: the metric
runner never sees '/mi' or 'mpw', and the Eddington number counts in km."""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

import metrics
import scenario
import trends
from enrichment import enrich
from tests.helpers import make_activity, temp_activity_data, temp_config, temp_plan

MI = 1609.34


def _run(day: str, miles: float, pace_min: float = 10.0, hr: float = 142.0, **over) -> dict:
    start = f"{day}T07:00:00"
    kw = dict(start_date_local=start, start_date=start + "Z", distance=miles * MI,
              moving_time=int(miles * pace_min * 60), elapsed_time=int(miles * pace_min * 60) + 30,
              average_heartrate=hr, max_heartrate=hr + 12)
    kw.update(over)
    return enrich(make_activity(**kw))


@contextlib.contextmanager
def _metric_world(tmp, acts):
    with temp_config(Path(tmp), {"athlete": {"units": "km"}, "race_history": []}), \
            temp_plan(Path(tmp)), temp_activity_data(Path(tmp), cache_activities=acts):
        yield


class TestEddingtonUnits(unittest.TestCase):
    def test_counts_in_the_athletes_unit(self):
        acts = [_run(f"2026-09-{d:02d}", 8 / 1.609344) for d in range(1, 8)]   # seven 8 km runs
        self.assertEqual(metrics.eddington_number(acts), 4)                   # 4 runs of 4+ mi
        with tempfile.TemporaryDirectory() as tmp:
            with temp_config(Path(tmp), {"athlete": {"units": "km"}}):
                self.assertEqual(metrics.eddington_number(acts), 7)           # 7 runs of 7+ km
                ep = metrics.eddington_progress(acts)
        # every 8 km run already clears the 8 km bar; E8 needs one more of them
        self.assertEqual((ep["current"], ep["next_n"], ep["runs_at_or_above_next"],
                          ep["runs_needed_for_next"]), (7, 8, 7, 1))


class TestMetricPrints(unittest.TestCase):
    ACTS = [_run(f"2026-09-{d:02d}", 5 + (d % 3), 9.8) for d in (2, 5, 8, 11, 14, 17, 20, 23, 26)]

    def _capture(self, fn, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            fn(*args)
        return out.getvalue()

    def test_metrics_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _metric_world(tmp, self.ACTS):
                text = self._capture(metrics.print_summary)
        self.assertIn("/km", text)
        self.assertIn("runs of", text)
        self.assertIn("+ km", text)
        self.assertNotIn("/mi", text)
        self.assertNotIn(" mi |", text)

    def test_trends(self):
        acts = self.ACTS + [_run("2026-08-20", 12, 10.2, hr=150), _run("2026-08-06", 11, 10.3, hr=149)]
        with tempfile.TemporaryDirectory() as tmp:
            with _metric_world(tmp, acts):
                runs = trends.load_trend_runs()
                text = self._capture(trends.print_trends, runs)
        self.assertIn("/km", text)
        self.assertIn("per km", text)
        self.assertNotIn("/mi", text)

    def test_scenarios(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _metric_world(tmp, self.ACTS):
                text = self._capture(scenario.print_scenarios, [40.0])
        self.assertIn("km/wk", text)
        self.assertIn("1.6 km", text)                 # the bike-equivalence line
        self.assertNotIn("mpw", text)
        self.assertNotIn("/mi", text)


if __name__ == "__main__":
    unittest.main()
