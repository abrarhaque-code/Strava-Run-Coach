"""The single-run review, on a long run that fooled the old one.

A 17.4 mi long run with 8 @ MP prescribed. Read by averages it looked even
and the fast miles looked like a deliberate tired-legs finish; read lap by
lap it was a marathon-pace block run above the HR band, then a fade, with
75 minutes of stops. The laps below are that run's real mile splits; the
stream is synthetic but shaped like a real one (5-second spacing, a
recording gap, a run of still samples).
"""

import contextlib
import io
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import config
import intervals
import plan_tracker
import post_run_review as prr
from enrichment import enrich
from tests.helpers import make_activity, make_plan, temp_activity_data, temp_config, temp_plan

MI = 1609.34

# (moving_s, elapsed_s, avg_hr, max_hr, elev_gain_m) for each mile lap, real values.
LR_MILES = [
    (587, 2321, 131.8, 145, 7.0),     # to the start; 29 min waiting for the group
    (580, 594, 137.2, 145, 13.8),
    (568, 660, 138.9, 153, 8.2),
    (558, 914, 139.6, 151, 4.2),
    (527, 527, 153.1, 163, 10.8),
    (525, 525, 157.7, 162, 0.0),
    (554, 554, 155.1, 162, 0.0),
    (517, 517, 164.7, 171, 3.4),
    (527, 527, 169.7, 175, 3.8),
    (557, 875, 161.4, 173, 4.8),      # water stop
    (517, 517, 173.7, 178, 3.6),
    (531, 612, 174.4, 179, 0.2),
    (581, 581, 171.8, 178, 0.0),
    (591, 1967, 155.4, 177, 42.0),    # a long stop + a bridge climb
    (530, 642, 166.0, 180, 0.0),
    (582, 734, 163.2, 179, 2.2),
    (625, 944, 151.3, 162, 3.2),
]
LR_TAIL = (0.41, 267, 267, 162.6, 176, 4.2)

LR_ACTIVITY = {
    "id": 777001, "name": "Morning Run", "start_date_local": "2026-09-12T07:35:05",
    "distance": 28019.7, "moving_time": 9723, "elapsed_time": 14278,
    "average_heartrate": 156.569, "max_heartrate": 180,
}
# The plan week, with the marathon-pace block as a structured quality entry.
WK17 = {
    "week_num": 17, "phase": "base", "start_date": "2026-09-07", "target_miles": 48,
    "long_run_target": 18, "long_run_time_cap_min": 175,
    "key_workout": "Long run 18 w/ 8 @ MP by HR. The block is the point; the rest is easy.",
    "quality": {"kind": "mp", "miles": 8},
}
BANDS = [("recovery", 0, 130), ("easy", 130, 148), ("steady", 148, 155), ("mp", 155, 162),
         ("threshold", 162, 172), ("vo2", 172, 999)]


def long_run_laps() -> list:
    laps = []
    for i, (mv, el, hr, mx, gain) in enumerate(LR_MILES):
        laps.append({"lap_index": i + 1, "distance": MI, "moving_time": mv,
                     "elapsed_time": el, "average_heartrate": hr, "max_heartrate": mx,
                     "total_elevation_gain": gain})
    d, mv, el, hr, mx, gain = LR_TAIL
    laps.append({"lap_index": len(laps) + 1, "distance": d * MI, "moving_time": mv,
                 "elapsed_time": el, "average_heartrate": hr, "max_heartrate": mx,
                 "total_elevation_gain": gain})
    return laps


def mile_laps(spec: list, dist_m: float = MI) -> list:
    """[(pace_min, hr), ...] -> continuous full laps (elapsed = moving + 3 s)."""
    out = []
    for i, (pace, hr) in enumerate(spec):
        mv = int(round(pace * 60 * dist_m / MI))
        out.append({"lap_index": i + 1, "distance": dist_m, "moving_time": mv,
                    "elapsed_time": mv + 3, "average_heartrate": hr})
    return out


def run_from_laps(laps: list, **over) -> dict:
    dist = sum(lp["distance"] for lp in laps)
    mv = sum(lp["moving_time"] for lp in laps)
    el = sum(lp.get("elapsed_time") or lp["moving_time"] for lp in laps)
    hrs = [lp["average_heartrate"] for lp in laps if lp.get("average_heartrate")]
    a = {"id": 777002, "name": "Run", "start_date_local": "2026-05-23T07:00:00",
         "distance": dist, "moving_time": mv, "elapsed_time": el,
         "average_heartrate": (sum(hrs) / len(hrs)) if hrs else None,
         "max_heartrate": (max(hrs) + 8) if hrs else None}
    a.update(over)
    return a


