"""Wrist-HR checks run the same way for a low reading as for a high one."""

import unittest

import hr_quality


def _stream(segments):
    """segments: [(seconds, hr, spm or None, moving)], 5-s samples; a segment
    with moving=None is a recording gap of that length."""
    t, hr, cad, mv = [], [], [], []
    ts = 0
    for secs, h, spm, moving in segments:
        if moving is None:
            ts += secs
            continue
        for _ in range(secs // 5):
            t.append(ts)
            hr.append(h(ts) if callable(h) else h)
            cad.append((spm(ts) if callable(spm) else spm) / 2 if spm else None)
            mv.append(moving)
            ts += 5
    out = {"time": t, "heartrate": hr, "moving": mv}
    if any(c is not None for c in cad):
        out["cadence"] = [c or 0 for c in cad]
    return out


class TestHrQuality(unittest.TestCase):
    def test_too_short_is_unavailable(self):
        q = hr_quality.hr_quality({"time": [0, 5], "heartrate": [120, 121]})
        self.assertFalse(q["available"])
        self.assertFalse(q["unreliable"])

    def test_slow_return_after_a_stop_is_noted_and_kept_out_of_lap_hr(self):
        def ramp(ts):
            return min(133, 60 + (ts - 1140) * 0.5) if ts >= 1140 else 133
        s = _stream([(900, 133, None, True), (240, 0, None, None), (600, ramp, None, True)])
        laps = [{"idx": 1, "elapsed_s": 900, "moving_s": 900},
                {"idx": 2, "elapsed_s": 840, "moving_s": 600}]
        q = hr_quality.hr_quality(s, laps)
        self.assertEqual(len(q["slow_reacq"]), 1)
        raw = sum(ramp(t) for t in range(1140, 1740, 5)) / 120
        self.assertGreater(q["lap_hr"][2], raw)          # the ramp is out of lap 2's HR
        self.assertFalse(q["unreliable"])                 # one slow return is not a verdict

    def test_two_slow_returns_flag_the_trace(self):
        def ramp(base):
            return lambda ts: min(133, 60 + (ts - base) * 0.3) if ts >= base else 133
        s = _stream([(900, 133, None, True), (240, 0, None, None), (600, ramp(1140), None, True),
                     (240, 0, None, None), (600, ramp(1980), None, True)])
        q = hr_quality.hr_quality(s)
        self.assertEqual(len(q["slow_reacq"]), 2)
        self.assertTrue(q["unreliable"])
        self.assertIn("unreliable", q["flag"])

    def test_coincidental_cadence_match_is_not_a_lock(self):
        s = _stream([(1200, lambda ts: 160 + (ts % 60 > 30), 161, True)])
        q = hr_quality.hr_quality(s)
        self.assertFalse(q["lock"]["flagged"])
        self.assertFalse(q["unreliable"])

    def test_co_moving_cadence_is_flagged(self):
        def spm(ts):
            return 162 + 4 * ((ts // 60) % 2)
        s = _stream([(1200, lambda ts: spm(ts) + 1, spm, True)])
        q = hr_quality.hr_quality(s)
        self.assertTrue(q["lock"]["flagged"])
        self.assertTrue(any("cadence lock" in n for n in q["notes"]))
        self.assertTrue(q["unreliable"])
        self.assertTrue(q["flag"].startswith("Wrist HR looks unreliable"))

    def test_clean_trace_has_no_notes(self):
        s = _stream([(1800, 140, 158, True)])
        q = hr_quality.hr_quality(s)
        self.assertEqual(q["notes"], [])
        self.assertFalse(q["unreliable"])
        self.assertIsNone(q["flag"])


if __name__ == "__main__":
    unittest.main()
