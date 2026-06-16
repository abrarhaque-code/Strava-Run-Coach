"""Tests for the base-build scenario projector (ramp + marathon projection)."""

import unittest

import scenario
from scenario import (
    build_ramp,
    project_marathon,
    feasibility,
    aerobic_gain_bounds,
    exec_penalty_bounds,
)

C = dict(scenario.DEFAULTS)


class TestRamp(unittest.TestCase):
    def test_length_matches_block(self):
        ramp = build_ramp(25, C)
        self.assertEqual(len(ramp["weeks"]), C["block_weeks"])

    def test_peak_scales_with_entry(self):
        self.assertGreater(build_ramp(30, C)["peak_mi"], build_ramp(20, C)["peak_mi"])

    def test_peak_is_a_sane_multiple_of_entry(self):
        ramp = build_ramp(20, C)
        self.assertGreater(ramp["peak_mi"], 20)
        self.assertLess(ramp["peak_mi"], 20 * 2.0)  # not runaway

    def test_long_run_capped(self):
        ramp = build_ramp(40, C)  # high entry would imply a huge long run
        self.assertLessEqual(ramp["long_run_peak"], C["long_run_cap_mi"] + 0.001)

    def test_taper_at_end_and_descending(self):
        ramp = build_ramp(25, C)
        taper = ramp["weeks"][-C["taper_weeks"]:]
        self.assertTrue(all(w["phase"] == "taper" for w in taper))
        targets = [w["target_miles"] for w in taper]
        self.assertEqual(targets, sorted(targets, reverse=True))  # comes down into race

    def test_no_unsafe_weekly_jump(self):
        ramp = build_ramp(20, C)
        t = [w["target_miles"] for w in ramp["weeks"]]
        # Allow a small slack for 0.1mi rounding of the published targets.
        tol = C["max_weekly_jump"] + 0.03
        for prev, cur in zip(t, t[1:]):
            if cur > prev:
                self.assertLessEqual((cur - prev) / prev, tol)


class TestProjection(unittest.TestCase):
    def test_range_ordered(self):
        ramp = build_ramp(25, C)
        proj = project_marathon(38.7, ramp, 12.0, C)
        self.assertLess(proj["fast_sec"], proj["slow_sec"])

    def test_more_mileage_not_slower(self):
        anchor, cur = 38.7, 12.0
        p20 = project_marathon(anchor, build_ramp(20, C), cur, C)
        p30 = project_marathon(anchor, build_ramp(30, C), cur, C)
        self.assertLessEqual(p30["fast_sec"], p20["fast_sec"])
        self.assertLessEqual(p30["slow_sec"], p20["slow_sec"])

    def test_aerobic_gain_bounds_ordered_and_monotone(self):
        lo1, hi1 = aerobic_gain_bounds(25, 12, C)
        lo2, hi2 = aerobic_gain_bounds(40, 12, C)
        self.assertLessEqual(lo1, hi1)
        self.assertGreater(hi2, hi1)  # more avg mileage -> more projected gain
        self.assertLessEqual(hi2, C["aerobic_gain_max"] + 1e-6)

    def test_penalty_shrinks_with_durability(self):
        best_lo, worst_lo = exec_penalty_bounds(12, C)   # low durability
        best_hi, worst_hi = exec_penalty_bounds(20, C)   # high durability
        self.assertLessEqual(best_lo, worst_lo)
        self.assertLess(worst_hi, worst_lo)
        self.assertGreaterEqual(worst_hi, 1.0)


class TestFeasibility(unittest.TestCase):
    def test_higher_entry_needs_higher_rate(self):
        f20 = feasibility(12, 20, 3.7, C)
        f30 = feasibility(12, 30, 3.7, C)
        self.assertGreater(f30["required_weekly_rate"], f20["required_weekly_rate"])

    def test_labels(self):
        # Reaching current mileage needs ~0% growth -> comfortable.
        self.assertEqual(feasibility(20, 20, 4, C)["label"], "comfortable")
        # Doubling on a short runway -> unsafe.
        self.assertEqual(feasibility(12, 30, 3.7, C)["label"], "unsafe")


if __name__ == "__main__":
    unittest.main()