def synthetic_stream(with_moving: bool = True) -> dict:
    """5-s samples: 10 min @140, a 300 s recording gap, 10 min @160, 90 s
    standing (moving=False, HR 150), 10 min @170, 10 min @175. Flat 3 m/s."""
    t, hr, mv, dist = [], [], [], []
    d = 0.0

    def add(ts, h, moving, step_m):
        nonlocal d
        d += step_m
        t.append(ts)
        hr.append(h)
        mv.append(moving)
        dist.append(d)

    ts = 0
    for _ in range(120):                        # A: 0..595
        add(ts, 140, True, 15.0)
        ts += 5
    ts += 300 - 5                               # gap: 595 -> 895
    for _ in range(120):                        # B: 895..1490
        add(ts, 160, True, 15.0)
        ts += 5
    for _ in range(18):                         # still: 1495..1580
        add(ts, 150, False, 0.0)
        ts += 5
    for _ in range(120):                        # C: 1585..2180
        add(ts, 170, True, 15.0)
        ts += 5
    for _ in range(120):                        # D: 2185..2780
        add(ts, 175, True, 15.0)
        ts += 5
    out = {"time": t, "heartrate": hr, "distance": dist,
           "velocity_smooth": [3.0 if m else 0.0 for m in mv]}
    if with_moving:
        out["moving"] = mv
    return out


@contextlib.contextmanager
def isolated_plan(with_plan: bool = True):
    """A throwaway plan (goal 8:35) or no plan at all; never the repo's data dir."""
    with tempfile.TemporaryDirectory() as tmp:
        with temp_plan(Path(tmp), plan=make_plan() if with_plan else None,
                       state={} if with_plan else None):
            yield


# ---------------------------------------------------------------------------
# Laps
# ---------------------------------------------------------------------------

class TestLapReads(unittest.TestCase):
    def setUp(self):
        self.lb = prr.lap_breakdown(long_run_laps(), max_hr=191)

    def test_parsed_laps_carry_the_clock(self):
        mi14 = self.lb["laps"][13]
        self.assertAlmostEqual(mi14["pace"], 591 / 60, places=3)
        self.assertAlmostEqual(mi14["elapsed_min"], 1967 / 60, places=3)
        self.assertEqual(mi14["stopped_s"], 1967 - 591)
        self.assertAlmostEqual(mi14["moving_ratio"], 591 / 1967, places=3)
        self.assertEqual(mi14["elev_gain_m"], 42.0)
        self.assertEqual(self.lb["split_unit_m"], prr.MI_M)

    def test_surge_reads_as_over_spent(self):
        s = self.lb["surge"]
        self.assertTrue(s["present"])
        self.assertEqual(s["miles"], [9, 11, 12, 15])
        self.assertEqual(s["verdict"], "surge_then_fade")
        self.assertIn("over-spent", s["note"])
        self.assertIn("confirm with the athlete", s["note"])
        self.assertNotIn("deliberate", s["note"])
        self.assertAlmostEqual(s["peak_hr"], 174.4)
        self.assertEqual(s["fade"]["n_late"], 2)
        self.assertGreater(s["fade"]["fade_sec"], 50)
        self.assertGreater(s["fade"]["late_hr"], config.easy_hr_cap())

    def test_fade_and_pattern(self):
        self.assertEqual(self.lb["pattern"], "even")      # the halves lie; the fade does not
        self.assertGreater(self.lb["fade_sec"], 30)
        self.assertIn("Pace fade", self.lb["fade_note"])

    def test_lap_stops(self):
        st = self.lb["stops"]
        self.assertTrue(st["available"])
        self.assertEqual([d["idx"] for d in st["diluted"]], [1, 4, 10, 14, 16, 17])
        self.assertEqual(st["n_diluted"], 6)
        self.assertEqual(st["stopped_s"], 14278 - 9724)
        self.assertAlmostEqual(st["moving_ratio"], 9724 / 14278, places=3)
        self.assertEqual(st["min_ratio"], prr.MIN_LAP_MOVING_RATIO)

    def test_pace_matched_decoupling_is_real_drift(self):
        d = self.lb["decoupling"]
        self.assertTrue(d["available"])
        self.assertIn("REAL DRIFT", d["verdict"])

    def test_mp_lap_summary_pace_vs_band(self):
        s = plan_tracker.mp_lap_summary(long_run_laps(), 8.583 + plan_tracker.MP_PACE_TOLERANCE_MIN,
                                        (150, 162))
        self.assertAlmostEqual(s["at_pace_mi"], 7.0)
        self.assertAlmostEqual(s["in_band_mi"], 2.0)
        self.assertAlmostEqual(s["over_band_mi"], 5.0)
        self.assertAlmostEqual(s["longest_in_band_mi"], 2.0)
        self.assertEqual([x["idx"] for x in s["laps"]], [5, 6, 8, 9, 11, 12, 15])

    def test_laps_without_elapsed_time_still_work(self):
        laps = [{"lap_index": i + 1, "distance": MI, "moving_time": 570,
                 "average_heartrate": 150} for i in range(6)]
        lb = prr.lap_breakdown(laps)
        self.assertIsNone(lb["laps"][0]["moving_ratio"])
        self.assertEqual(lb["laps"][0]["stopped_s"], 0)
        self.assertFalse(lb["stops"]["available"])
        self.assertFalse(lb["surge"]["present"])

    def test_empty_laps(self):
        self.assertEqual(prr.lap_breakdown([])["pattern"], "no laps")

    def test_kilometre_auto_laps_count_as_full_splits(self):
        spec = [(6.0, 140), (6.0, 141), (6.0, 142), (6.0, 143), (6.0, 150), (6.0, 151),
                (6.0, 152), (6.0, 153), (6.0, 154), (6.0, 155)]
        laps = mile_laps(spec, dist_m=1000.0)       # pace is min per MILE internally
        lb = prr.lap_breakdown(laps)
        self.assertEqual(lb["split_unit_m"], 1000.0)
        self.assertEqual(len(prr._full_laps(lb["laps"], unit_m=1000.0)), 10)
        self.assertTrue(lb["decoupling"]["available"])
        self.assertGreater(lb["decoupling"]["median_delta"], 8)
        self.assertEqual(prr._split_unit_m(prr.lap_breakdown(mile_laps([(10, 140)] * 5))["laps"]),
                         prr.MI_M)


