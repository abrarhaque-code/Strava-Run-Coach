"""Tests for Jack Daniels VDOT math in race_predictor."""

import unittest

from race_predictor import compute_vdot, predict_race_time


class TestVdot(unittest.TestCase):
    def test_5k_20min_is_about_50(self):
        # A 20:00 5K is a well-known ~VDOT 50 benchmark.
        v = compute_vdot(5000, 20 * 60)
        self.assertAlmostEqual(v, 50.0, delta=2.0)

    def test_zero_inputs_are_zero(self):
        self.assertEqual(compute_vdot(0, 1200), 0.0)
        self.assertEqual(compute_vdot(5000, 0), 0.0)

    def test_faster_time_higher_vdot(self):
        # Same distance, faster time -> higher VDOT.
        slow = compute_vdot(10000, 55 * 60)
        fast = compute_vdot(10000, 45 * 60)
        self.assertGreater(fast, slow)

    def test_predict_race_time_roundtrip(self):
        # predict_race_time inverts compute_vdot; round-tripping should return
        # the same VDOT to within a small tolerance.
        for v in (38.0, 45.0, 52.0):
            t = predict_race_time(v, 10000)
            self.assertGreater(t, 0)
            recovered = compute_vdot(10000, t)
            self.assertAlmostEqual(recovered, v, delta=0.5)

    def test_predict_race_time_longer_is_slower(self):
        # At a fixed VDOT, a longer race takes more time.
        v = 45.0
        t_10k = predict_race_time(v, 10000)
        t_half = predict_race_time(v, 21097.5)
        self.assertGreater(t_half, t_10k)


if __name__ == "__main__":
    unittest.main()
