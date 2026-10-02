"""plan_tracker: thresholds from the plan, split long runs, MP laps by band,
and parameterised decision-point metrics."""

import tempfile
import unittest
from datetime import date
from pathlib import Path

import plan_tracker as pt
from enrichment import enrich
from tests.helpers import make_activity, make_plan, temp_activity_data, temp_plan


def _run_on(day: str, miles: float = 5.0, hour: int = 19):
    start = f"{day}T{hour:02d}:00:00"
    return enrich(make_activity(start_date_local=start, start_date=start + "Z",
                                distance=miles * 1609.34, moving_time=int(miles * 600)))


class TestComplianceThresholds(unittest.TestCase):
    """The 80% / 50% / 90% rules come from the plan's _meta, not literals."""

    def test_override_from_plan_meta(self):
        acts = [_run_on("2026-05-18", 9), _run_on("2026-05-23", 8)]   # 17/20 = 85%, LR 8 hit
        plan = make_plan()
        plan["_meta"] = {"compliance_thresholds": {"repeat_below_pct": 0.9}}
        with tempfile.TemporaryDirectory() as tmp:
            with temp_plan(Path(tmp), plan=plan) as mp_mod:
                with temp_activity_data(Path(tmp), cache_activities=acts):
                    th = mp_mod.compliance_thresholds()
                    self.assertEqual(th["repeat_below_pct"], 0.9)
                    self.assertEqual(th["missed_below_pct"], 0.5)   # default kept
                    c = pt.weekly_compliance(1, today=date(2026, 5, 26))
                    self.assertEqual(c["status"], "partial")
        with tempfile.TemporaryDirectory() as tmp:
            with temp_plan(Path(tmp), plan=make_plan()):
                with temp_activity_data(Path(tmp), cache_activities=acts):
                    c = pt.weekly_compliance(1, today=date(2026, 5, 26))
                    self.assertEqual(c["status"], "complete")


class TestSplitLongRun(unittest.TestCase):
    def test_two_runs_on_one_day_count_as_the_long_run(self):
        acts = [_run_on("2026-05-23", 5, hour=7), _run_on("2026-05-23", 3.5, hour=9),
                _run_on("2026-05-19", 6), _run_on("2026-05-21", 6)]
        with tempfile.TemporaryDirectory() as tmp:
            with temp_plan(Path(tmp), plan=make_plan()):
                with temp_activity_data(Path(tmp), cache_activities=acts):
                    c = pt.weekly_compliance(1, today=date(2026, 5, 26))
                    self.assertAlmostEqual(c["long_run_actual"], 8.5)
                    self.assertTrue(c["long_run_hit"])
                    self.assertEqual(c["status"], "complete")


class TestMpLaps(unittest.TestCase):
    """MP miles at pace vs MP miles in the HR band, from mile laps."""

    LIMIT = 9.083          # goal 8:35 + 0:30 tolerance
    BAND = (155, 162)

    @staticmethod
    def _lap(pace_min, hr, dist_mi=1.0, idx=1):
        return {"lap_index": idx, "distance": dist_mi * 1609.34,
                "moving_time": pace_min * 60 * dist_mi, "average_heartrate": hr}

    def _laps(self, spec):
        return [self._lap(p, hr, idx=i + 1) for i, (p, hr) in enumerate(spec)]

    def test_longest_stretch_with_and_without_band(self):
        laps = self._laps([(8.83, 158), (8.83, 158), (8.83, 170), (9.5, 150),
                           (8.83, 160), (8.83, 161)])
        self.assertAlmostEqual(pt._longest_mp_stretch_mi(laps, self.LIMIT), 3.0)
        self.assertAlmostEqual(pt._longest_mp_stretch_mi(laps, self.LIMIT, self.BAND), 2.0)

    def test_summary_separates_pace_from_band(self):
        laps = self._laps([(8.83, 158), (8.83, 158), (8.83, 170), (9.5, 150),
                           (8.83, 160), (8.83, 161), (8.9, None)])
        s = pt.mp_lap_summary(laps, self.LIMIT, self.BAND)
        self.assertAlmostEqual(s["at_pace_mi"], 6.0)
        self.assertAlmostEqual(s["in_band_mi"], 4.0)
        self.assertAlmostEqual(s["over_band_mi"], 1.0)
        self.assertAlmostEqual(s["no_hr_mi"], 1.0)
        self.assertAlmostEqual(s["longest_at_pace_mi"], 3.0)
        self.assertAlmostEqual(s["longest_in_band_mi"], 2.0)
        self.assertEqual([x["hr"] for x in s["laps"]],
                         ["in_band", "in_band", "over", "in_band", "in_band", "no_hr"])

    def test_short_and_missing_laps_never_count(self):
        laps = [self._lap(8.5, 158, dist_mi=0.1), {"lap_index": 2, "distance": 0}]
        self.assertEqual(pt._longest_mp_stretch_mi(laps, self.LIMIT), 0.0)
        s = pt.mp_lap_summary(laps, self.LIMIT, self.BAND)
        self.assertEqual(s["at_pace_mi"], 0.0)
        self.assertEqual(s["laps"], [])

    def test_mp_pace_limit_from_the_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            with temp_plan(Path(tmp), plan=make_plan()):
                self.assertAlmostEqual(pt._mp_pace_limit(), 8.583 + pt.MP_PACE_TOLERANCE_MIN)


class TestParameterisedMetrics(unittest.TestCase):
    def test_weeks_with_min_runs_reads_the_criterion(self):
        # three runs in each of the last two full weeks before 'today' (a Tuesday)
        acts = [_run_on(d) for d in ("2026-06-01", "2026-06-03", "2026-06-05",
                                     "2026-06-08", "2026-06-10", "2026-06-12")]
        with tempfile.TemporaryDirectory() as tmp:
            with temp_plan(Path(tmp), plan=make_plan()):
                with temp_activity_data(Path(tmp), cache_activities=acts):
                    today = date(2026, 6, 16)
                    self.assertEqual(pt.metric_value("weeks_with_min_runs_of_4", today,
                                                     {"min_runs": 3}), 2)
                    self.assertEqual(pt.metric_value("weeks_with_min_runs_of_4", today,
                                                     {"min_runs": 4}), 0)
                    self.assertEqual(pt.metric_value("weeks_at_4plus_of_4", today), 0)

    def test_cli_without_a_plan_prints_a_hint(self):
        import contextlib
        import io
        import marathon_plan as mp
        with tempfile.TemporaryDirectory() as tmp:
            old = mp.PLAN_PATH
            mp.PLAN_PATH = Path(tmp) / "none.json"
            mp.load_plan.cache_clear()
            try:
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    pt._cli()
                self.assertIn("coach.py plan", buf.getvalue())
            finally:
                mp.PLAN_PATH = old
                mp.load_plan.cache_clear()


if __name__ == "__main__":
    unittest.main()