class TestLateSurgeVerdicts(unittest.TestCase):
    def test_controlled_progression_is_deliberate(self):
        spec = [(10.0, 138)] * 6 + [(9.3, 150), (9.2, 152), (9.1, 154), (9.0, 156)]
        s = prr.late_surge(prr.lap_breakdown(mile_laps(spec))["laps"])
        self.assertTrue(s["present"])
        self.assertEqual(s["verdict"], "deliberate")
        self.assertIn("deliberate", s["note"])

    def test_slow_finish_under_the_cap_is_a_cool_down(self):
        spec = [(10.0, 138)] * 6 + [(9.0, 156), (9.0, 157), (11.0, 135), (11.0, 132)]
        s = prr.late_surge(prr.lap_breakdown(mile_laps(spec))["laps"])
        self.assertEqual(s["verdict"], "deliberate")
        self.assertTrue(any("cool-down" in r for r in s["reasons"]))

    def test_fast_split_after_a_stop_is_a_rested_split(self):
        laps = mile_laps([(10.0, 138)] * 6 + [(9.0, 150)] + [(10.0, 140)] * 2)
        laps[6]["elapsed_time"] = laps[6]["moving_time"] + 400      # stood 400 s inside the fast lap
        s = prr.late_surge(prr.lap_breakdown(laps)["laps"])
        self.assertFalse(s["present"])
        self.assertEqual(s["rested_fast_splits"], [7])


# ---------------------------------------------------------------------------
# Streams
# ---------------------------------------------------------------------------

