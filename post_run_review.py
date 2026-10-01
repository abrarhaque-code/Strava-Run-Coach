"""Single-run review: the five things a runner wants to know, graded.

    python3 coach.py review              # latest run
    python3 coach.py review <id> [--json]

1. Did I keep easy easy?        lap HR under the cap (stream HR minus post-stop ramps)
2. Did I execute the workout?   declared or planned segments by pace AND HR; reps from the stream
3. How did the long run hold?   pace-matched decoupling, stops, surge-then-fade vs a deliberate finish
4. How hard was it, really?     TSS and where it left form (TSB) going into tomorrow
5. A grade, one line per dimension (ok / watch / miss / n/a / info)

Plus a plain flag when the wrist HR trace looks unreliable (hr_quality).

Intent comes from the athlete's own Strava description first ("13 easy, 3 hmp,
2 easy"), then the plan week; the plan stays a separate compliance line. HR is
treated as what the day cost, pace as what the athlete did: a hot day raises HR
at the same pace, so no dimension deducts for HR alone except the easy cap,
which is the one thing easy running is about.

`review()` is pure (no I/O) and returns a dict; `render()` prints it;
`print_review()` loads from the cache (REST API only when credentials exist).
Streams come from metrics.load_streams (the MCP ingest writes them); without
them the review says so and uses the laps alone.
"""

import argparse
import contextlib
import json
import re
import statistics
import sys
import textwrap
from datetime import date
from typing import Optional

import config
import enrichment
import hr_quality
import intervals
import marathon_plan
import metrics
import plan_layout
import plan_tracker
import session_intent
import units
from strava_api import StravaAPI, StravaAPIError

CACHE_DIR = config.CACHE_DIR
ACT_DIR = config.ACTIVITIES_DIR
STREAMS_DIR = config.STREAMS_DIR

MI_M = 1609.34
MIN_LAP_MOVING_RATIO = 0.80      # a lap under this share moving is stop-diluted
FADE_SEC = 30                    # s per split-unit slower than the reference = a fade
SURGE_FASTER_SEC = 15            # s per split-unit faster than the early average = a surge
PACE_MATCH_TOLERANCE_SEC = 12    # two laps within this are "the same pace"
STOP_MIN_S = 60                  # a stream gap / still run this long is a stop
START_WAIT_MIN_S = 300           # a 5+ min stop inside lap 1 is pre-run waiting, not time on feet
LONG_RUN_DISTANCE_PCT = 0.90     # under this share of the long-run target = missed long run
NOT_THE_LONG_RUN_PCT = 0.70      # under this share it was not the long run at all
CAP_DISTANCE_TOL = 0.15          # the clock cap belongs to the planned long run's distance
SEGMENT_PACE_SLACK_SEC = 10      # a segment this close outside its band is a watch
REAL_DRIFT_BPM = 8               # pace-matched HR delta above this is real decoupling

VERDICT_DEDUCT = {"miss": 20, "watch": 10}
SCORED_DIMS = ("intent", "easy", "quality", "finish", "cap")
_MARK = {"ok": "+", "watch": "~", "miss": "-", "info": "i", "n/a": " "}

LABELS = {"long": "LONG RUN", "intervals": "INTERVALS", "tempo": "TEMPO",
          "threshold": "THRESHOLD", "easy": "EASY", "recovery": "RECOVERY",
          "walk": "WALK / SHAKEOUT", "general_aerobic": "GENERAL AEROBIC", "unknown": "UNKNOWN"}
STREAM_CLASSES = ("LONG RUN", "INTERVALS", "TEMPO", "THRESHOLD")

# Re-exports: the one classifier lives in enrichment.
looks_like_intervals = enrichment.looks_like_intervals
INTERVAL_MOVING_RATIO = enrichment.INTERVAL_MOVING_RATIO


def classify_run(a: dict) -> str:
    """Review label for a run (enrichment.classify_run, labelled)."""
    return LABELS.get(enrichment.classify_run(a), "GENERAL AEROBIC")


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def _fmt_pace(pace_min_per_mi: float) -> str:
    return units.fmt_pace(pace_min_per_mi)


