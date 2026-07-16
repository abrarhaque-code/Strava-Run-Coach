"""Tests for compliance windows, override semantics, and the reconcile loop."""

import tempfile
import unittest
from datetime import date
from pathlib import Path

from enrichment import enrich
from tests.helpers import (
    make_activity, make_bike_equiv, make_plan, temp_activity_data, temp_plan,
)


def _run_on(day: str, miles: float = 5.0, hour: int = 19):
    dist_m = miles * 1609.34
    start = f"{day}T{hour:02d}:00:00"
    return enrich(make_activity(
        start_date_local=start, start_date=start + "Z",
        distance=dist_m, moving_time=int(miles * 600),  # 10:00/mi
    ))


class TestWeeklyCompliance(unittest.TestCase):
    """Plan: 4 weeks starting Mon 2026-05-18, 20mi target, 8mi LR."""

    def _ctx(self, tmp, cache_acts, state=None):
        self._tp = temp_plan(Path(tmp), plan=make_plan(), state=state)
        self._td = temp_activity_data(Path(tmp), cache_activities=cache_acts)
        self._tp.__enter__()
        self._td.__enter__()

    def _close(self):
        self._td.__exit__(None, None, None)
        self._tp.__exit__(None, None, None)

    def test_window_boundaries_and_totals(self):
        acts = [
            _run_on("2026-05-18", 4),   # Mon wk1 — counted
            _run_on("2026-05-24", 8),   # Sun wk1 — counted (long run)
            _run_on("2026-05-25", 5),   # Mon wk2 — NOT wk1
        ]
        with tempfile.TemporaryDirectory() as tmp:
            self._ctx(tmp, acts)
            try:
                import plan_tracker
                c = plan_tracker.weekly_compliance(1, today=date(2026, 5, 26))
                self.assertEqual(c["miles_actual"], 12.0)
                self.assertEqual(c["run_count"], 2)
                self.assertEqual(c["long_run_actual"], 8.0)
                self.assertTrue(c["long_run_hit"])   # 8 >= 8*0.9
                # Week has ended at 60% — short but not a miss
                self.assertEqual(c["status"], "partial")
            finally:
                self._close()

    def test_bike_equiv_excluded_from_miles_but_credited(self):
        acts = [
            _run_on("2026-05-18", 4),
            enrich(make_bike_equiv(start_date_local="2026-05-19T19:00:00",
                                   start_date="2026-05-19T19:00:00Z",
                                   average_heartrate=124.0)),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            self._ctx(tmp, acts)
            try:
                import plan_tracker
                c = plan_tracker.weekly_compliance(1, today=date(2026, 5, 20))
                self.assertEqual(c["miles_actual"], 4.0)   # fake 2.5mi NOT added
                self.assertEqual(c["run_count"], 1)
                self.assertEqual(c["bike_sessions"], 1)
                self.assertEqual(c["bike_min"], 25)
            finally:
                self._close()

    def test_manual_override_wins_auto_does_not(self):
        acts = [_run_on("2026-05-18", 4)]
        state = {
            "weeks_status": {"1": "complete"},
            "weeks_status_source": {"1": "auto"},
        }
        with tempfile.TemporaryDirectory() as tmp:
            self._ctx(tmp, acts, state=state)
            try:
                import plan_tracker
                # auto entry is recomputed: 4/20 after week end -> missed
                c = plan_tracker.weekly_compliance(1, today=date(2026, 6, 30))
                self.assertEqual(c["status"], "missed")
                # manual entry sticks
                plan_tracker.mark_week_complete(1)
                c2 = plan_tracker.weekly_compliance(1, today=date(2026, 6, 30))
                self.assertEqual(c2["status"], "complete")
            finally:
                self._close()

    def test_slide_offset_shifts_window(self):
        acts = [_run_on("2026-05-25", 6)]  # calendar wk2
        state = {"slide_offset_weeks": 1}
        with tempfile.TemporaryDirectory() as tmp:
            self._ctx(tmp, acts, state=state)
            try:
                import plan_tracker
                # With offset 1, plan wk1's effective window is May 25-31
                c = plan_tracker.weekly_compliance(1, today=date(2026, 5, 26))
                self.assertEqual(c["miles_actual"], 6.0)
            finally:
                self._close()


class TestReconcile(unittest.TestCase):
    def test_reconcile_writes_actuals_and_auto_status(self):
        acts = [
            _run_on("2026-05-18", 10),
            _run_on("2026-05-20", 4),
            _run_on("2026-05-23", 8),
        ]  # wk1: 22mi (>=80%), LR 8 -> complete
        with tempfile.TemporaryDirectory() as tmp:
            with temp_plan(Path(tmp), plan=make_plan()) as mp_mod:
                with temp_activity_data(Path(tmp), cache_activities=acts):
                    import reconcile
                    result = reconcile.reconcile(today=date(2026, 5, 26), verbose=False)
                    self.assertGreaterEqual(result["weeks_reconciled"], 2)
                    state = mp_mod.load_state()
                    wk1 = state["weeks_actuals"]["1"]
                    self.assertEqual(wk1["run_mi"], 22.0)
                    self.assertEqual(wk1["runs"], 3)
                    self.assertEqual(state["weeks_status"]["1"], "complete")
                    self.assertEqual(state["weeks_status_source"]["1"], "auto")
                    # In-progress week 2 gets actuals but no status entry
                    self.assertNotIn("2", state["weeks_status"])

    def test_reconcile_idempotent(self):
        acts = [_run_on("2026-05-18", 10)]
        with tempfile.TemporaryDirectory() as tmp:
            with temp_plan(Path(tmp), plan=make_plan()):
                with temp_activity_data(Path(tmp), cache_activities=acts):
                    import reconcile
                    r1 = reconcile.reconcile(today=date(2026, 5, 26), verbose=False)
                    self.assertTrue(r1["changes"])
                    r2 = reconcile.reconcile(today=date(2026, 5, 26), verbose=False)
                    self.assertEqual(r2["changes"], [])

    def test_zero_data_guard(self):
        acts = [_run_on("2026-05-18", 10)]
        with tempfile.TemporaryDirectory() as tmp:
            with temp_plan(Path(tmp), plan=make_plan()) as mp_mod:
                with temp_activity_data(Path(tmp), cache_activities=acts):
                    import reconcile
                    reconcile.reconcile(today=date(2026, 5, 26), verbose=False)
                # Activity data gone (fresh clone scenario) — actuals survive
                empty_dir = Path(tmp) / "empty"
                empty_dir.mkdir()
                with temp_activity_data(empty_dir, cache_activities=[]):
                    import reconcile
                    reconcile.reconcile(today=date(2026, 5, 26), verbose=False)
                    state = mp_mod.load_state()
                    wk1 = state["weeks_actuals"]["1"]
                    self.assertEqual(wk1["run_mi"], 10.0)  # not zeroed
                    self.assertTrue(wk1.get("data_stale"))

    def test_manual_status_never_overwritten(self):
        acts = [_run_on("2026-05-18", 2)]  # missed week by numbers
        state = {"weeks_status": {"1": "complete"},
                 "weeks_status_source": {"1": "manual"}}
        with tempfile.TemporaryDirectory() as tmp:
            with temp_plan(Path(tmp), plan=make_plan(), state=state) as mp_mod:
                with temp_activity_data(Path(tmp), cache_activities=acts):
                    import reconcile
                    reconcile.reconcile(today=date(2026, 6, 2), verbose=False)
                    st = mp_mod.load_state()
                    self.assertEqual(st["weeks_status"]["1"], "complete")
                    self.assertEqual(st["weeks_status_source"]["1"], "manual")

    def test_legacy_manual_status_with_no_source_key_protected(self):
        """A status written before weeks_status_source existed (no source
        tag at all) must default to manual, same as plan_tracker's own rule,
        so reconcile can't silently reclassify pre-schema-v2 manual marks."""
        acts = [_run_on("2026-05-18", 2)]  # numbers alone say "missed"
        state = {"weeks_status": {"1": "complete"}}  # no weeks_status_source key
        with tempfile.TemporaryDirectory() as tmp:
            with temp_plan(Path(tmp), plan=make_plan(), state=state) as mp_mod:
                with temp_activity_data(Path(tmp), cache_activities=acts):
                    import reconcile
                    reconcile.reconcile(today=date(2026, 6, 2), verbose=False)
                    st = mp_mod.load_state()
                    self.assertEqual(st["weeks_status"]["1"], "complete")

    def test_first_ever_reconcile_still_writes_auto_status(self):
        """The missing-source-defaults-to-manual rule must not block the
        very first auto status write for a week that's never been recorded."""
        acts = [_run_on("2026-05-18", 2)]  # <50% of 20mi target -> missed
        with tempfile.TemporaryDirectory() as tmp:
            with temp_plan(Path(tmp), plan=make_plan()) as mp_mod:
                with temp_activity_data(Path(tmp), cache_activities=acts):
                    import reconcile
                    reconcile.reconcile(today=date(2026, 6, 2), verbose=False)
                    st = mp_mod.load_state()
                    self.assertEqual(st["weeks_status"]["1"], "missed")
                    self.assertEqual(st["weeks_status_source"]["1"], "auto")

    def test_slide_freezes_finalized_week_history(self):
        """Once a week is finalized (complete/partial/missed), slide_plan()
        shifting everyone's effective window must NOT retroactively recompute
        it against the new window — history stays what actually happened."""
        acts = [
            _run_on("2026-05-18", 10), _run_on("2026-05-20", 4), _run_on("2026-05-23", 8),
        ]  # wk1: 22mi, complete
        with tempfile.TemporaryDirectory() as tmp:
            with temp_plan(Path(tmp), plan=make_plan()) as mp_mod:
                with temp_activity_data(Path(tmp), cache_activities=acts):
                    import reconcile
                    import plan_tracker
                    reconcile.reconcile(today=date(2026, 5, 26), verbose=False)
                    before = dict(mp_mod.load_state()["weeks_actuals"]["1"])
                    plan_tracker.slide_plan(1)
                    reconcile.reconcile(today=date(2026, 6, 2), verbose=False)
                    after = mp_mod.load_state()["weeks_actuals"]["1"]
                    self.assertEqual(after["run_mi"], before["run_mi"])
                    self.assertEqual(after["start_date"], before["start_date"])
                    self.assertEqual(after["status"], "complete")

    def test_stale_backfill_cannot_downgrade_finalized_week(self):
        """Calling reconcile with an earlier frozen --date after a week has
        already been finalized must not regress its status to in_progress."""
        acts = [
            _run_on("2026-05-18", 10), _run_on("2026-05-20", 4), _run_on("2026-05-23", 8),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            with temp_plan(Path(tmp), plan=make_plan()) as mp_mod:
                with temp_activity_data(Path(tmp), cache_activities=acts):
                    import reconcile
                    reconcile.reconcile(today=date(2026, 6, 2), verbose=False)  # wk1 finalized
                    reconcile.reconcile(today=date(2026, 5, 19), verbose=False)  # backfill mid-week1
                    st = mp_mod.load_state()
                    self.assertEqual(st["weeks_actuals"]["1"]["status"], "complete")
                    self.assertEqual(st["weeks_status"]["1"], "complete")

    def test_partial_coverage_does_not_zero_uncovered_older_week(self):
        """A load that only covers recent weeks (e.g. a short --backfill)
        must not report a real zero for an older, already-recorded week
        that simply falls outside this pass's coverage."""
        wk1_acts = [_run_on("2026-05-18", 10), _run_on("2026-05-20", 4), _run_on("2026-05-23", 8)]
        with tempfile.TemporaryDirectory() as tmp:
            with temp_plan(Path(tmp), plan=make_plan()) as mp_mod:
                with temp_activity_data(Path(tmp), cache_activities=wk1_acts):
                    import reconcile
                    reconcile.reconcile(today=date(2026, 6, 2), verbose=False)
                # Second pass only loads week-3 data (a short backfill) —
                # week 1 is outside this load's coverage entirely.
                wk3_only = [_run_on("2026-06-05", 5)]
                cov_dir = Path(tmp) / "coverage"
                cov_dir.mkdir()
                with temp_activity_data(cov_dir, cache_activities=wk3_only):
                    import reconcile
                    reconcile.reconcile(today=date(2026, 6, 9), verbose=False)
                    st = mp_mod.load_state()
                    wk1 = st["weeks_actuals"]["1"]
                    self.assertEqual(wk1["run_mi"], 22.0)   # not zeroed
                    self.assertTrue(wk1.get("data_stale"))

    def test_data_stale_clears_when_coverage_returns(self):
        acts = [_run_on("2026-05-18", 10)]
        with tempfile.TemporaryDirectory() as tmp:
            with temp_plan(Path(tmp), plan=make_plan()) as mp_mod:
                with temp_activity_data(Path(tmp), cache_activities=acts):
                    import reconcile
                    reconcile.reconcile(today=date(2026, 5, 26), verbose=False)
                empty_dir = Path(tmp) / "empty"
                empty_dir.mkdir()
                with temp_activity_data(empty_dir, cache_activities=[]):
                    import reconcile
                    reconcile.reconcile(today=date(2026, 5, 26), verbose=False)
                    self.assertTrue(mp_mod.load_state()["weeks_actuals"]["1"].get("data_stale"))
                with temp_activity_data(Path(tmp), cache_activities=acts):
                    import reconcile
                    reconcile.reconcile(today=date(2026, 5, 26), verbose=False)
                    self.assertFalse(mp_mod.load_state()["weeks_actuals"]["1"].get("data_stale", False))


if __name__ == "__main__":
    unittest.main()
