"""Rep detection. The fixture is a real 8 x 800 that fell apart at the end.

That session is the reason this module exists: laps were auto mile splits, the
recovery-diluted average HR classified it EASY, and the lap reads congratulated
the athlete on a "negative split" in a session he lost the last two reps of.
Every assertion here is a fact established by hand-segmenting that stream.
"""

import unittest

import intervals


def synth_session(rep_times, rep_dist=800.0, recovery_s=90, warmup_s=360,
                  warmup_v=2.9, hr_base=110, hr_peak=150, cadence=82):
    """Build a stream shaped like a track session with standing recoveries.

    Standing recovery is modelled the way a real watch records it: the samples
    simply are not there, leaving a time gap. That is the primary signal
    `segment_stream` keys off.
    """
    t, dist, vel, hr, cad, moving = [], [], [], [], [], []
    clock = 0
    total = 0.0

    def emit(secs, v, hr_from, hr_to):
        nonlocal clock, total
        for i in range(secs):
            t.append(clock)
            total += v
            dist.append(total)
            vel.append(v)
            hr.append(int(hr_from + (hr_to - hr_from) * (i / max(1, secs - 1))))
            cad.append(cadence // 2)
            moving.append(True)
            clock += 1

    emit(warmup_s, warmup_v, 90, 128)
    for n, rt in enumerate(rep_times):
        clock += recovery_s              # the gap IS the recovery
        v = rep_dist / rt
        emit(int(rt), v, hr_base + n * 3, hr_peak + n * 2)
    return {"time": t, "distance": dist, "velocity_smooth": vel,
            "heart_rate": hr, "cadence": cad, "moving": moving}


class TestPrescriptionParsing(unittest.TestCase):
    def test_bandit_phrasing(self):
        self.assertEqual(
            intervals.parse_prescription("8 reps of 800m, starting at Tempo pace"),
            [(8, 800.0)])

    def test_athlete_shorthand(self):
        for text, want in (("8x800", [(8, 800.0)]),
                           ("8 x 800m", [(8, 800.0)]),
                           ("16 x 200", [(16, 200.0)]),
                           ("6 x 1k", [(6, 1000.0)])):
            self.assertEqual(intervals.parse_prescription(text), want, text)

    def test_mile_reps(self):
        self.assertEqual(intervals.parse_prescription("4 reps of 1 mile at Tempo"),
                         [(4, 1609.34)])

    def test_range_count_takes_the_low_end(self):
        # "8-10 reps of 600m" (Bandit W05). Committing to the low end keeps a
        # completed session from being scored as a miss.
        self.assertEqual(intervals.parse_prescription("8-10 reps of 600m"),
                         [(8, 600.0)])

    def test_compound_session_yields_each_block(self):
        blocks = intervals.parse_prescription(
            "8 reps of 200m at Interval 1 pace, followed by 1 mile at Tempo "
            "pace, followed by another 8 reps of 200m")
        self.assertEqual(blocks, [(8, 200.0), (8, 200.0)])

    def test_primary_block_is_the_biggest(self):
        blocks = [(2, 200.0), (4, 800.0)]
        self.assertEqual(intervals.primary_block(blocks), (4, 800.0))

    def test_easy_run_has_no_reps(self):
        self.assertEqual(intervals.parse_prescription("45 mins easy"), [])
        self.assertEqual(intervals.parse_prescription(""), [])


class TestStreamNormalisation(unittest.TestCase):
    def test_mcp_dialect(self):
        s = intervals.normalize_streams({"heart_rate": [1, 2], "time": [0, 1]})
        self.assertEqual(s["heartrate"], [1, 2])

    def test_rest_dialect(self):
        s = intervals.normalize_streams({"heartrate": {"data": [1, 2]},
                                         "time": {"data": [0, 1]}})
        self.assertEqual(s["heartrate"], [1, 2])
        self.assertEqual(s["time"], [0, 1])

    def test_missing_streams_are_absent_not_zeroed(self):
        s = intervals.normalize_streams({"time": [0, 1]})
        self.assertNotIn("heartrate", s)

    def test_junk_input(self):
        self.assertEqual(intervals.normalize_streams(None), {})


class TestRepDetection(unittest.TestCase):
    def test_finds_eight_clean_reps(self):
        streams = synth_session([230] * 8)
        rs = intervals.detect_reps(intervals.normalize_streams(streams),
                                   prescription="8 x 800m")
        self.assertEqual(rs.completed, 8)
        self.assertEqual(rs.target_m, 800.0)

    def test_recovers_an_abandoned_final_rep(self):
        # The Aug 19 shape: six on pace, one slow, one bailed at ~59%.
        streams = synth_session([230, 232, 229, 220, 221, 222, 234, 254])
        rs = intervals.detect_reps(intervals.normalize_streams(streams),
                                   prescription="8 x 800m")
        self.assertEqual(rs.completed, 8)

    def test_warmup_is_not_a_rep(self):
        streams = synth_session([230] * 8)
        rs = intervals.detect_reps(intervals.normalize_streams(streams),
                                   prescription="8 x 800m")
        self.assertIsNotNone(rs.warmup)
        # The warm-up ran 6 min at 2.9 m/s — far longer than any 800.
        self.assertGreater(rs.warmup.distance_m, 900)

    def test_steady_easy_run_has_no_reps(self):
        streams = synth_session([], warmup_s=1800, warmup_v=2.8)
        rs = intervals.detect_reps(intervals.normalize_streams(streams))
        self.assertEqual(rs.completed, 0)

    def test_normalisation_makes_gps_slop_comparable(self):
        # Two reps run at the same speed but measured 800m and 760m must
        # normalise to the same time; raw duration would differ by 5%.
        rs = intervals.RepSet(target_m=800.0, reps=[
            intervals.Segment(0, 230, 800.0, 230),
            intervals.Segment(400, 619, 760.0, 219),
        ])
        self.assertAlmostEqual(rs.norm_s(rs.reps[0]), rs.norm_s(rs.reps[1]),
                               delta=1.0)

    def test_no_prescription_infers_the_rep_distance(self):
        streams = synth_session([230] * 6)
        rs = intervals.detect_reps(intervals.normalize_streams(streams))
        self.assertGreaterEqual(rs.completed, 5)
        self.assertAlmostEqual(rs.target_m, 800.0, delta=60)


class TestAnalysis(unittest.TestCase):
    def _aug19(self):
        streams = synth_session([230, 232, 229, 220, 221, 222, 234, 254])
        return intervals.detect_reps(intervals.normalize_streams(streams),
                                     prescription="8 x 800m")

    def test_fade_is_detected(self):
        f = intervals.fade(self._aug19())
        self.assertIsNotNone(f)
        self.assertGreater(f["delta_s"], 10)

    def test_short_rep_flagged(self):
        rs = intervals.RepSet(target_m=800.0, prescribed_reps=8, reps=[
            intervals.Segment(0, 230, 800.0, 230),
            intervals.Segment(400, 630, 472.0, 150),
        ])
        self.assertEqual(intervals.short_reps(rs), [2])

    def test_completed_full_excludes_a_bailed_rep(self):
        rs = intervals.RepSet(target_m=800.0, prescribed_reps=2, reps=[
            intervals.Segment(0, 230, 800.0, 230),
            intervals.Segment(400, 630, 472.0, 150),
        ])
        c = intervals.compliance(rs)
        self.assertEqual(c["completed"], 2)
        self.assertEqual(c["completed_full"], 1)

    def test_work_pct_reports_the_session_not_the_mileage(self):
        rs = intervals.RepSet(target_m=800.0, prescribed_reps=8, reps=[
            intervals.Segment(i * 400, i * 400 + 230, 800.0, 230)
            for i in range(6)])
        c = intervals.compliance(rs)
        self.assertAlmostEqual(c["work_pct"], 75.0, places=1)

    def test_pace_matched_pair_ignores_mismatched_recoveries(self):
        # Rep 1 after a 150s stand vs rep 2 after 90s: the rest is not
        # comparable, so no start-HR delta should be claimed.
        rs = intervals.RepSet(target_m=800.0, reps=[
            intervals.Segment(0, 230, 800.0, 230, hr_start=101, hr_end=144,
                              recovery_s=150),
            intervals.Segment(400, 630, 800.0, 230, hr_start=127, hr_end=156,
                              recovery_s=90),
        ])
        pm = intervals.pace_matched_pair(rs)
        self.assertIsNotNone(pm)
        self.assertIsNone(pm["hr_start_delta"])
        self.assertEqual(pm["hr_end_delta"], 12)

    def test_pace_matched_pair_reports_delta_when_rests_agree(self):
        rs = intervals.RepSet(target_m=800.0, reps=[
            intervals.Segment(0, 230, 800.0, 230, hr_start=110, hr_end=147,
                              recovery_s=90),
            intervals.Segment(400, 630, 800.0, 230, hr_start=127, hr_end=156,
                              recovery_s=92),
        ])
        pm = intervals.pace_matched_pair(rs)
        self.assertEqual(pm["hr_start_delta"], 17)

    def test_report_is_empty_without_reps(self):
        self.assertEqual(intervals.format_report(intervals.RepSet()), [])

    def test_target_labels(self):
        self.assertEqual(intervals.target_label(800), "800m")
        self.assertEqual(intervals.target_label(1000), "1k")
        self.assertEqual(intervals.target_label(1609.34), "mile")




class TestWrappedStreams(unittest.TestCase):
    def test_merged_activity_wrapper_is_unwrapped(self):
        s = intervals.normalize_streams({"streams": {"heart_rate": [1, 2], "time": [0, 1]}})
        self.assertEqual(s["heartrate"], [1, 2])

    def test_fmt_pace_uses_units(self):
        self.assertEqual(intervals.fmt_pace(0), "--")
        self.assertTrue(intervals.fmt_pace(540).startswith("9:00"))


if __name__ == "__main__":
    unittest.main()