class TestStreamReads(unittest.TestCase):
    def test_stream_stops_merge_gaps_and_still_runs(self):
        norm = intervals.normalize_streams(synthetic_stream())
        st = prr.stream_stops(norm)
        self.assertTrue(st["available"])
        self.assertEqual(st["n"], 2)
        self.assertEqual(st["total_s"], 390)
        gap, still = st["stops"]
        self.assertEqual((gap["start_s"], gap["duration_s"]), (595, 300))
        self.assertEqual((gap["hr_before"], gap["hr_after"]), (140, 160))
        self.assertAlmostEqual(gap["at_mi"], 120 * 15.0 / MI, places=3)
        self.assertEqual((still["start_s"], still["duration_s"]), (1495, 90))
        self.assertEqual((still["hr_before"], still["hr_after"]), (160, 170))

    def test_stream_stops_threshold_and_missing_time(self):
        norm = intervals.normalize_streams(synthetic_stream())
        self.assertEqual(prr.stream_stops(norm, min_gap_s=120)["n"], 1)
        self.assertFalse(prr.stream_stops({})["available"])
        self.assertFalse(prr.stream_stops({"time": [0]})["available"])

    def test_time_in_bands_is_dt_weighted_capped_and_moving_only(self):
        norm = intervals.normalize_streams(synthetic_stream())
        b = prr.time_in_hr_bands(norm, bands=BANDS)
        self.assertTrue(b["available"])
        secs = {x["name"]: x["seconds"] for x in b["bands"]}
        self.assertEqual(secs["easy"], 595)             # 119 intervals of 5 s
        self.assertEqual(secs["mp"], 595 + intervals.GAP_S)   # the gap credited at GAP_S
        self.assertEqual(secs["steady"], 0)             # still samples carry nothing
        self.assertEqual(secs["threshold"], 600)
        self.assertEqual(secs["vo2"], 600)
        self.assertEqual(b["total_s"], 2390 + intervals.GAP_S)
        self.assertEqual(b["at_or_above_lt_s"], 1200)
        self.assertEqual(b["at_or_above_90pct_s"], 600)
        self.assertEqual(b["peak_hr"], 175)
        self.assertEqual(b["max_dt"], intervals.GAP_S)
        self.assertEqual(b["lt_hr"], config.threshold_hr())
        self.assertAlmostEqual(b["hr_90pct"], config.max_hr() * config.vo2_pct_max())
        self.assertAlmostEqual(sum(x["pct"] for x in b["bands"]), 1.0)

    def test_time_in_bands_without_moving_stream_counts_every_sample(self):
        norm = intervals.normalize_streams(synthetic_stream(with_moving=False))
        b = prr.time_in_hr_bands(norm, bands=BANDS)
        secs = {x["name"]: x["seconds"] for x in b["bands"]}
        self.assertEqual(secs["steady"], 90)
        self.assertEqual(b["total_s"], 2480 + intervals.GAP_S)

    def test_default_bands_come_from_config(self):
        norm = intervals.normalize_streams(synthetic_stream())
        b = prr.time_in_hr_bands(norm)
        self.assertEqual([x["name"] for x in b["bands"]], [n for n, _, _ in config.zone_edges()])
        self.assertEqual(b["easy_cap"], config.easy_hr_cap())

    def test_time_in_bands_needs_hr(self):
        self.assertFalse(prr.time_in_hr_bands({"time": [0, 5, 10]})["available"])
        self.assertFalse(prr.time_in_hr_bands({})["available"])

    def test_cardiac_drift_and_projection_are_gone(self):
        """Half-vs-half HR drift measured how a run was designed, not decoupling;
        the half-marathon projection belongs to the forecast, not the debrief."""
        self.assertFalse(hasattr(prr, "cardiac_drift"))
        self.assertFalse(hasattr(prr, "projected_half_marathon"))