def _fmt_hms(seconds: float) -> str:
    seconds = int(round(seconds or 0))
    h, rem = divmod(seconds, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def _fmt_hm(minutes: float) -> str:
    total = int(round(minutes or 0))
    return f"{total // 60}:{total % 60:02d}"


def _dist(mi: float) -> str:
    return units.fmt_dist(mi=mi)


# ---------------------------------------------------------------------------
# Laps
# ---------------------------------------------------------------------------

def _split_unit_m(parsed: list) -> float:
    """Auto-split unit: metric watches lap every 1000 m, imperial every mile.
    The modal lap distance decides, so km laps count as full laps too."""
    dists = [lp["dist_mi"] * MI_M for lp in parsed if (lp.get("dist_mi") or 0) * MI_M >= 800]
    if not dists:
        return MI_M
    near_km = sum(1 for d in dists if abs(d - 1000) / 1000 <= 0.05)
    near_mi = sum(1 for d in dists if abs(d - MI_M) / MI_M <= 0.05)
    return 1000.0 if near_km > near_mi else MI_M


def _full_laps(parsed: list, need_hr: bool = False, unit_m: Optional[float] = None) -> list:
    """The full-split laps every pattern/surge/decoupling read is built on."""
    unit_m = unit_m or _split_unit_m(parsed)
    lo, hi = 0.95 * unit_m / MI_M, 1.05 * unit_m / MI_M
    return [lp for lp in parsed
            if lo <= lp["dist_mi"] <= hi and lp["pace"] > 0
            and (lp.get("avg_hr") or not need_hr)]


_full_miles = _full_laps   # legacy name


def lap_breakdown(laps: list, max_hr: float = None) -> dict:
    """Parse the laps and read their structure: split pattern, fade, late
    surge, pace-matched decoupling, stops. Each parsed lap carries moving AND
    elapsed time, so a stop inside a split is visible."""
    if not laps:
        return {"laps": [], "pattern": "no laps", "split_unit_m": MI_M, "fade_note": None,
                "fade_sec": None, "surge": {"present": False},
                "decoupling": {"available": False, "reason": "no laps"},
                "stops": {"available": False, "reason": "no laps"}}

    parsed = []
    for lap in laps:
        dist_mi = (lap.get("distance", 0) or 0) / MI_M
        mt_s = lap.get("moving_time", 0) or 0
        el_s = lap.get("elapsed_time", 0) or 0
        mt = mt_s / 60
        pace = mt / dist_mi if dist_mi > 0 else 0
        parsed.append({
            "idx": lap.get("lap_index"),
            "dist_mi": dist_mi,
            "moving_min": mt,
            "elapsed_min": (el_s / 60) if el_s else None,
            "stopped_s": max(0, el_s - mt_s) if el_s else 0,
            "moving_ratio": (mt_s / el_s) if (mt_s and el_s) else None,
            "pace": pace,
            "avg_hr": lap.get("average_heartrate"),
            "max_hr": lap.get("max_heartrate"),
            "elev_gain_m": lap.get("total_elevation_gain"),
            "cadence": lap.get("average_cadence") or lap.get("avg_cadence"),
            "elapsed_s": el_s,
            "moving_s": mt_s,
        })

    unit_m = _split_unit_m(parsed)
    full = _full_laps(parsed, unit_m=unit_m)
    pattern = "even"
    if len(full) >= 3:
        first_half = full[: len(full) // 2]
        second_half = full[len(full) // 2:]
        diff_sec = (statistics.mean(lap["pace"] for lap in second_half)
                    - statistics.mean(lap["pace"] for lap in first_half)) * 60
        if diff_sec > 30:
            pattern = "positive split (slowed down)"
        elif diff_sec < -30:
            pattern = "negative split (sped up)"
        elif diff_sec > 15:
            pattern = "slight fade"

    fade = _fade_sec(full)
    fade_note = None
    if fade and fade["fade_sec"] > FADE_SEC:
        fade_note = (f"Last {fade['n_late']} split(s) ran {fade['fade_sec']:.0f} s/{units.unit()} "
                     f"slower than the rest. Pace fade.")

    return {
        "laps": parsed,
        "split_unit_m": unit_m,
        "pattern": pattern,
        "fade_note": fade_note,
        "fade_sec": fade["fade_sec"] if fade else None,
        "surge": late_surge(parsed, max_hr=max_hr, unit_m=unit_m),
        "decoupling": pace_matched_decoupling(parsed, unit_m=unit_m),
        "stops": lap_stops(parsed),
    }


def _fade_sec(full: list, after: Optional[list] = None) -> Optional[dict]:
    """How much slower the closing splits ran than the rest, in s/mi.
    `after` names a specific tail (the splits after a surge)."""
    if after is not None:
        late = list(after)
        rest = [lp for lp in full if not any(lp is x for x in late)]
    else:
        if len(full) < 4:
            return None
        late = full[-max(1, len(full) // 4):]
        rest = full[: -len(late)]
    if not late or not rest:
        return None
    rest_pace = statistics.mean(lp["pace"] for lp in rest)
    late_pace = statistics.mean(lp["pace"] for lp in late)
    hrs = [lp["avg_hr"] for lp in late if lp.get("avg_hr")]
    return {"fade_sec": (late_pace - rest_pace) * 60, "n_late": len(late),
            "late_hr": statistics.mean(hrs) if hrs else None}


def lap_stops(parsed: list) -> dict:
    """Stop dilution read off the laps: which laps were under
    MIN_LAP_MOVING_RATIO moving, and how long the clock stopped overall."""
    with_ratio = [lp for lp in parsed if lp.get("moving_ratio") is not None]
    if not with_ratio:
        return {"available": False, "reason": "laps carry no elapsed time"}
    diluted = [lp for lp in with_ratio if lp["moving_ratio"] < MIN_LAP_MOVING_RATIO]
    moving_s = sum(lp["moving_min"] * 60 for lp in with_ratio)
    elapsed_s = sum(lp["elapsed_min"] * 60 for lp in with_ratio)
    return {
        "available": True,
        "n_laps": len(with_ratio),
        "diluted": [{"idx": lp["idx"], "moving_ratio": lp["moving_ratio"],
                     "stopped_s": lp["stopped_s"], "pace": lp["pace"]} for lp in diluted],
        "n_diluted": len(diluted),
        "stopped_s": max(0.0, elapsed_s - moving_s),
        "moving_ratio": (moving_s / elapsed_s) if elapsed_s else None,
        "min_ratio": MIN_LAP_MOVING_RATIO,
    }


def late_surge(parsed: list, max_hr: float = None, easy_cap: float = None,
               unit_m: Optional[float] = None) -> dict:
    """Faster splits in the back half of a run, and what they were.

    verdict is "deliberate" (a planned progression / tired-legs finish, which
    many runners do on purpose, so it is presumed) or "surge_then_fade" (the
    splits AFTER the last fast one ran FADE_SEC or more slower at effort: fast
    miles that then fall apart are over-spending). A slow finish at or under
    the easy cap is a cool-down, not a fade; a fast split after a stop is a
    rested split and is left out. Either way, ask the athlete before scoring.
    """
    full = _full_laps(parsed, unit_m=unit_m)
    if len(full) < 4:
        return {"present": False}
    easy_cap = float(easy_cap or config.easy_hr_cap())
    ref_max = float(max_hr or config.max_hr())
    ceiling = ref_max * config.vo2_pct_max()

    half = len(full) // 2
    early_pace = statistics.mean(lp["pace"] for lp in full[:half])
    surge, rested = [], []
    for i, lp in enumerate(full):
        if i < half or (early_pace - lp["pace"]) * 60 < SURGE_FASTER_SEC:
            continue
        ratio = lp.get("moving_ratio")
        (rested if ratio is not None and ratio < MIN_LAP_MOVING_RATIO else surge).append(i + 1)
    if not surge:
        out = {"present": False}
        if rested:
            out["rested_fast_splits"] = rested
        return out

    surge_hrs = [full[m - 1]["avg_hr"] for m in surge if full[m - 1].get("avg_hr")]
    peak_hr = max(surge_hrs) if surge_hrs else None
    fastest_idx = min(range(len(full)), key=lambda i: full[i]["pace"]) + 1
    gain = (early_pace - min(full[i]["pace"] for i in range(half, len(full)))) * 60
    tail = full[surge[-1]:]
    fade = _fade_sec(full, after=tail) if tail else None
    fade_at_effort = bool(fade and fade["fade_sec"] >= FADE_SEC
                          and (fade["late_hr"] is None or fade["late_hr"] > easy_cap))

    reasons = []
    if fade_at_effort:
        hr_txt = f" at HR {fade['late_hr']:.0f}" if fade["late_hr"] else ""
        reasons.append(f"{fade['fade_sec']:.0f} s slower per split over the {fade['n_late']} "
                       f"split(s) after the surge{hr_txt}")
    elif fade and fade["fade_sec"] >= FADE_SEC:
        reasons.append(f"slower finish at HR {fade['late_hr']:.0f} reads as a cool-down")
    if peak_hr is not None and peak_hr >= ceiling:
        reasons.append(f"surge HR {peak_hr:.0f} = {peak_hr / ref_max:.0%} of max")
    if rested:
        reasons.append("fast split(s) after a stop left out: " + ", ".join(f"#{m}" for m in rested))

    splits_txt = ", ".join(f"#{m}" for m in surge)
    head = f"Faster split(s) at {splits_txt} (up to {gain:.0f} s quicker than the early average)"
    if fade_at_effort:
        verdict = "surge_then_fade"
        hr_txt = f" at HR {fade['late_hr']:.0f}" if fade["late_hr"] else ""
        note = (f"{head}, then {fade['fade_sec']:.0f} s slower over the last {fade['n_late']} "
                f"split(s){hr_txt}. Reads as over-spent rather than a controlled progression; "
                "confirm with the athlete before scoring it as a plan.")
    else:
        verdict = "deliberate"
        note = f"{head}. Reads as a deliberate tired-legs finish; not lost control."
        if peak_hr is not None and peak_hr >= ceiling:
            note += (f" HR peaked at {peak_hr:.0f} ({peak_hr / ref_max:.0%} of max) on the "
                     "surge: a finishing kick, not race-pace work.")
    return {"present": True, "miles": surge, "splits": surge, "fastest_mile": fastest_idx,
            "verdict": verdict, "reasons": reasons, "peak_hr": peak_hr, "fade": fade, "note": note}


def pace_matched_decoupling(parsed: list, unit_m: Optional[float] = None) -> dict:
    """True aerobic decoupling: HR at equivalent PACE, early vs late.

    Aggregate first-half-vs-second-half HR cannot tell decoupling from a
    progression workout; it measures how the session was DESIGNED. Only
    splits run at comparable speed are compared, every pair within tolerance
    is scored, the verdict is the MEDIAN delta and the worst pair is named.
    """
    full = _full_laps(parsed, need_hr=True, unit_m=unit_m)
    if len(full) < 4:
        return {"available": False, "reason": "need 4+ full splits with HR"}
    half = len(full) // 2
    pairs = []
    for i, e in enumerate(full[:half]):
        for j, lp in enumerate(full[half:]):
            gap_sec = abs(e["pace"] - lp["pace"]) * 60
            if gap_sec <= PACE_MATCH_TOLERANCE_SEC:
                pairs.append({"early_mile": i + 1, "late_mile": half + j + 1,
                              "early_pace": e["pace"], "late_pace": lp["pace"],
                              "early_hr": e["avg_hr"], "late_hr": lp["avg_hr"],
                              "pace_gap_sec": gap_sec, "hr_delta": lp["avg_hr"] - e["avg_hr"]})
    if not pairs:
        return {"available": False, "reason": "no pace-matched pair (pace varied too much to compare)"}
    closest = min(pairs, key=lambda p: p["pace_gap_sec"])
    worst = max(pairs, key=lambda p: p["hr_delta"])
    median_delta = statistics.median(p["hr_delta"] for p in pairs)
    result = dict(closest)
    result.update({"available": True, "n_pairs": len(pairs), "median_delta": median_delta,
                   "max_delta": worst["hr_delta"], "worst": worst,
                   "verdict": _decoupling_verdict(median_delta)})
    if median_delta <= REAL_DRIFT_BPM and worst["hr_delta"] > REAL_DRIFT_BPM:
        result["outlier_note"] = (
            f"Median is benign but split #{worst['early_mile']} -> #{worst['late_mile']} at matched "
            f"pace shows {worst['hr_delta']:+.0f} bpm: one drifting stretch, not a whole-run "
            "pattern. Check fuel and heat at that point in the run.")
    return result


def _decoupling_verdict(delta: float) -> str:
    if delta <= 3:
        return "CLEAN: no meaningful decoupling at matched pace"
    if delta <= REAL_DRIFT_BPM:
        return "MILD: normal for a long run in heat"
    return "REAL DRIFT: HR climbing at equal pace; check heat, fuel, sleep"


# ---------------------------------------------------------------------------
# Streams
# ---------------------------------------------------------------------------

def first_sentence(text) -> str:
    """Prescription first, rationale after: only the first sentence is read."""
    if not text:
        return ""
    s = str(text).strip()
    m = re.search(r"[.!?](\s|$)", s)
    return s[: m.start() + 1].strip() if m else s


def rep_table_for(a: dict, streams: dict, debug: bool = False,
                  prescription: Optional[str] = None, plan_week: Optional[dict] = None) -> tuple:
    """(report_lines, compliance): ([], None) when there is no rep structure.
    The prescription text is the description, the title, then the first
    sentence of the plan week's key workout."""
    try:
        if prescription is None:
            parts = [str(a[k]) for k in ("description", "name") if a.get(k)]
            if plan_week:
                parts.append(first_sentence(plan_week.get("key_workout")))
            prescription = "\n".join(p for p in parts if p)
        rs = intervals.detect_reps(intervals.normalize_streams(streams), prescription=prescription)
        if rs.completed < 3:
            if debug:
                print(f"[debug] rep table: {rs.completed} running segment(s), not a rep session",
                      file=sys.stderr)
            return [], None
        return intervals.format_report(rs), intervals.compliance(rs)
    except Exception as e:
        if debug:
            print(f"[debug] rep table skipped: {type(e).__name__}: {e}", file=sys.stderr)
        return [], None


def _hr_near(hr: list, idx: int, step: int, reach: int = 10) -> Optional[float]:
    for k in range(reach):
        j = idx + step * k
        if j < 0 or j >= len(hr):
            return None
        if hr[j]:
            return hr[j]
    return None


def stream_stops(norm: dict, min_gap_s: int = STOP_MIN_S) -> dict:
    """Stops read off the stream, each with where it fell and HR into and out."""
    t = norm.get("time") or []
    if len(t) < 2:
        return {"available": False, "reason": "no time stream"}
    hr = norm.get("heartrate") or []
    dist = norm.get("distance") or []
    moving = norm.get("moving") or []
    n = len(t)
    spans = []
    for i in range(1, n):
        if t[i] - t[i - 1] >= min_gap_s:
            spans.append([t[i - 1], t[i], i - 1, i])
    m = min(n, len(moving))
    i = 0
    while i < m:
        if moving[i] is False:
            j = i
            while j + 1 < m and moving[j + 1] is False:
                j += 1
            end_idx = min(j + 1, n - 1)
            if t[end_idx] - t[i] >= min_gap_s:
                spans.append([t[i], t[end_idx], max(i - 1, 0), end_idx])
            i = j + 1
        else:
            i += 1
    spans.sort()
    merged = []
    for sp in spans:
        if merged and sp[0] <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], sp[1])
            merged[-1][3] = max(merged[-1][3], sp[3])
        else:
            merged.append(list(sp))
    stops = []
    for s0, s1, i0, i1 in merged:
        at = dist[i0] if i0 < len(dist) else None
        stops.append({"start_s": s0, "duration_s": s1 - s0,
                      "at_mi": (at / MI_M) if at is not None else None,
                      "hr_before": _hr_near(hr, i0, -1) if hr else None,
                      "hr_after": _hr_near(hr, i1, +1) if hr else None})
    return {"available": True, "stops": stops, "n": len(stops),
            "total_s": sum(x["duration_s"] for x in stops), "min_gap_s": min_gap_s}


def time_in_hr_bands(norm: dict, bands: Optional[list] = None, max_dt: int = intervals.GAP_S,
                     lt_hr: Optional[float] = None, max_hr: Optional[float] = None) -> dict:
    """Seconds in each HR band, dt-weighted over moving samples (a downsampled
    stream is credited in full; a recording gap counts for one interval)."""
    t = norm.get("time") or []
    hr = norm.get("heartrate") or []
    if len(t) < 2 or not hr:
        return {"available": False, "reason": "no HR stream"}
    moving = norm.get("moving") or []
    bands = bands or config.zone_edges()
    lt_hr = float(lt_hr or config.threshold_hr())
    max_hr = float(max_hr or config.max_hr())
    hr90 = max_hr * config.vo2_pct_max()
    n = min(len(t), len(hr))
    secs = {name: 0.0 for name, _, _ in bands}
    total = at_lt = at_90 = 0.0
    peak = None
    no_hr = 0
    for i in range(1, n):
        if i < len(moving) and moving[i] is False:
            continue
        h = hr[i]
        if not h:
            no_hr += 1
            continue
        dt = min(t[i] - t[i - 1], max_dt)
        if dt <= 0:
            continue
        total += dt
        if h >= lt_hr:
            at_lt += dt
        if h >= hr90:
            at_90 += dt
        peak = h if peak is None or h > peak else peak
        for name, lo, hi in bands:
            if lo <= h < hi:
                secs[name] += dt
                break
    under_cap = sum(secs[name] for name, lo, hi in bands if hi <= config.easy_hr_cap())
    return {"available": True, "total_s": total,
            "bands": [{"name": name, "lo": lo, "hi": hi, "seconds": secs[name],
                       "pct": (secs[name] / total) if total else 0.0} for name, lo, hi in bands],
            "under_easy_cap_s": under_cap, "easy_cap": config.easy_hr_cap(),
            "at_or_above_lt_s": at_lt, "lt_hr": lt_hr,
            "at_or_above_90pct_s": at_90, "hr_90pct": hr90, "peak_hr": peak,
            "max_dt": max_dt, "samples_without_hr": no_hr}


# ---------------------------------------------------------------------------
# Plan context
# ---------------------------------------------------------------------------

_QUALITY_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:mi(?:les?)?|km|k)?\s*(?:@|at)\s*(mp|marathon|hmp|half|tempo|threshold)",
    re.I)


def _run_date(a: dict) -> Optional[date]:
    try:
        return date.fromisoformat(str(a.get("start_date_local") or a.get("start_date"))[:10])
    except (TypeError, ValueError):
        return None


def plan_quality_mi(week: Optional[dict]) -> float:
    """Quality miles the week prescribes: `quality.miles` when structured, else
    'N @ MP' in the first sentence of key_workout."""
    if not week:
        return 0.0
    q = week.get("quality") or {}
    if q.get("kind") in ("mp", "tempo", "threshold") and q.get("miles"):
        return float(q["miles"])
    m = _QUALITY_RE.search(first_sentence(week.get("key_workout")))
    return float(m.group(1)) if m else 0.0


def planned_context(plan_week: Optional[dict], run_date: Optional[date]) -> Optional[dict]:
    """What the plan asked of this run, in the numbers the rubric scores."""
    if not plan_week:
        return None
    q = plan_week.get("quality") or {}
    ctx = {
        "week_num": plan_week.get("week_num"),
        "phase": plan_week.get("phase"),
        "key_workout": first_sentence(plan_week.get("key_workout")),
        "target_miles": plan_week.get("target_miles"),
        "long_run_target_mi": plan_week.get("long_run_target"),
        "long_run_time_cap_min": plan_week.get("long_run_time_cap_min"),
        "quality_mi": plan_quality_mi(plan_week),
        "quality_kind": q.get("kind"),
        "mp_pace_limit_min_mi": None,
        "mp_hr_band": tuple(config.race_pace_hr_range()),
        "role": None,
    }
    try:
        ctx["mp_pace_limit_min_mi"] = marathon_plan.goal_mp_pace() + plan_tracker.MP_PACE_TOLERANCE_MIN
    except Exception:
        try:
            ctx["mp_pace_limit_min_mi"] = (float(config.active_race()["goal_pace_min_per_mi"])
                                           + plan_tracker.MP_PACE_TOLERANCE_MIN)
        except Exception:
            pass
    if run_date is not None:
        try:
            ctx["role"] = plan_layout.role_for_date(plan_week, run_date)
        except Exception:
            ctx["role"] = None
    return ctx


def _on_feet(a: dict, lap_info: dict) -> dict:
    """Elapsed time with the wait at the start taken out: what a clock cap governs."""
    el = a.get("elapsed_time") or 0
    mt = a.get("moving_time") or 0
    if not el:
        return {"on_feet_min": mt / 60, "start_wait_s": 0, "elapsed_min": mt / 60, "estimated": True}
    laps = lap_info.get("laps") or []
    wait = laps[0]["stopped_s"] if laps and (laps[0].get("stopped_s") or 0) >= START_WAIT_MIN_S else 0
    return {"on_feet_min": (el - wait) / 60, "start_wait_s": wait, "elapsed_min": el / 60,
            "estimated": False}


# ---------------------------------------------------------------------------
# Segments and bands
# ---------------------------------------------------------------------------

def _v(dim: str, verdict: str, line: str) -> dict:
    return {"dim": dim, "verdict": verdict, "line": line}


def _pace_span(laps: list) -> str:
    ps = [lp["pace"] for lp in laps if lp.get("pace")]
    if not ps:
        return ""
    lo, hi = min(ps), max(ps)
    if abs(hi - lo) < 0.01:
        return units.fmt_pace(lo)
    return units.fmt_pace_range(lo, hi)


def _hr_span(laps: list) -> str:
    hs = [lp["avg_hr"] for lp in laps if lp.get("avg_hr")]
    if not hs:
        return "no HR"
    lo, hi = min(hs), max(hs)
    return f"HR {lo:.0f}" if abs(hi - lo) < 1 else f"HR {lo:.0f}-{hi:.0f}"


def _easy_pace_fast_edge(planned_paces: Optional[dict]) -> float:
    """Faster edge of the easy band, min/mi (plan paces first, config second)."""
    try:
        e = (planned_paces or {}).get("easy") or {}
        if e.get("min"):
            return float(e["min"])
    except Exception:
        pass
    return float((config.pace_zones().get("easy") or {}).get("ceiling", 10.0))


def assign_segments(laps_out: list, intent: Optional[dict], mp_laps: Optional[set] = None,
                    easy_cap: Optional[float] = None, easy_fast_edge: Optional[float] = None) -> list:
    """Tag each lap with the segment it belongs to (`seg`, `seg_kind`, `seg_source`).

    Order of authority: the athlete's description (placed segments by
    distance; an unplaced block "20 w/ 10 @ MP" lands on the fastest n full
    laps), else the plan's MP laps, else an undeclared block (two or more
    consecutive full laps after lap 1 run faster than the easy band AND above
    the easy cap), which is reported but never scored.
    """
    easy_cap = easy_cap or config.easy_hr_cap()
    easy_fast = easy_fast_edge or _easy_pace_fast_edge(None)
    for lp in laps_out:
        lp["seg"], lp["seg_kind"], lp["seg_source"] = None, None, None
    segs = (intent or {}).get("segments") or []
    if segs:
        pos = 0.0
        for lp in laps_out:
            mid = pos + (lp.get("dist_mi") or 0) / 2
            pos += lp.get("dist_mi") or 0
            for i, s in enumerate(segs):
                if s["placed"] and s["start_mi"] <= mid < s["end_mi"] + 1e-6:
                    lp["seg"], lp["seg_kind"], lp["seg_source"] = i, s["kind"], "description"
                    break
        for i, s in enumerate(segs):
            if s["placed"] or not s.get("miles"):
                continue
            full = [k for k, lp in enumerate(laps_out) if 0.95 <= (lp.get("dist_mi") or 0) <= 1.05]
            n = max(1, int(round(s["miles"])))
            best, best_k = None, None
            for j in range(0, max(0, len(full) - n) + 1):
                window = full[j:j + n]
                if len(window) < n:
                    break
                m = statistics.mean(laps_out[k].get("pace") or 99 for k in window)
                if best is None or m < best:
                    best, best_k = m, window
            for k in best_k or []:
                laps_out[k]["seg"], laps_out[k]["seg_kind"], laps_out[k]["seg_source"] = i, s["kind"], "description"
        for lp in laps_out:
            if lp["seg"] is None:
                lp["seg_kind"], lp["seg_source"] = "easy", "description"
        return laps_out
    if mp_laps:
        idxs = [k for k, lp in enumerate(laps_out) if lp.get("idx") in mp_laps]
        if idxs:
            for k in range(min(idxs), max(idxs) + 1):
                laps_out[k]["seg"], laps_out[k]["seg_kind"], laps_out[k]["seg_source"] = 0, "mp", "plan"
            return laps_out
    run = []
    for k, lp in enumerate(laps_out + [None]):
        hot = (lp is not None and (lp.get("idx") or 0) >= 2 and 0.95 <= (lp.get("dist_mi") or 0) <= 1.05
               and lp.get("pace") and lp["pace"] < easy_fast
               and (lp.get("avg_hr") or 0) > easy_cap)
        if hot:
            run.append(k)
            continue
        if len(run) >= 2:
            for j in run:
                laps_out[j]["seg"], laps_out[j]["seg_kind"], laps_out[j]["seg_source"] = 0, "undeclared", "undeclared"
        run = []
    return laps_out


def _pace_band(kind: str, planned_paces: Optional[dict], vdot: Optional[float] = None) -> Optional[tuple]:
    """(fast_edge, slow_edge) in min/mi for a declared segment kind."""
    pz = config.pace_zones()
    paces = planned_paces or {}

    def _from(d):
        if isinstance(d, dict) and d.get("min") and d.get("max"):
            return (float(d["min"]), float(d["max"]))
        return None

    def _cfg(key):
        z = pz.get(key) or {}
        if z.get("ceiling") and z.get("floor"):
            return (float(z["ceiling"]), float(z["floor"]))
        return None

    def _race(dist_m):
        if not vdot:
            return None
        try:
            from race_predictor import predict_race_time
            sec = predict_race_time(vdot, dist_m)
            p = (sec / 60) / (dist_m / MI_M)
            return (p * 0.97, p * 1.03)
        except Exception:
            return None

    if kind == "mp":
        mp = paces.get("marathon_pace")
        if mp:
            return (float(mp) - 0.25, float(mp) + plan_tracker.MP_PACE_TOLERANCE_MIN)
        return _cfg("race_pace")
    if kind == "tempo":
        return _from(paces.get("tempo")) or _cfg("tempo")
    if kind == "threshold":
        return _from(paces.get("threshold")) or _cfg("threshold")
    if kind == "half":
        return _race(21097.5) or _from(paces.get("tempo")) or _cfg("tempo")
    if kind == "10k":
        return _race(10000) or _from(paces.get("threshold")) or _cfg("threshold")
    if kind == "5k":
        r = _race(5000)
        if r:
            return r
        t = _from(paces.get("vo2max")) or _cfg("threshold")
        return (t[0] - 0.3, t[1] - 0.3) if t else None
    if kind == "steady":
        easy_fast = _easy_pace_fast_edge(paces)
        mp = paces.get("marathon_pace")
        slow = easy_fast
        fast = float(mp) if mp else easy_fast - 0.75
        return (fast, slow)
    return None


def _hr_band(kind: str) -> Optional[tuple]:
    pz = config.pace_zones()
    if kind == "mp":
        return tuple(config.race_pace_hr_range())
    if kind == "tempo":
        r = (pz.get("tempo") or {}).get("hr_range")
        return tuple(r) if r else None
    if kind == "threshold":
        r = (pz.get("threshold") or {}).get("hr_range")
        return tuple(r) if r else None
    if kind == "half":
        lo = (pz.get("tempo") or {}).get("hr_range", [None, None])[0]
        hi = (pz.get("threshold") or {}).get("hr_range", [None, None])[1]
        return (lo, hi) if lo and hi else None
    if kind in ("10k", "5k"):
        lo = (pz.get("threshold") or {}).get("hr_range", [None, None])[0]
        return (lo, config.max_hr()) if lo else None
    return None


def _inner_stops(seg_laps: list, all_laps: list, stream_stops_: dict) -> list:
    pos, start, end = 0.0, None, None
    for lp in all_laps:
        if lp in seg_laps and start is None:
            start = pos
        pos += lp.get("dist_mi") or 0
        if lp in seg_laps:
            end = pos
    if start is None:
        return []
    if stream_stops_.get("available"):
        return [st["duration_s"] for st in stream_stops_.get("stops") or []
                if st.get("at_mi") is not None and start + 0.15 < st["at_mi"] < end - 0.15
                and st["duration_s"] >= STOP_MIN_S]
    return [lp["stopped_s"] for lp in seg_laps[1:] if (lp.get("stopped_s") or 0) >= STOP_MIN_S]


# ---------------------------------------------------------------------------
# Verdicts
# ---------------------------------------------------------------------------

def _easy_verdict(ctx: dict, classification: str, a: dict) -> dict:
    laps_out = ctx.get("laps_out") or []
    cap = config.long_run_hr_cap() if classification == "LONG RUN" else config.easy_hr_cap()
    label = "long-run cap" if classification == "LONG RUN" else "easy cap"
    easy = [lp for lp in laps_out if lp.get("seg_kind") in (None, "easy")
            and 0.95 <= (lp.get("dist_mi") or 0) and (lp.get("idx") or 0) >= 2 and lp.get("avg_hr")]
    bands = ctx.get("bands") or {}
    if easy:
        under = [lp for lp in easy if lp["avg_hr"] <= cap]
        share = len(under) / len(easy)
        verdict = "ok" if share >= 0.9 else ("watch" if share >= 0.7 else "miss")
        line = (f"{len(under)} of {len(easy)} easy splits under the {label} {cap:.0f} "
                f"({_hr_span(easy)}, {_pace_span(easy)})")
        if verdict != "ok":
            over = [str(lp["idx"]) for lp in easy if lp["avg_hr"] > cap]
            line += f"; over the cap on split {', '.join(over)}"
        if bands.get("available") and bands.get("total_s"):
            line += f"; {bands['under_easy_cap_s'] / bands['total_s']:.0%} of moving time under {bands['easy_cap']:.0f}"
        elif not bands.get("available"):
            line += "; time in zones n/a without a stream"
        return _v("easy", verdict, line + ".")
    avg = a.get("average_heartrate")
    if avg:
        if classification in ("INTERVALS", "TEMPO", "THRESHOLD"):
            return _v("easy", "n/a", "A quality session: the easy read is the warm-up and cool-down, "
                                     "which the laps did not separate.")
        if avg <= cap:
            verdict = "ok"
        elif avg <= cap + 5:
            verdict = "watch"
        else:
            verdict = "miss"
        return _v("easy", verdict, f"Average HR {avg:.0f} vs the {label} {cap:.0f} (from the run "
                                   "average: no laps).")
    return _v("easy", "n/a", "No heart rate recorded.")


def _quality_verdicts(ctx: dict, vdot: Optional[float] = None) -> list:
    laps_out = ctx.get("laps_out") or []
    paces = ctx.get("paces") or {}
    out = []
    groups = {}
    for lp in laps_out:
        if lp.get("seg_kind") not in (None, "easy"):
            groups.setdefault((lp["seg"], lp["seg_kind"], lp["seg_source"]), []).append(lp)
    for (_, kind, source), seg_laps in groups.items():
        span = (f"splits #{seg_laps[0]['idx']}-{seg_laps[-1]['idx']}" if len(seg_laps) > 1
                else f"split #{seg_laps[0]['idx']}")
        shown = f"{_pace_span(seg_laps)}, {_hr_span(seg_laps)}"
        if kind == "undeclared":
            out.append(_v("quality", "info",
                          f"{span} ran faster than easy ({shown}) with no stated intent. If it was "
                          "planned, say so in the description and it gets scored."))
            continue
        if kind == "mp":
            mp_s = ctx.get("mp")
            q = ctx.get("mp_quality_mi") or 0
            if not mp_s or not q:
                out.append(_v("quality", "n/a", f"Marathon pace {span}: no laps to read it from."))
                continue
            lo, hi = mp_s["hr_band"]
            got, at_pace = mp_s["in_band_mi"], mp_s["at_pace_mi"]
            limit = units.fmt_pace(mp_s["pace_limit_min_mi"])
            longest = mp_s["longest_in_band_mi"]
            if got >= q:
                verdict = "ok"
                line = (f"{_dist(got)} of {_dist(q)} at marathon pace inside HR {lo}-{hi} "
                        f"(pace <= {limit}); longest in-band stretch {_dist(longest)}.")
            else:
                verdict = "watch" if got >= 0.5 * q else "miss"
                line = (f"{_dist(got)} of {_dist(q)} at marathon pace inside HR {lo}-{hi}: "
                        f"{_dist(at_pace)} ran at the pace (<= {limit}) but "
                        f"{_dist(mp_s['over_band_mi'])} of them sat above {hi}; longest in-band "
                        f"stretch {_dist(longest)}. Pace right, HR above the band is a hot day or "
                        "too fast for today; either way it is not controlled marathon work.")
            out.append(_v("quality", verdict, line))
            continue
        if kind == "progression":
            n = max(1, len(seg_laps) // 3)
            first = statistics.mean(lp["pace"] for lp in seg_laps[:n] if lp.get("pace"))
            last = statistics.mean(lp["pace"] for lp in seg_laps[-n:] if lp.get("pace"))
            ok = (first - last) * 60 >= 10
            out.append(_v("quality", "ok" if ok else "watch",
                          f"Progression {span}: {units.fmt_pace(first)} -> {units.fmt_pace(last)} "
                          f"({(first - last) * 60:+.0f} s), {_hr_span(seg_laps)}."))
            continue
        band = _pace_band(kind, paces, vdot)
        paces_in = [lp["pace"] for lp in seg_laps if lp.get("pace")]
        if not band or not paces_in:
            out.append(_v("quality", "n/a", f"{kind} {span}: no pace band to judge against."))
            continue
        fast, slow = band
        med = statistics.median(paces_in)
        hrs = [lp["avg_hr"] for lp in seg_laps if lp.get("avg_hr")]
        hr_med = statistics.median(hrs) if hrs else None
        hr_band = _hr_band(kind)
        if fast <= med <= slow:
            verdict, word = "ok", "inside"
        elif fast - SEGMENT_PACE_SLACK_SEC / 60 <= med <= slow + SEGMENT_PACE_SLACK_SEC / 60:
            verdict, word = "watch", ("just faster than" if med < fast else "just slower than")
        else:
            verdict, word = "miss", ("faster than" if med < fast else "slower than")
        tgt = units.fmt_pace_range(fast, slow)
        line = f"{kind} {span}: {shown}, {word} the {kind} band ({tgt})"
        if verdict == "ok" and hr_band and hr_med is not None and hr_band[1] and hr_med > hr_band[1]:
            verdict = "watch"
            line += f"; pace right, HR {hr_med:.0f} above the band's {hr_band[1]:.0f}"
        if kind == "half" and med < fast:
            line += ": closer to 10k effort than half-marathon effort"
        inner = _inner_stops(seg_laps, laps_out, ctx.get("stream_stops") or {})
        if inner:
            line += (f". Ran as {len(inner) + 1} pieces with {_fmt_hms(sum(inner))} standing "
                     "between: a rep session, not a continuous block")
            if verdict == "ok":
                verdict = "watch"
        out.append(_v("quality", verdict, line + "."))
    return out


def _finish_verdict(ctx: dict) -> dict:
    laps_out = ctx.get("laps_out") or []
    lap_info = ctx.get("lap_info") or {}
    declared = [lp for lp in laps_out if lp.get("seg_kind") not in (None, "easy", "undeclared")]
    dec = lap_info.get("decoupling") or {}
    drift_txt = ""
    if dec.get("available"):
        drift_txt = (f" Pace-matched HR: {dec['median_delta']:+.0f} bpm late vs early "
                     f"({dec['verdict'].split(':')[0].lower()}).")
    if declared and (ctx.get("intent") or {}).get("segments"):
        last = max(laps_out.index(lp) for lp in declared)
        after = [lp for lp in laps_out[last + 1:] if 0.95 <= (lp.get("dist_mi") or 0) <= 1.05 and lp.get("pace")]
        opening = [lp for lp in laps_out if 2 <= (lp.get("idx") or 0) <= 4
                   and lp.get("seg_kind") == "easy" and lp.get("pace")]
        if not after or not opening:
            return _v("finish", "n/a", "No easy splits after the declared segment to read." + drift_txt)
        d = (statistics.mean(lp["pace"] for lp in after) - statistics.mean(lp["pace"] for lp in opening)) * 60
        line = (f"After the declared work you ran {_pace_span(after)} vs {_pace_span(opening)} in the "
                f"opening splits ({d:+.0f} s)")
        if d >= FADE_SEC:
            return _v("finish", "miss", line + ": a fade after the quality, not a cool-down." + drift_txt)
        if d >= SURGE_FASTER_SEC:
            return _v("finish", "watch", line + "." + drift_txt)
        return _v("finish", "ok", line + ": held." + drift_txt)
    if not lap_info.get("laps"):
        return _v("finish", "n/a", "No laps.")
    surge = lap_info.get("surge") or {}
    fade_sec = lap_info.get("fade_sec")
    if surge.get("verdict") == "surge_then_fade":
        last_fast = max((laps_out.index(lp) for lp in laps_out if lp.get("seg_kind") == "undeclared"),
                        default=None)
        after = [lp for lp in laps_out[(last_fast or 0) + 1:] if (lp.get("dist_mi") or 0) >= 0.5]
        if last_fast is not None and after and all((lp.get("avg_hr") or 999) <= config.easy_hr_cap() for lp in after):
            return _v("finish", "watch",
                      f"After the fast splits you came back under the easy cap ({_pace_span(after)}, "
                      f"{_hr_span(after)}): a cool-down if you meant it, a fade if you didn't. Put "
                      "the plan in the description and this reads properly." + drift_txt)
        return _v("finish", "miss", f"Fast splits then a fade: {'; '.join(surge['reasons'])}. "
                                    "Over-spent rather than a progression; confirm with the athlete." + drift_txt)
    if fade_sec is not None and fade_sec > FADE_SEC:
        return _v("finish", "miss", f"Faded {fade_sec:.0f} s per split over the closing splits." + drift_txt)
    verdict = "ok"
    if dec.get("available") and dec["median_delta"] > REAL_DRIFT_BPM:
        verdict = "watch"
    if surge.get("present"):
        return _v("finish", verdict, f"Late surge at {', '.join('#' + str(m) for m in surge['miles'])} "
                                     "held to the finish." + drift_txt)
    if len(_full_laps(lap_info["laps"], unit_m=lap_info.get("split_unit_m"))) < 4:
        return _v("finish", "n/a", "Too few full splits to read a fade." + drift_txt)
    return _v("finish", verdict, "No fade over the closing splits." + drift_txt)


def _cap_verdict(ctx: dict, dist_mi: float) -> dict:
    planned = ctx.get("planned") or {}
    on_feet = ctx.get("on_feet") or {}
    cap_min = planned.get("long_run_time_cap_min")
    tgt = planned.get("long_run_target_mi")
    feet = on_feet.get("on_feet_min")
    if not cap_min or not tgt or not feet:
        return _v("cap", "n/a", "No clock cap for this run.")
    if abs(dist_mi - float(tgt)) / float(tgt) > CAP_DISTANCE_TOL:
        return _v("cap", "n/a", f"The {_fmt_hm(float(cap_min))} cap belongs to the plan's "
                                f"{_dist(float(tgt))} long run; this was {_dist(dist_mi)}, so it "
                                f"doesn't apply. On feet {_fmt_hm(feet)}.")
    wait = on_feet.get("start_wait_s") or 0
    wait_txt = f" after the {_fmt_hms(wait)} wait at the start" if wait else ""
    if feet > float(cap_min):
        return _v("cap", "watch", f"{_fmt_hm(feet)} on feet{wait_txt} against a "
                                  f"{_fmt_hm(float(cap_min))} cap (+{feet - float(cap_min):.0f} min). "
                                  "The cap is what ends a bad day.")
    return _v("cap", "ok", f"{_fmt_hm(feet)} on feet{wait_txt}, inside the {_fmt_hm(float(cap_min))} cap.")


def _load_verdict(load_ctx: Optional[dict], a: dict) -> dict:
    tss = (load_ctx or {}).get("tss")
    if tss is None:
        tss = a.get("_run_tss")
    if tss is None:
        return _v("load", "n/a", "No training load computed for this run.")
    src = "HR-based" if a.get("average_heartrate") else "pace-based"
    line = f"{tss:.0f} TSS ({src})"
    lc = load_ctx or {}
    if lc.get("tsb_before") is not None and lc.get("tsb_after") is not None:
        line += (f"; form (TSB) {lc['tsb_before']:+.0f} going in, {lc['tsb_after']:+.0f} the next "
                 f"morning; fitness (CTL) {lc.get('ctl') or 0:.0f}")
        if lc.get("history_sufficient") is False:
            line += (f". Only {lc.get('history_days', 0)}d of history loaded: CTL is understated, "
                     "do not read fatigue off these")
    return _v("load", "info", line + ".")


def _sensor_verdict(hrq: dict) -> dict:
    if not hrq or not hrq.get("available"):
        return _v("sensor", "n/a", "No stream: wrist-HR checks not run.")
    if hrq.get("flag"):
        return _v("sensor", "info", hrq["flag"] + " " + " ".join(hrq.get("notes") or []))
    if hrq.get("notes"):
        return _v("sensor", "info", " ".join(hrq["notes"]))
    return _v("sensor", "info", "HR trace passed the checks: no slow re-acquisition after stops, "
                                "no cadence lock.")


def _stops_verdict(ctx: dict) -> Optional[dict]:
    st = (ctx.get("lap_info") or {}).get("stops") or {}
    if not st.get("available"):
        s_stops = ctx.get("stream_stops") or {}
        if s_stops.get("available"):
            return _v("stops", "info", f"{s_stops['n']} stop(s) of {s_stops['min_gap_s']}s+ in the "
                                       f"stream, {_fmt_hms(s_stops['total_s'])} in total.")
        return _v("stops", "info", "Stops: from the run's moving vs elapsed time only (no lap detail).")
    first_idx = ((ctx.get("lap_info") or {}).get("laps") or [{}])[0].get("idx")
    mid = [d for d in st["diluted"] if d["idx"] != first_idx]
    ratio = st.get("moving_ratio")
    if mid or (ratio is not None and ratio < MIN_LAP_MOVING_RATIO):
        which = ", ".join(f"split #{d['idx']} {d['moving_ratio']:.0%}" for d in mid)
        return _v("stops", "info", f"{len(mid)} split(s) under {MIN_LAP_MOVING_RATIO:.0%} moving "
                                   f"({which}); {_fmt_hms(st['stopped_s'])} stopped, "
                                   f"{ratio:.0%} moving overall.")
    return _v("stops", "info", f"Continuous: {ratio:.0%} moving.")


def session_verdicts(a: dict, classification: str, ctx: dict) -> list:
    """One line per dimension; grade_from turns the scored ones into a grade."""
    dist_mi = (a.get("distance", 0) or 0) / MI_M
    planned = ctx.get("planned") or {}
    intent = ctx.get("intent")
    out = []

    # Intent: what you said, else what the plan said.
    if intent:
        stated = sum(s.get("miles") or 0 for s in intent["segments"] if s["placed"]) or dist_mi
        out.append(_v("intent", "miss" if dist_mi < 0.9 * stated else "ok",
                      f"Ran {_dist(dist_mi)} as you described it: {session_intent.summary(intent)}."))
        if planned:
            pl = f"wk{planned.get('week_num')}: {planned.get('key_workout') or 'no key workout'}"
            if planned.get("long_run_target_mi"):
                pl += f" (long run {_dist(float(planned['long_run_target_mi']))})"
            out.append(_v("plan", "info", pl + ". Judged against what you said, not the plan."))
    elif classification == "LONG RUN" and planned.get("long_run_target_mi"):
        tgt = float(planned["long_run_target_mi"])
        pct = dist_mi / tgt if tgt else 0
        if pct < NOT_THE_LONG_RUN_PCT:
            out.append(_v("intent", "n/a", f"{_dist(dist_mi)} is not the week's {_dist(tgt)} long run "
                                           f"({pct:.0%}); judged as a run of its own."))
        elif pct < LONG_RUN_DISTANCE_PCT:
            out.append(_v("intent", "miss", f"{_dist(dist_mi)} of {_dist(tgt)} planned ({pct:.0%}). "
                                            f"Under {LONG_RUN_DISTANCE_PCT:.0%}: the long run is the week."))
        else:
            out.append(_v("intent", "ok", f"{_dist(dist_mi)} of {_dist(tgt)} planned ({pct:.0%})."))
    elif planned.get("role") in ("easy", "key", "long") and planned.get("key_workout"):
        out.append(_v("plan", "info", f"wk{planned.get('week_num')} {planned['role']} day: "
                                      f"{planned['key_workout']}"))
        out.append(_v("intent", "n/a", "No stated session; judged on its own terms."))
    else:
        out.append(_v("intent", "n/a", "No stated or planned session to hold this run to."))

    out.append(_easy_verdict(ctx, classification, a))
    out += _quality_verdicts(ctx, ctx.get("vdot")) or [_v("quality", "n/a", "No quality declared or planned.")]
    out.append(_finish_verdict(ctx))
    if classification == "LONG RUN":
        out.append(_cap_verdict(ctx, dist_mi))
    out.append(_load_verdict(ctx.get("load"), a))
    out.append(_sensor_verdict(ctx.get("hrq") or {}))
    st = _stops_verdict(ctx)
    if st:
        out.append(st)
    return out


def _interval_verdicts(a: dict, reps: Optional[dict], ctx: dict) -> list:
    """A rep session is judged on its reps: quality = reps done at full
    distance, finish = rep fade. Laps are auto-splits and are not read."""
    out = []
    planned = ctx.get("planned") or {}
    if planned.get("key_workout"):
        out.append(_v("plan", "info", f"wk{planned.get('week_num')}: {planned['key_workout']}"))
    if reps and reps.get("prescribed"):
        want = reps["prescribed"]
        done = reps.get("completed_full", reps["completed"])
        if done >= want:
            out.append(_v("quality", "ok", f"Completed all {want} reps at full distance."))
        else:
            verdict = "watch" if done >= want - 1 and (reps.get("work_pct") or 0) >= 85 else "miss"
            out.append(_v("quality", verdict,
                          f"{done} of {want} reps at full distance ({reps.get('work_pct') or 0:.0f}% of "
                          "the prescribed work). The target pace was set above what the session could hold."))
        f = reps.get("fade")
        if f and f["delta_s"] >= 15:
            out.append(_v("finish", "miss", f"Closing reps {f['delta_s']:.0f} s slower than the best. "
                                            "Start slower and the whole set lands."))
        elif f and f["delta_s"] >= 8:
            out.append(_v("finish", "watch", f"Closing reps {f['delta_s']:.0f} s slower than the best."))
        else:
            out.append(_v("finish", "ok", "Reps held their pace to the end."))
        pm = reps.get("pace_matched")
        if pm and pm.get("hr_end_delta") is not None:
            out.append(_v("fatigue", "info", f"Rep {pm['early_idx']} vs rep {pm['late_idx']} at the same "
                                             f"pace: {pm['hr_end_delta']:+d} bpm at the end. That delta is "
                                             "fatigue, nothing else."))
    elif reps:
        out.append(_v("quality", "info", f"{reps['completed']} reps found in the stream, no prescription "
                                         "to score them against. Put '8 x 800' in the title and they get scored."))
        out.append(_v("finish", "n/a", "No prescription."))
    else:
        out.append(_v("quality", "n/a", "Reps live in the stream; the laps are auto-splits. Fetch the "
                                        "stream and the reps get scored."))
        out.append(_v("finish", "n/a", "No stream."))
    out.append(_load_verdict(ctx.get("load"), a))
    out.append(_sensor_verdict(ctx.get("hrq") or {}))
    return out


def grade_from(verdicts: list) -> tuple:
    score = 100
    for v in verdicts:
        if v["dim"] in SCORED_DIMS:
            score -= VERDICT_DEDUCT.get(v["verdict"], 0)
    score = max(0, score)
    grade = "A" if score >= 90 else "B" if score >= 75 else "C" if score >= 60 else "D"
    notes = [f"[{_MARK.get(v['verdict'], ' ')}] {v['dim'].title()}: {v['line']}" for v in verdicts]
    return score, grade, notes


def execution_score(a: dict, classification: str, reps: dict = None,
                    ctx: Optional[dict] = None) -> tuple:
    """(score, grade, notes). Rep sessions are judged on their reps; every other
    run on session_verdicts."""
    ctx = ctx or {}
    if classification == "INTERVALS":
        return grade_from(_interval_verdicts(a, reps, ctx))
    verdicts = ctx.get("verdicts")
    if verdicts is None:
        verdicts = session_verdicts(a, classification, ctx)
    return grade_from(verdicts)


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

def classify_session(a: dict, intent: Optional[dict], plan_role: Optional[str] = None,
                     lap_info: Optional[dict] = None) -> tuple:
    """(label, source). Stated intent first, then the rep-session shape, then
    the plan's day role, then the shared HR/pace classifier."""
    dist_mi = (a.get("distance", 0) or 0) / MI_M
    mt_min = (a.get("moving_time", 0) or 0) / 60
    pace = mt_min / dist_mi if dist_mi > 0 else 0
    if pace >= 13:
        return "WALK / SHAKEOUT", "pace"
    if not intent and looks_like_intervals(a):
        return "INTERVALS", "shape"
    if intent:
        if dist_mi >= config.long_run_min_mi():
            return "LONG RUN", "intent"
        return ("TEMPO" if session_intent.quality_segments(intent) else "EASY"), "intent"
    if plan_role == "long" and dist_mi >= NOT_THE_LONG_RUN_PCT * config.long_run_min_mi():
        return "LONG RUN", "plan"
    if plan_role == "key":
        kind = enrichment.classify_run(a)
        if kind in ("tempo", "threshold", "intervals"):
            return LABELS[kind], "plan"
    return classify_run(a), "hr_pace"


def review(activity: dict, streams: Optional[dict] = None, plan_week: Optional[dict] = None,
           laps: Optional[list] = None, lookup_plan: bool = True, load_ctx: Optional[dict] = None,
           vdot: Optional[float] = None, debug: bool = False) -> dict:
    """Everything the review knows about one run, as data. Pure: no I/O."""
    a = activity
    run_date = _run_date(a)
    dist_mi = (a.get("distance", 0) or 0) / MI_M
    mt_min = (a.get("moving_time", 0) or 0) / 60
    el_min = (a.get("elapsed_time", 0) or 0) / 60
    pace = mt_min / dist_mi if dist_mi > 0 else 0

    intent = None
    for field in ("description", "name"):
        if a.get(field):
            intent = session_intent.parse_intent(a[field], dist_mi)
            if intent:
                break

    laps_raw = laps if laps is not None else (a.get("laps") or [])
    lap_info = lap_breakdown(laps_raw)
    norm = intervals.normalize_streams(streams) if streams else {}
    t_span = (norm.get("time") or [0])[-1] - (norm.get("time") or [0])[0] if norm else 0
    spans = bool(norm) and (not a.get("elapsed_time") or t_span >= 0.9 * a["elapsed_time"])
    hrq = (hr_quality.hr_quality(norm, lap_info.get("laps") if spans else None)
           if norm else {"available": False, "notes": [], "unreliable": False, "flag": None})
    for lp in lap_info.get("laps") or []:
        lp["strava_hr"] = lp.get("avg_hr")
        alt = (hrq.get("lap_hr") or {}).get(lp.get("idx"))
        if alt is not None and lp.get("avg_hr"):
            lp["avg_hr"] = alt

    if plan_week is None and lookup_plan and run_date is not None:
        try:
            plan_week = marathon_plan.current_week(run_date) if marathon_plan.has_plan() else None
        except Exception:
            plan_week = None
    planned = planned_context(plan_week, run_date)
    paces = {}
    if plan_week is not None:
        try:
            paces = marathon_plan.paces() if marathon_plan.has_plan() else {}
        except Exception:
            paces = {}

    cls, cls_source = classify_session(a, intent, (planned or {}).get("role"), lap_info)
    lap_reads_valid = cls != "INTERVALS"
    laps_out = lap_info.get("laps") or []

    bands = time_in_hr_bands(norm) if norm else {"available": False, "reason": "no stream"}
    s_stops = stream_stops(norm) if norm else {"available": False, "reason": "no stream"}
    rep_lines, rep_comp = ([], None)
    if streams and cls == "INTERVALS":
        rep_lines, rep_comp = rep_table_for(a, streams, debug=debug, plan_week=plan_week)

    # Marathon-pace read: the declared MP block, else the plan's prescription.
    mp_s, mp_q, mp_laps = None, 0.0, None
    stated_mp = [sg for sg in (intent or {}).get("segments", []) if sg["kind"] == "mp"]
    if planned and planned.get("mp_pace_limit_min_mi") and laps_raw and lap_reads_valid and intent is None \
            and (planned.get("quality_kind") == "mp" or planned.get("quality_mi")):
        mp_s = plan_tracker.mp_lap_summary(laps_raw, planned["mp_pace_limit_min_mi"], planned["mp_hr_band"])
        mp_q = float(planned.get("quality_mi") or 0)
        if mp_q:
            mp_laps = {x["idx"] for x in mp_s["laps"]}
    assign_segments(laps_out, intent, mp_laps, easy_fast_edge=_easy_pace_fast_edge(paces))
    if stated_mp and laps_raw and lap_reads_valid:
        idx = {lp["idx"] for lp in laps_out if lp.get("seg_kind") == "mp"}
        limit = (planned or {}).get("mp_pace_limit_min_mi")
        if limit is None:
            try:
                limit = float(config.active_race()["goal_pace_min_per_mi"]) + plan_tracker.MP_PACE_TOLERANCE_MIN
            except Exception:
                limit = 99.0
        mp_s = plan_tracker.mp_lap_summary([lp for lp in laps_raw if lp.get("lap_index") in idx],
                                           limit, tuple(config.race_pace_hr_range()))
        mp_q = sum(sg.get("miles") or 0 for sg in stated_mp)

    declared = [lp for lp in laps_out if lp.get("seg_source") == "description"
                and lp.get("seg_kind") not in (None, "easy")]
    if declared and lap_reads_valid:
        lap_info["surge"] = {"present": False, "suppressed": "declared quality segment"}
        lap_info["fade_note"] = None
        lap_info["pattern"] = (f"declared {declared[0]['seg_kind']} at splits #{declared[0]['idx']}-"
                               f"{declared[-1]['idx']} (see VERDICTS); split pattern not read")

    on_feet = _on_feet(a, lap_info)
    ctx = {"planned": planned, "paces": paces, "lap_info": lap_info if lap_reads_valid else {},
           "laps_out": laps_out if lap_reads_valid else [], "intent": intent,
           "mp": mp_s, "mp_quality_mi": mp_q, "bands": bands, "on_feet": on_feet,
           "hrq": hrq, "stream_stops": s_stops, "load": load_ctx, "vdot": vdot}
    verdicts = _interval_verdicts(a, rep_comp, ctx) if cls == "INTERVALS" else session_verdicts(a, cls, ctx)
    ctx["verdicts"] = verdicts
    score, grade, notes = grade_from(verdicts)

    return {
        "schema": 1,
        "activity": {
            "id": a.get("id"), "name": a.get("name", ""), "description": a.get("description"),
            "date": run_date.isoformat() if run_date else None,
            "weekday": run_date.strftime("%a") if run_date else None,
            "distance_m": a.get("distance"), "distance_mi": dist_mi,
            "moving_s": a.get("moving_time"), "elapsed_s": a.get("elapsed_time"),
            "moving_min": mt_min, "elapsed_min": el_min, "pace_min_mi": pace,
            "avg_hr": a.get("average_heartrate"), "max_hr": a.get("max_heartrate"),
            "unit": units.unit(),
        },
        "classification": cls,
        "classification_source": cls_source,
        "lap_reads_valid": lap_reads_valid,
        "planned": planned,
        "intent": intent,
        "executed": {"distance_mi": dist_mi, "moving_min": mt_min, "elapsed_min": el_min,
                     "pace_min_mi": pace, "mp": mp_s, "on_feet": on_feet,
                     "stopped_s": (lap_info.get("stops") or {}).get("stopped_s"),
                     "under_easy_cap_s": bands.get("under_easy_cap_s") if bands.get("available") else None},
        "laps": lap_info,
        "stream": {"available": bool(norm), "stops": s_stops, "bands": bands,
                   "hr_quality": {k: hrq.get(k) for k in ("available", "unreliable", "flag",
                                                          "slow_reacq", "lock", "notes")}},
        "reps": {"lines": rep_lines, "compliance": rep_comp},
        "load": load_ctx,
        "verdicts": verdicts,
        "score": {"score": score, "grade": grade, "notes": notes},
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _wrap(label: str, text: str, width: int = 70) -> str:
    head = f"  {label:<14} "
    return textwrap.fill(text, width=width, initial_indent=head, subsequent_indent=" " * len(head))


def render(r: dict) -> None:
    """Print a review() dict."""
    act = r["activity"]
    planned = r.get("planned") or {}
    ex = r["executed"]
    lap_info = r["laps"]
    lap_reads_valid = r["lap_reads_valid"]
    sc = r["score"]
    stream = r.get("stream") or {}

    print()
    print("=" * 70)
    header = f"  POST-RUN REVIEW  |  {act['date']}"
    if act.get("weekday"):
        header += f" ({act['weekday']})"
    if planned.get("week_num"):
        header += f"  |  plan wk{planned['week_num']} {planned.get('phase') or ''}".rstrip()
    print(header)
    print("=" * 70)
    print()
    print(f"  {act['name']}")
    clock = f"{_fmt_hms(act['moving_min'] * 60)} moving"
    if act["elapsed_min"]:
        clock += f" / {_fmt_hms(act['elapsed_min'] * 60)} elapsed"
    avg = f"{act['avg_hr']:.0f}" if act.get("avg_hr") else "N/A"
    print(f"  {units.fmt_dist(mi=act['distance_mi'], nd=2)}  |  {clock}  |  {_fmt_pace(act['pace_min_mi'])}"
          f"  |  HR avg {avg}, max {act.get('max_hr') or 'N/A'}")
    print()
    print(f"  Classification:    {r['classification']}  (from {r.get('classification_source', '?')})")
    print(f"  Execution score:   {sc['grade']} ({sc['score']}/100)")
    print(f"  Data:              laps {'yes' if lap_info.get('laps') else 'no'}, "
          f"stream {'yes' if stream.get('available') else 'no'}")
    if r.get("intent"):
        print(f"  You said:          {session_intent.summary(r['intent'])}  (from the description)")
    flag = (stream.get("hr_quality") or {}).get("flag")
    if flag:
        print(f"  ! {flag}")
    print()

    if planned:
        print("-" * 70)
        print("  PLANNED vs EXECUTED")
        print("-" * 70)
        if planned.get("key_workout"):
            print(_wrap(f"Plan (wk{planned['week_num']}):", planned["key_workout"]))
        bits = []
        if planned.get("role"):
            bits.append(f"{planned['role']} day")
        if planned.get("long_run_target_mi"):
            bits.append(f"long run {_dist(float(planned['long_run_target_mi']))}")
        if planned.get("long_run_time_cap_min"):
            bits.append(f"cap {_fmt_hm(planned['long_run_time_cap_min'])}")
        if planned.get("quality_mi"):
            bits.append(f"{_dist(planned['quality_mi'])} quality")
        if planned.get("mp_pace_limit_min_mi") and (planned.get("quality_mi")
                                                    or planned.get("quality_kind") == "mp"):
            lo, hi = planned["mp_hr_band"]
            bits.append(f"marathon pace = <= {_fmt_pace(planned['mp_pace_limit_min_mi'])} at HR {lo}-{hi}")
        if bits:
            print(_wrap("", "; ".join(bits)))
        if planned.get("long_run_target_mi") and r["classification"] == "LONG RUN":
            tgt = float(planned["long_run_target_mi"])
            print(f"  {'Distance:':<14} {_dist(ex['distance_mi'])} of {_dist(tgt)} "
                  f"({ex['distance_mi'] / tgt:.0%})")
        mp_s = ex.get("mp")
        if mp_s and (planned.get("quality_mi") or mp_s["at_pace_mi"] >= 1.0):
            lo, hi = mp_s["hr_band"]
            print(_wrap("MP splits:",
                        f"{_dist(mp_s['at_pace_mi'])} at pace (<= {_fmt_pace(mp_s['pace_limit_min_mi'])}) "
                        f"| {_dist(mp_s['in_band_mi'])} in band {lo}-{hi} | {_dist(mp_s['over_band_mi'])} over "
                        f"| longest in-band {_dist(mp_s['longest_in_band_mi'])} "
                        f"(planned {_dist(planned.get('quality_mi') or 0)})"))
        feet = ex.get("on_feet") or {}
        cap_applies = any(v["dim"] == "cap" and v["verdict"] != "n/a" for v in r.get("verdicts") or [])
        if planned.get("long_run_time_cap_min") and feet.get("on_feet_min") and cap_applies:
            cap = float(planned["long_run_time_cap_min"])
            wait = feet.get("start_wait_s") or 0
            wait_txt = f" (after the {_fmt_hms(wait)} wait at the start)" if wait else ""
            print(_wrap("Time on feet:", f"{_fmt_hm(feet['on_feet_min'])}{wait_txt} vs "
                                          f"{_fmt_hm(cap)} cap -> {feet['on_feet_min'] - cap:+.0f} min"))
        print()

    rep_lines = (r.get("reps") or {}).get("lines") or []
    if rep_lines:
        print("-" * 70)
        print("  INTERVAL BREAKDOWN (reconstructed from the stream)")
        print("-" * 70)
        for ln in rep_lines:
            print(ln)
        print()

    if lap_info.get("laps"):
        print("-" * 70)
        print("  LAP BREAKDOWN" if lap_reads_valid
              else "  LAP BREAKDOWN (auto splits: reps + recoveries blended)")
        print("-" * 70)
        for lap in lap_info["laps"][:40]:
            hr_str = f"HR {lap['avg_hr']:.0f}" if lap.get("avg_hr") else "no HR"
            if lap.get("strava_hr") and lap.get("avg_hr") and abs(lap["strava_hr"] - lap["avg_hr"]) >= 3:
                hr_str += f" (Strava lap {lap['strava_hr']:.0f})"
            pace_str = _fmt_pace(lap["pace"]) if lap["pace"] > 0 else "N/A"
            seg = f" [{lap['seg_kind']}]" if lap.get("seg_kind") not in (None, "easy") else ""
            stop_str = (f" | stopped {_fmt_hms(lap['stopped_s'])}" if (lap.get("stopped_s") or 0) >= 30 else "")
            print(f"  Lap {lap['idx']}: {units.fmt_dist(mi=lap['dist_mi'], nd=2):>8} @ {pace_str:>9}"
                  f" | {hr_str}{seg}{stop_str}")
        print()
        if lap_reads_valid:
            print(f"  Pattern: {lap_info['pattern']}")
            if lap_info.get("fade_note"):
                print(f"  ! {lap_info['fade_note']}")
            surge = lap_info.get("surge") or {}
            if surge.get("present"):
                mark = "!" if surge.get("verdict") == "surge_then_fade" else "+"
                print(textwrap.fill(surge["note"], width=70, initial_indent=f"  {mark} ",
                                    subsequent_indent="    "))
            dec = lap_info.get("decoupling") or {}
            if dec.get("available"):
                print(textwrap.fill(
                    f"  Pace-matched decoupling: {dec['median_delta']:+.0f} bpm median over "
                    f"{dec['n_pairs']} pair(s), worst {dec['max_delta']:+.0f} bpm "
                    f"(split #{dec['worst']['early_mile']} -> #{dec['worst']['late_mile']}). {dec['verdict']}.",
                    width=70, subsequent_indent="    "))
                if dec.get("outlier_note"):
                    print(textwrap.fill("  " + dec["outlier_note"], width=70, subsequent_indent="    "))
            print()

    lstops = lap_info.get("stops") or {}
    s_stops = stream.get("stops") or {}
    if lstops.get("available") or (s_stops.get("available") and s_stops.get("n")):
        print("-" * 70)
        print("  STOPS")
        print("-" * 70)
        if lstops.get("available"):
            clock = f" of {_fmt_hms(act['elapsed_min'] * 60)}" if act["elapsed_min"] else ""
            line = (f"  Clock stopped {_fmt_hms(lstops['stopped_s'])}{clock} "
                    f"({lstops['moving_ratio']:.0%} moving); {lstops['n_diluted']} split(s) "
                    f"under {lstops['min_ratio']:.0%} moving")
            if lstops["diluted"]:
                line += ": " + ", ".join(f"#{d['idx']} {d['moving_ratio']:.0%}" for d in lstops["diluted"])
            print(textwrap.fill(line, width=70, subsequent_indent="    "))
        if s_stops.get("available") and s_stops.get("n"):
            print(f"  {s_stops['n']} stop(s) of {s_stops['min_gap_s']}s+ in the stream, "
                  f"{_fmt_hms(s_stops['total_s'])} in total:")
            for st in s_stops["stops"][:12]:
                where = units.fmt_dist(mi=st["at_mi"]) if st.get("at_mi") is not None else "?"
                hr_in = f"{st['hr_before']:.0f}" if st.get("hr_before") else "-"
                hr_out = f"{st['hr_after']:.0f}" if st.get("hr_after") else "-"
                print(f"    {where:>9}  {_fmt_hms(st['duration_s']):>7}  HR {hr_in} -> {hr_out}")
        print()

    bands = stream.get("bands") or {}
    if bands.get("available") and bands["total_s"] > 0:
        print("-" * 70)
        print("  TIME IN HR BANDS (moving samples)")
        print("-" * 70)
        for b in bands["bands"]:
            if b["seconds"] < 30:
                continue
            rng = f"{b['lo']:.0f}-{b['hi']:.0f}" if b["hi"] < 999 else f"{b['lo']:.0f}+"
            print(f"  {b['name']:<10} {rng:>8}   {_fmt_hms(b['seconds']):>8}   {b['pct']:>4.0%}")
        print(f"  Under the easy cap {bands['easy_cap']:.0f}: {_fmt_hms(bands['under_easy_cap_s'])}   "
              f"At/above LT {bands['lt_hr']:.0f}: {_fmt_hms(bands['at_or_above_lt_s'])}   "
              f"peak {bands['peak_hr']}")
        print()

    print("-" * 70)
    print("  VERDICTS")
    print("-" * 70)
    for n in sc["notes"]:
        print(textwrap.fill(n, width=70, initial_indent="  ", subsequent_indent="      "))
    print()


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def _make_api() -> StravaAPI:
    try:
        return StravaAPI()
    except Exception as e:
        raise StravaAPIError(f"No Strava API configured: {e}")


def _load_or_fetch_activity(activity_id: int, api: StravaAPI = None) -> dict:
    a = metrics.activity_by_id(activity_id)
    if a is not None:
        return a
    if api is None:
        api = _make_api()
    detail = api.get_activity(activity_id)
    ACT_DIR.mkdir(parents=True, exist_ok=True)
    (ACT_DIR / f"{activity_id}.json").write_text(json.dumps(detail, indent=2), encoding="utf-8")
    return detail


def _load_or_fetch_streams(activity_id: int, api: StravaAPI = None) -> dict:
    cached = metrics.load_streams(activity_id)
    if cached is not None:
        return cached
    if api is None:
        api = _make_api()
    streams = api.get_activity_streams(activity_id)
    STREAMS_DIR.mkdir(parents=True, exist_ok=True)
    (STREAMS_DIR / f"{activity_id}.json").write_text(json.dumps(streams), encoding="utf-8")
    return streams


def _latest_activity() -> dict:
    a = metrics.latest_run()
    if a is None:
        raise ValueError("No runs in the cache yet. Ingest your Strava history first.")
    return a


def _load_context(a: dict, debug: bool = False) -> Optional[dict]:
    try:
        import fitness_tracker
        return fitness_tracker.tsb_context(_run_date(a), run_tss=a.get("_run_tss"))
    except Exception as e:
        if debug:
            print(f"[debug] load context unavailable: {e}", file=sys.stderr)
        return None


def print_review(activity_id: int = None, as_json: bool = False, debug: bool = False) -> int:
    """Load one run (cache first, REST API only with credentials), review it, print it.
    Returns 0, or 1 when there is no run to review. Under --json stdout is one
    JSON document; load-phase chatter goes to stderr."""
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    with contextlib.redirect_stdout(sys.stderr if as_json else sys.stdout):
        # The REST API is a fallback for laps/streams the cache lacks, built only
        # when something is actually missing; MCP and sample data never hit it.
        box = {}

        def _api():
            if "api" not in box:
                try:
                    box["api"] = StravaAPI()
                except Exception:
                    box["api"] = None
            return box["api"]

        try:
            if activity_id is None:
                a = _latest_activity()
            else:
                a = metrics.activity_by_id(activity_id)
                if a is None:
                    a = _load_or_fetch_activity(activity_id, _api())
        except (FileNotFoundError, ValueError, StravaAPIError, OSError) as e:
            print(f"  No run to review: {e}", file=sys.stderr)
            return 1
        aid = a["id"]
        remote_ok = a.get("_source") not in ("mcp", "sample")
        laps = a.get("laps") or []
        if not laps and remote_ok and _api() is not None:
            try:
                laps = _api().get_activity_laps(aid)
            except StravaAPIError:
                laps = []
        streams = metrics.load_streams(aid)
        if streams is None and remote_ok and classify_run(a) in STREAM_CLASSES and _api() is not None:
            try:
                streams = _load_or_fetch_streams(aid, _api())
            except (StravaAPIError, OSError) as e:
                if debug:
                    print(f"[debug] streams unavailable: {e}", file=sys.stderr)
        load_ctx = _load_context(a, debug)
    r = review(a, streams=streams, laps=laps, load_ctx=load_ctx, debug=debug)
    if as_json:
        print(json.dumps(r, indent=2, default=str))
    else:
        render(r)
        if not streams:
            print("  Streams: not cached. Stops, time in HR bands, the wrist-HR check and rep "
                  "detection need them; the review-run skill fetches them.")
            print()
    return 0


def _parse_cli(argv: list) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="coach.py review", description="Debrief one run.")
    ap.add_argument("activity_id", nargs="?", type=int, help="Strava activity id (default: latest run)")
    ap.add_argument("--json", action="store_true", help="print the review dict as JSON")
    ap.add_argument("--debug", action="store_true", help="explain skipped rep tables/streams on stderr")
    return ap.parse_args(argv)


if __name__ == "__main__":
    ns = _parse_cli(sys.argv[1:])
    sys.exit(print_review(ns.activity_id, as_json=ns.json, debug=ns.debug))