class TestRepTable(unittest.TestCase):
    def test_debug_explains_a_skipped_table_on_stderr(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            lines, comp = prr.rep_table_for({}, {}, debug=True, prescription="")
        self.assertEqual((lines, comp), ([], None))
        self.assertIn("[debug]", err.getvalue())
        quiet = io.StringIO()
        with contextlib.redirect_stderr(quiet):
            prr.rep_table_for({}, {}, prescription="")
        self.assertEqual(quiet.getvalue(), "")


# ---------------------------------------------------------------------------
# Plan context
# ---------------------------------------------------------------------------

class TestPlannedContext(unittest.TestCase):
    def test_first_sentence_targets_and_role(self):
        with isolated_plan():
            ctx = prr.planned_context(WK17, date(2026, 9, 12))
            monday = prr.planned_context(WK17, date(2026, 9, 7))
        self.assertEqual(ctx["key_workout"], "Long run 18 w/ 8 @ MP by HR.")
        self.assertEqual(ctx["quality_mi"], 8)
        self.assertEqual(ctx["quality_kind"], "mp")
        self.assertEqual(ctx["long_run_target_mi"], 18)
        self.assertEqual(ctx["long_run_time_cap_min"], 175)
        self.assertAlmostEqual(ctx["mp_pace_limit_min_mi"], 8.583 + plan_tracker.MP_PACE_TOLERANCE_MIN)
        self.assertEqual(ctx["mp_hr_band"], tuple(config.race_pace_hr_range()))
        self.assertEqual(ctx["role"], "long")              # Saturday is the long-run day
        self.assertNotEqual(monday["role"], "long")

    def test_quality_miles_fall_back_to_the_first_sentence(self):
        wk = {"key_workout": "Long run 16 w/ 6 @ MP. The program's own page asks 4 x 2mi."}
        self.assertEqual(prr.plan_quality_mi(wk), 6.0)
        self.assertEqual(prr.plan_quality_mi({"key_workout": "Easy week with strides."}), 0.0)
        self.assertEqual(prr.plan_quality_mi({"quality": {"kind": "tempo", "miles": 4}}), 4.0)
        self.assertEqual(prr.plan_quality_mi(None), 0.0)
        self.assertEqual(prr.first_sentence("One. Two."), "One.")
        self.assertEqual(prr.first_sentence(None), "")

    def test_no_week(self):
        with isolated_plan():
            self.assertIsNone(prr.planned_context(None, date(2026, 9, 12)))
            self.assertIsNone(prr.planned_context({}, date(2026, 9, 12)))


class TestOnFeet(unittest.TestCase):
    def test_start_wait_is_taken_out(self):
        lb = prr.lap_breakdown(long_run_laps())
        f = prr._on_feet(LR_ACTIVITY, lb)
        self.assertEqual(f["start_wait_s"], 2321 - 587)
        self.assertAlmostEqual(f["on_feet_min"], (14278 - (2321 - 587)) / 60)
        self.assertFalse(f["estimated"])

    def test_short_first_lap_stop_counts_as_time_on_feet(self):
        laps = [{"lap_index": 1, "distance": MI, "moving_time": 570, "elapsed_time": 630}]
        f = prr._on_feet({"elapsed_time": 3600, "moving_time": 3500}, prr.lap_breakdown(laps))
        self.assertEqual(f["start_wait_s"], 0)
        self.assertEqual(f["on_feet_min"], 60)

    def test_no_elapsed_falls_back_to_moving(self):
        f = prr._on_feet({"moving_time": 3000}, {"laps": []})
        self.assertTrue(f["estimated"])
        self.assertEqual(f["on_feet_min"], 50)


# ---------------------------------------------------------------------------
# Classification and segments
# ---------------------------------------------------------------------------

class TestClassifySession(unittest.TestCase):
    def test_order_of_authority(self):
        walk = {"distance": 3 * MI, "moving_time": 3 * 14 * 60}
        self.assertEqual(prr.classify_session(walk, None), ("WALK / SHAKEOUT", "pace"))
        reps = {"name": "8x800", "distance": 8000, "moving_time": 2400, "elapsed_time": 3600}
        self.assertEqual(prr.classify_session(reps, None), ("INTERVALS", "shape"))
        intent = {"segments": [{"kind": "easy", "placed": True, "miles": 2, "start_mi": 0, "end_mi": 2},
                               {"kind": "tempo", "placed": True, "miles": 3, "start_mi": 2, "end_mi": 5}]}
        short = {"distance": 5 * MI, "moving_time": 5 * 570}
        self.assertEqual(prr.classify_session(short, intent), ("TEMPO", "intent"))
        easy_intent = {"segments": [{"kind": "easy", "placed": True, "miles": 5, "start_mi": 0, "end_mi": 5}]}
        self.assertEqual(prr.classify_session(short, easy_intent), ("EASY", "intent"))
        longish = {"distance": 9 * MI, "moving_time": 9 * 600, "average_heartrate": 140}
        self.assertEqual(prr.classify_session(longish, easy_intent), ("LONG RUN", "intent"))
        self.assertEqual(prr.classify_session(short, None, plan_role="long"), ("LONG RUN", "plan"))
        self.assertEqual(prr.classify_session({**short, "average_heartrate": 140}, None), ("EASY", "hr_pace"))


class TestSegmentsFromDescription(unittest.TestCase):
    SPEC = [(10.0, 140)] * 3 + [(9.0, 158)] * 4 + [(10.17, 145)] * 2

    def _review(self, spec, description="3 easy, 4 tempo, 2 easy"):
        laps = mile_laps(spec)
        a = run_from_laps(laps, description=description)
        with isolated_plan(with_plan=False):
            return prr.review(a, laps=laps, lookup_plan=False)

    def _v(self, r, dim):
        return [v for v in r["verdicts"] if v["dim"] == dim]

    def test_declared_tempo_scores_against_the_config_band(self):
        r = self._review(self.SPEC)
        self.assertEqual(r["classification"], "LONG RUN")       # 9 mi, stated intent
        self.assertEqual(r["classification_source"], "intent")
        kinds = [lp["seg_kind"] for lp in r["laps"]["laps"]]
        self.assertEqual(kinds, ["easy"] * 3 + ["tempo"] * 4 + ["easy"] * 2)
        self.assertEqual(self._v(r, "intent")[0]["verdict"], "ok")
        self.assertIn("as you described it", self._v(r, "intent")[0]["line"])
        self.assertEqual(self._v(r, "easy")[0]["verdict"], "ok")
        q = self._v(r, "quality")[0]
        self.assertEqual(q["verdict"], "ok")
        self.assertIn("inside the tempo band", q["line"])
        self.assertEqual(self._v(r, "finish")[0]["verdict"], "ok")
        self.assertEqual(r["score"]["grade"], "A")
        self.assertIn("declared tempo", r["laps"]["pattern"])
        self.assertFalse(r["laps"]["surge"]["present"])

    def test_fade_after_the_block_is_a_miss(self):
        spec = [(10.0, 140)] * 3 + [(9.0, 158)] * 4 + [(10.75, 152)] * 2
        r = self._review(spec)
        f = self._v(r, "finish")[0]
        self.assertEqual(f["verdict"], "miss")
        self.assertIn("fade after the quality", f["line"])

    def test_tempo_run_too_fast_is_flagged_not_praised(self):
        spec = [(10.0, 140)] * 3 + [(8.3, 168)] * 4 + [(10.17, 145)] * 2
        r = self._review(spec)
        q = self._v(r, "quality")[0]
        self.assertEqual(q["verdict"], "miss")
        self.assertIn("faster than the tempo band", q["line"])

    def test_unplaced_block_lands_on_the_fastest_laps(self):
        spec = [(10.0, 140)] * 4 + [(8.7, 158)] * 3 + [(10.0, 142)] * 3
        laps = mile_laps(spec)
        a = run_from_laps(laps, description="10 w/ 3 @ MP")
        with isolated_plan(with_plan=False):
            r = prr.review(a, laps=laps, lookup_plan=False)
        mp_idx = [lp["idx"] for lp in r["laps"]["laps"] if lp["seg_kind"] == "mp"]
        self.assertEqual(mp_idx, [5, 6, 7])
        q = self._v(r, "quality")[0]
        self.assertEqual(q["verdict"], "ok")
        self.assertIn("3.0 mi of 3.0 mi at marathon pace", q["line"])

    def test_undeclared_fast_miles_are_reported_not_scored(self):
        spec = [(10.0, 140)] * 3 + [(9.0, 158)] * 3 + [(10.0, 142)] * 2
        laps = mile_laps(spec)
        a = run_from_laps(laps, description=None, name="Morning Run")
        with isolated_plan(with_plan=False):
            r = prr.review(a, laps=laps, lookup_plan=False)
        q = self._v(r, "quality")[0]
        self.assertEqual(q["verdict"], "info")
        self.assertIn("no stated intent", q["line"])
        self.assertEqual([lp["idx"] for lp in r["laps"]["laps"] if lp["seg_kind"] == "undeclared"],
                         [4, 5, 6])


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

class TestReviewAssembly(unittest.TestCase):
    def _long_run(self, **kw):
        with isolated_plan():
            return prr.review(LR_ACTIVITY, laps=long_run_laps(), plan_week=WK17,
                              lookup_plan=False, **kw)

    def _v(self, r, dim):
        return [v for v in r["verdicts"] if v["dim"] == dim]

    def test_long_run_is_a_d_that_names_the_band_miss(self):
        r = self._long_run()
        self.assertEqual(r["classification"], "LONG RUN")
        self.assertEqual(r["classification_source"], "plan")
        # easy miss -20 (post-block miles above the long-run cap), quality miss -20,
        # finish miss -20 (surge then fade), cap watch -10
        self.assertEqual(self._v(r, "easy")[0]["verdict"], "miss")
        self.assertEqual(self._v(r, "quality")[0]["verdict"], "miss")
        self.assertEqual(self._v(r, "finish")[0]["verdict"], "miss")
        self.assertEqual(self._v(r, "cap")[0]["verdict"], "watch")
        self.assertEqual(self._v(r, "intent")[0]["verdict"], "ok")
        self.assertEqual((r["score"]["score"], r["score"]["grade"]), (30, "D"))
        notes = "\n".join(r["score"]["notes"])
        self.assertIn("2.0 mi of 8.0 mi at marathon pace inside HR 150-162", notes)
        self.assertIn("7.0 mi ran at the pace", notes)
        self.assertIn("Over-spent", notes)
        self.assertIn("5 split(s) under 80% moving", notes)
        self.assertIn("1:15:54 stopped", notes)
        self.assertIn("against a 2:55 cap", notes)
        self.assertIn("28:54 wait at the start", notes)
        self.assertIn("[+] Intent: 17.4 mi of 18.0 mi planned (97%)", notes)
        self.assertEqual(r["planned"]["quality_mi"], 8)
        self.assertEqual(r["executed"]["mp"]["in_band_mi"], 2.0)
        self.assertEqual(r["executed"]["mp"]["longest_in_band_mi"], 2.0)
        self.assertEqual(r["laps"]["surge"]["verdict"], "surge_then_fade")
        self.assertFalse(r["stream"]["available"])
        self.assertIsNone(r["executed"]["under_easy_cap_s"])
        self.assertEqual(self._v(r, "load")[0]["verdict"], "n/a")
        self.assertEqual(self._v(r, "sensor")[0]["verdict"], "n/a")

    def test_stream_adds_cost_and_sensor_checks_without_changing_the_grade(self):
        r = self._long_run(streams=synthetic_stream())
        self.assertEqual(r["score"]["score"], 30)             # HR minutes are cost, not a deduction
        self.assertTrue(r["stream"]["available"])
        self.assertEqual(r["stream"]["stops"]["n"], 2)
        self.assertTrue(r["stream"]["bands"]["available"])
        self.assertEqual(r["stream"]["bands"]["at_or_above_lt_s"], 1200)
        self.assertIsNotNone(r["executed"]["under_easy_cap_s"])
        self.assertEqual(self._v(r, "sensor")[0]["verdict"], "info")

    def test_without_a_plan_the_run_still_scores(self):
        with isolated_plan(with_plan=False):
            r = prr.review(LR_ACTIVITY, laps=long_run_laps(), lookup_plan=False)
        self.assertIsNone(r["planned"])
        self.assertIsNone(r["executed"]["mp"])
        self.assertEqual(self._v(r, "intent")[0]["verdict"], "n/a")
        self.assertEqual(self._v(r, "cap")[0]["verdict"], "n/a")
        # No plan, no description: the fast miles are undeclared and only reported;
        # what still counts is the fade after them and the easy mile that wasn't.
        self.assertTrue(any(v["verdict"] == "info" for v in self._v(r, "quality")))
        self.assertEqual(self._v(r, "finish")[0]["verdict"], "miss")
        self.assertEqual(self._v(r, "easy")[0]["verdict"], "watch")
        self.assertEqual((r["score"]["score"], r["score"]["grade"]), (70, "C"))

    def test_easy_long_run_on_plan_scores_a(self):
        laps = [{"lap_index": i + 1, "distance": MI, "moving_time": 600, "elapsed_time": 603,
                 "average_heartrate": 145} for i in range(12)]
        a = {"id": 1, "name": "Long easy", "start_date_local": "2026-05-23T07:00:00",
             "distance": 12.1 * MI, "moving_time": 7260, "elapsed_time": 7300,
             "average_heartrate": 145, "max_heartrate": 160}
        wk = {"week_num": 1, "phase": "base", "start_date": "2026-05-18", "target_miles": 30,
              "long_run_target": 12, "long_run_time_cap_min": 130, "key_workout": "Long run 12 easy."}
        with isolated_plan():
            r = prr.review(a, laps=laps, plan_week=wk, lookup_plan=False)
        self.assertEqual((r["score"]["score"], r["score"]["grade"]), (100, "A"))
        scored = [v for v in r["verdicts"] if v["dim"] in prr.SCORED_DIMS]
        self.assertTrue(all(v["verdict"] in ("ok", "n/a") for v in scored))
        self.assertTrue(any("Continuous" in n for n in r["score"]["notes"]))
        self.assertEqual(self._v(r, "cap")[0]["verdict"], "ok")

    def test_render_and_json(self):
        r = self._long_run()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            prr.render(r)
        text = out.getvalue()
        for needle in ("PLANNED vs EXECUTED", "Plan (wk17):", "MP splits:", "Time on feet:",
                       "LAP BREAKDOWN", "STOPS", "VERDICTS", "Over-spent", "D (30/100)",
                       "Data:              laps yes, stream no"):
            self.assertIn(needle, text)
        self.assertNotIn("The block is the point", text)      # rationale stays out of the header
        self.assertNotIn("Projected half marathon", text)
        back = json.loads(json.dumps(r, default=str))
        self.assertEqual(back["score"]["grade"], "D")
        self.assertEqual(back["planned"]["mp_hr_band"], [150, 162])
        self.assertEqual(back["schema"], 1)
        self.assertIn("verdicts", back)

    def test_interval_session_skips_lap_verdicts_and_mp_summary(self):
        a = {"id": 2, "name": "8x800", "start_date_local": "2026-09-09T19:00:00",
             "distance": 8000, "moving_time": 2400, "elapsed_time": 3600,
             "average_heartrate": 150, "max_heartrate": 178}
        laps = [{"lap_index": i + 1, "distance": MI, "moving_time": 480, "elapsed_time": 720,
                 "average_heartrate": 150} for i in range(5)]
        with isolated_plan():
            r = prr.review(a, laps=laps, plan_week=WK17, lookup_plan=False)
        self.assertEqual(r["classification"], "INTERVALS")
        self.assertFalse(r["lap_reads_valid"])
        self.assertIsNone(r["executed"]["mp"])
        q = self._v(r, "quality")[0]
        self.assertEqual(q["verdict"], "n/a")
        self.assertIn("stream", q["line"])

    def test_metric_units_follow_config(self):
        laps = mile_laps([(10.0, 140)] * 6)
        a = run_from_laps(laps, name="Easy")
        with tempfile.TemporaryDirectory() as tmp:
            with temp_config(Path(tmp), {"athlete": {"units": "km"}}):
                with isolated_plan(with_plan=False):
                    r = prr.review(a, laps=laps, lookup_plan=False)
                    out = io.StringIO()
                    with contextlib.redirect_stdout(out):
                        prr.render(r)
        text = out.getvalue()
        self.assertEqual(r["activity"]["unit"], "km")
        self.assertIn("/km", text)
        self.assertNotIn("/mi", text)
        self.assertIn("9.66 km", text)                      # 6 mi


class TestLoadAndSensorLines(unittest.TestCase):
    def test_load_line_reads_tsb_and_warns_on_thin_history(self):
        ctx = {"tss": 80, "tsb_before": -5.2, "tsb_after": -12.4, "ctl": 40.1,
               "history_days": 30, "history_sufficient": False}
        v = prr._load_verdict(ctx, {"average_heartrate": 150})
        self.assertEqual(v["verdict"], "info")
        self.assertIn("80 TSS (HR-based)", v["line"])
        self.assertIn("-5 going in, -12 the next morning", v["line"])
        self.assertIn("Only 30d of history", v["line"])
        ok = prr._load_verdict({**ctx, "history_sufficient": True}, {})
        self.assertNotIn("history", ok["line"])
        self.assertIn("pace-based", ok["line"])
        self.assertIn("55 TSS", prr._load_verdict(None, {"_run_tss": 55})["line"])
        self.assertEqual(prr._load_verdict(None, {})["verdict"], "n/a")

    def test_sensor_line(self):
        flagged = prr._sensor_verdict({"available": True, "flag": "Wrist HR looks unreliable here: x.",
                                       "notes": ["detail"]})
        self.assertEqual(flagged["verdict"], "info")
        self.assertTrue(flagged["line"].startswith("Wrist HR looks unreliable"))
        self.assertEqual(prr._sensor_verdict({})["verdict"], "n/a")
        clean = prr._sensor_verdict({"available": True, "flag": None, "notes": []})
        self.assertIn("passed", clean["line"])


class TestGradeFromVerdicts(unittest.TestCase):
    def test_only_scored_dimensions_deduct(self):
        vs = [prr._v("intent", "ok", ""), prr._v("easy", "watch", ""), prr._v("quality", "miss", ""),
              prr._v("load", "info", ""), prr._v("sensor", "info", ""), prr._v("cap", "n/a", "")]
        score, grade, notes = prr.grade_from(vs)
        self.assertEqual((score, grade), (70, "C"))
        self.assertTrue(notes[2].startswith("[-] Quality"))
        self.assertTrue(notes[1].startswith("[~] Easy"))

    def test_execution_score_without_context_still_answers(self):
        a = {"distance": 16093.4, "moving_time": 6000, "average_heartrate": 150}
        score, grade, notes = prr.execution_score(a, "LONG RUN")
        self.assertEqual((score, grade), (90, "A"))   # avg HR 150 vs the 145 long-run cap: watch, no laps
        self.assertTrue(any(n.startswith("[~] Easy") for n in notes))


class TestPrintReview(unittest.TestCase):
    def _no_api(self):
        return mock.patch.object(prr, "StravaAPI", side_effect=RuntimeError("no creds in tests"))

    def test_no_runs_returns_1(self):
        with tempfile.TemporaryDirectory() as tmp:
            with temp_activity_data(Path(tmp)), isolated_plan(with_plan=False), self._no_api():
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    rc = prr.print_review()
        self.assertEqual(rc, 1)
        self.assertIn("No run to review", err.getvalue())

    def test_latest_run_renders_and_json_is_one_document(self):
        laps = mile_laps([(10.0, 140)] * 5)
        a = enrich(make_activity(name="Easy five", distance=5 * MI, moving_time=3000, laps=laps))
        with tempfile.TemporaryDirectory() as tmp:
            with temp_activity_data(Path(tmp), cache_activities=[a]), isolated_plan(with_plan=False), \
                    self._no_api():
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    rc = prr.print_review()
                self.assertEqual(rc, 0)
                self.assertIn("POST-RUN REVIEW", out.getvalue())
                self.assertIn("Streams: not cached", out.getvalue())
                out = io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                    rc = prr.print_review(a["id"], as_json=True)
        self.assertEqual(rc, 0)
        doc = json.loads(out.getvalue())
        self.assertEqual(doc["activity"]["id"], a["id"])
        self.assertEqual(doc["score"]["grade"], "A")


class TestCli(unittest.TestCase):
    def test_parse(self):
        ns = prr._parse_cli(["20145951475", "--json", "--debug"])
        self.assertEqual((ns.activity_id, ns.json, ns.debug), (20145951475, True, True))
        ns = prr._parse_cli([])
        self.assertEqual((ns.activity_id, ns.json, ns.debug), (None, False, False))


class TestFormatting(unittest.TestCase):
    def test_fmt_hms(self):
        self.assertEqual(prr._fmt_hms(4554), "1:15:54")
        self.assertEqual(prr._fmt_hms(90), "1:30")
        self.assertEqual(prr._fmt_hms(0), "0:00")

    def test_fmt_pace_rounds(self):
        self.assertEqual(prr._fmt_pace(9.083), "9:05/mi")
        self.assertEqual(prr._fmt_pace(587 / 60), "9:47/mi")
        self.assertEqual(prr._fmt_pace(9.9999), "10:00/mi")
        self.assertEqual(prr._fmt_pace(0), "N/A")

    def test_fmt_hm(self):
        self.assertEqual(prr._fmt_hm(175), "2:55")
        self.assertEqual(prr._fmt_hm(209.07), "3:29")


if __name__ == "__main__":
    unittest.main()
