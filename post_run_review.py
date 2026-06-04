"""Post-run review — instant debrief on a single run with rich detail.

Pulls laps and per-second streams from Strava API for cardiac drift, pace fade,
and execution quality analysis. Tones is direct and brutally honest.

Usage:
    python3 post_run_review.py            # most recent run
    python3 post_run_review.py 18312672    # specific activity ID
"""

import json
import statistics
import sys
from datetime import datetime
from pathlib import Path

import config
from strava_api import StravaAPI, StravaAPIError


CACHE_DIR = Path(__file__).parent / "data" / "strava_cache"
ACT_DIR = CACHE_DIR / "activities"
STREAMS_DIR = CACHE_DIR / "streams"


def _make_api() -> StravaAPI:
    """Construct a StravaAPI, turning a missing .env into a StravaAPIError.

    Lets callers handle "no Strava configured" the same way they handle other
    API failures (one friendly message instead of a traceback).
    """
    try:
        return StravaAPI()
    except FileNotFoundError as e:
        raise StravaAPIError(f"No Strava configured: {e}")


def _load_or_fetch_activity(activity_id: int, api: StravaAPI = None) -> dict:
    """Load activity from cache, fetch if missing."""
    p = ACT_DIR / f"{activity_id}.json"
    if p.exists():
        return json.loads(p.read_text())
    if api is None:
        api = _make_api()
    detail = api.get_activity(activity_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(detail, indent=2))
    return detail


def _load_or_fetch_streams(activity_id: int, api: StravaAPI = None) -> dict:
    """Load streams from cache, fetch if missing. Stores by activity ID."""
    STREAMS_DIR.mkdir(parents=True, exist_ok=True)
    p = STREAMS_DIR / f"{activity_id}.json"
    if p.exists():
        return json.loads(p.read_text())
    if api is None:
        api = _make_api()
    streams = api.get_activity_streams(activity_id)
    p.write_text(json.dumps(streams, indent=2))
    return streams


def _latest_activity() -> dict:
    """Most recent run from cache."""
    if not ACT_DIR.exists():
        raise FileNotFoundError("No cached activities. Run python3 strava_sync.py first.")
    runs = []
    for p in ACT_DIR.glob("*.json"):
        try:
            a = json.loads(p.read_text())
            if a.get("type") == "Run":
                runs.append(a)
        except json.JSONDecodeError:
            continue
    if not runs:
        raise ValueError("No runs in cache.")
    return max(runs, key=lambda x: x["start_date"])


def _fmt_pace(pace_min_per_mi: float) -> str:
    if pace_min_per_mi <= 0:
        return "N/A"
    m = int(pace_min_per_mi)
    s = int((pace_min_per_mi - m) * 60)
    return f"{m}:{s:02d}/mi"


def classify_run(a: dict) -> str:
    """Classify a run by its data."""
    dist_mi = (a.get("distance", 0) or 0) / 1609.34
    mt_min = (a.get("moving_time", 0) or 0) / 60
    pace = mt_min / dist_mi if dist_mi > 0 else 0
    avg_hr = a.get("average_heartrate") or 0
    max_hr = a.get("max_heartrate") or 0

    if pace >= 13 or (avg_hr and avg_hr < 110):
        return "WALK / SHAKEOUT"
    if avg_hr and avg_hr < 130 and pace > 10.5:
        return "RECOVERY"
    if dist_mi >= 7:
        return "LONG RUN"
    if avg_hr and 145 <= avg_hr <= 165 and 8.0 <= pace <= 9.5:
        return "TEMPO"
    if max_hr and max_hr >= 175 and avg_hr and avg_hr < 150:
        return "INTERVALS"
    if avg_hr and avg_hr < config.easy_hr_cap():
        return "EASY"
    return "GENERAL AEROBIC"


def lap_breakdown(laps: list) -> dict:
    """Analyze the lap structure."""
    if not laps:
        return {"laps": [], "pattern": "no laps"}

    parsed = []
    for lap in laps:
        dist_mi = (lap.get("distance", 0) or 0) / 1609.34
        mt = (lap.get("moving_time", 0) or 0) / 60
        pace = mt / dist_mi if dist_mi > 0 else 0
        parsed.append({
            "idx": lap.get("lap_index"),
            "dist_mi": dist_mi,
            "moving_min": mt,
            "pace": pace,
            "avg_hr": lap.get("average_heartrate"),
            "max_hr": lap.get("max_heartrate"),
        })

    # Pattern detection (use full-mile laps only for split analysis)
    full_miles = [l for l in parsed if 0.95 <= l["dist_mi"] <= 1.05 and l["pace"] > 0]
    pattern = "even"
    if len(full_miles) >= 3:
        first_half = full_miles[: len(full_miles) // 2]
        second_half = full_miles[len(full_miles) // 2:]
        first_avg = statistics.mean(l["pace"] for l in first_half)
        second_avg = statistics.mean(l["pace"] for l in second_half)
        diff_sec = (second_avg - first_avg) * 60
        if diff_sec > 30:
            pattern = "positive split (slowed down)"
        elif diff_sec < -30:
            pattern = "negative split (sped up)"
        elif diff_sec > 15:
            pattern = "slight fade"

    # Pace fade in last quarter
    fade_note = None
    if len(full_miles) >= 4:
        last_quarter = full_miles[-max(1, len(full_miles) // 4):]
        rest = full_miles[: -len(last_quarter)]
        if rest and last_quarter:
            rest_pace = statistics.mean(l["pace"] for l in rest)
            late_pace = statistics.mean(l["pace"] for l in last_quarter)
            fade_sec = (late_pace - rest_pace) * 60
            if fade_sec > 30:
                fade_note = f"Last {len(last_quarter)}mi was {fade_sec:.0f}s/mi slower than rest. Pace fade."

    return {
        "laps": parsed,
        "pattern": pattern,
        "fade_note": fade_note,
    }


def cardiac_drift(streams: dict) -> dict:
    """Compute aerobic decoupling: HR/pace ratio shift between halves of run.

    Returns dict with first_ratio, second_ratio, drift_pct, severity.
    """
    if not streams or "heartrate" not in streams or "velocity_smooth" not in streams:
        return {"available": False}

    hr = streams["heartrate"].get("data", [])
    vel = streams["velocity_smooth"].get("data", [])
    if not hr or not vel or len(hr) != len(vel):
        return {"available": False}

    # Trim first 5min (warmup) and last 2min (cooldown) — assume 1Hz sampling
    n = len(hr)
    if n < 600:  # need at least 10 min
        return {"available": False, "reason": "too short"}

    start = 300
    end = n - 120
    trimmed_hr = hr[start:end]
    trimmed_vel = vel[start:end]

    half = len(trimmed_hr) // 2
    h1_hr = [v for v in trimmed_hr[:half] if v > 0]
    h2_hr = [v for v in trimmed_hr[half:] if v > 0]
    h1_vel = [v for v in trimmed_vel[:half] if v > 0.5]
    h2_vel = [v for v in trimmed_vel[half:] if v > 0.5]

    if not (h1_hr and h2_hr and h1_vel and h2_vel):
        return {"available": False, "reason": "insufficient data"}

    h1_ratio = statistics.mean(h1_vel) / statistics.mean(h1_hr) * 1000  # arbitrary scale
    h2_ratio = statistics.mean(h2_vel) / statistics.mean(h2_hr) * 1000
    drift_pct = ((h1_ratio - h2_ratio) / h1_ratio) * 100  # positive = drift

    if drift_pct < 5:
        severity = "GOOD aerobic fitness — minimal drift"
    elif drift_pct < 8:
        severity = "ACCEPTABLE — within normal range"
    else:
        severity = "HIGH drift — fitness ceiling for this distance"

    return {
        "available": True,
        "first_half_avg_hr": statistics.mean(h1_hr),
        "second_half_avg_hr": statistics.mean(h2_hr),
        "first_half_avg_pace_min_per_mi": 1609.34 / statistics.mean(h1_vel) / 60,
        "second_half_avg_pace_min_per_mi": 1609.34 / statistics.mean(h2_vel) / 60,
        "drift_pct": drift_pct,
        "severity": severity,
    }


def execution_score(a: dict, classification: str) -> tuple:
    """Score 0-100 with grade and notes."""
    avg_hr = a.get("average_heartrate") or 0
    max_hr = a.get("max_heartrate") or 0
    dist_mi = (a.get("distance", 0) or 0) / 1609.34
    mt_min = (a.get("moving_time", 0) or 0) / 60
    pace = mt_min / dist_mi if dist_mi > 0 else 0

    score = 100
    notes = []

    if classification == "EASY" or classification == "RECOVERY":
        easy_cap = config.easy_hr_cap()
        if avg_hr > easy_cap:
            over = avg_hr - easy_cap
            score -= min(over * 3, 40)
            notes.append(f"[-] Avg HR {avg_hr:.0f} exceeded easy cap of {easy_cap} "
                         f"by {over:.0f} bpm. Run slower next time.")
        else:
            notes.append(f"[+] Avg HR {avg_hr:.0f} stayed under easy cap. Discipline.")

    elif classification == "LONG RUN":
        if avg_hr > 150:
            score -= 25
            notes.append(f"[-] Long run avg HR {avg_hr:.0f} too high. Walk if it drifts.")
        else:
            notes.append(f"[+] Long run HR controlled at {avg_hr:.0f}.")
        if dist_mi < 7:
            score -= 15
            notes.append(f"[-] Long run was only {dist_mi:.1f}mi. Build distance.")

    elif classification == "TEMPO":
        if 145 <= avg_hr <= 165:
            notes.append(f"[+] Tempo HR {avg_hr:.0f} in target zone (150-165).")
        else:
            score -= 20
            notes.append(f"[-] Tempo HR {avg_hr:.0f} outside 150-165 zone.")
        if max_hr >= 180:
            notes.append(f"[+] Max HR {max_hr:.0f} touched threshold ceiling. "
                         f"Engine calibrated.")

    elif classification == "INTERVALS":
        if max_hr >= 175:
            notes.append(f"[+] Max HR {max_hr:.0f} hit interval target.")
        else:
            score -= 15
            notes.append(f"[-] Max HR {max_hr:.0f} below 175. Reps too easy.")

    if score >= 90:
        grade = "A"
    elif score >= 75:
        grade = "B"
    elif score >= 60:
        grade = "C"
    else:
        grade = "D"

    return int(max(0, score)), grade, notes


def projected_half_marathon(a: dict, laps: dict) -> str:
    """Use this run to project a half marathon time."""
    full_miles = [l for l in laps.get("laps", []) if 0.95 <= l["dist_mi"] <= 1.05]
    if not full_miles:
        return None

    # Find the work segment — if 5+ miles, use middle 60%; else use middle 50%
    if len(full_miles) >= 5:
        work = full_miles[1:-1]
    else:
        work = full_miles
    if not work:
        return None
    work_pace = statistics.mean(l["pace"] for l in work)

    # HM time = work_pace + 30 sec/mi fatigue tax for going from short tempo to 13.1
    projected_pace = work_pace + 0.5  # min/mi
    total_min = projected_pace * 13.1
    h = int(total_min // 60)
    m = int(total_min % 60)
    s = int((total_min * 60) % 60)
    return f"{h}:{m:02d}:{s:02d} ({_fmt_pace(projected_pace)})"


def print_review(activity_id: int = None):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    try:
        _print_review_body(activity_id)
    except (FileNotFoundError, StravaAPIError, ValueError) as e:
        print("No Strava configured and run not in cache — skipping detailed "
              f"review. ({e})")


def _print_review_body(activity_id: int = None):
    # API is constructed lazily — only when a cache miss actually needs it.
    api = None

    if activity_id is None:
        a = _latest_activity()
    else:
        a = _load_or_fetch_activity(activity_id, api)

    aid = a["id"]
    dist_mi = (a.get("distance", 0) or 0) / 1609.34
    mt_min = (a.get("moving_time", 0) or 0) / 60
    pace = mt_min / dist_mi if dist_mi > 0 else 0

    classification = classify_run(a)

    # Laps
    laps_data = a.get("laps") or []
    if not laps_data:
        try:
            if api is None:
                api = _make_api()
            laps_data = api.get_activity_laps(aid)
        except StravaAPIError:
            laps_data = []
    lap_info = lap_breakdown(laps_data)

    # Streams (for long runs only — saves API calls)
    drift = {"available": False}
    if classification in ("LONG RUN",):
        try:
            streams = _load_or_fetch_streams(aid, api)
            drift = cardiac_drift(streams)
        except StravaAPIError as e:
            drift = {"available": False, "reason": str(e)}

    score, grade, score_notes = execution_score(a, classification)
    hm_proj = projected_half_marathon(a, lap_info)

    print()
    print("=" * 70)
    print(f"  POST-RUN REVIEW  |  {a['start_date_local'][:10]}")
    print("=" * 70)
    print()
    print(f"  {a.get('name', '')}")
    print(f"  {dist_mi:.2f} mi  |  {int(mt_min)}:{int((mt_min%1)*60):02d}  |  "
          f"{_fmt_pace(pace)}  |  HR avg {a.get('average_heartrate', 'N/A')}, "
          f"max {a.get('max_heartrate', 'N/A')}")
    print()
    print(f"  Classification:    {classification}")
    print(f"  Execution score:   {grade} ({score}/100)")
    print()

    # Lap breakdown
    if lap_info["laps"]:
        print("-" * 70)
        print("  LAP BREAKDOWN")
        print("-" * 70)
        for l in lap_info["laps"][:15]:
            hr_str = f"HR avg {l['avg_hr']:.0f}" if l['avg_hr'] else "no HR"
            pace_str = _fmt_pace(l['pace']) if l['pace'] > 0 else 'N/A'
            print(f"  Lap {l['idx']}: {l['dist_mi']:.2f}mi @ {pace_str:>9} | {hr_str}")
        print()
        print(f"  Pattern: {lap_info['pattern']}")
        if lap_info.get("fade_note"):
            print(f"  ! {lap_info['fade_note']}")
        print()

    # Cardiac drift
    if drift.get("available"):
        print("-" * 70)
        print("  CARDIAC DRIFT (long run aerobic decoupling)")
        print("-" * 70)
        print(f"  First half:  HR {drift['first_half_avg_hr']:.0f}, "
              f"pace {_fmt_pace(drift['first_half_avg_pace_min_per_mi'])}")
        print(f"  Second half: HR {drift['second_half_avg_hr']:.0f}, "
              f"pace {_fmt_pace(drift['second_half_avg_pace_min_per_mi'])}")
        print(f"  Drift:       {drift['drift_pct']:.1f}%")
        print(f"  -> {drift['severity']}")
        print()

    # Coach notes
    print("-" * 70)
    print("  COACH NOTES")
    print("-" * 70)
    for n in score_notes:
        print(f"  {n}")
    print()

    # What this means
    if hm_proj:
        print("-" * 70)
        print("  PROJECTED HALF MARATHON")
        print("-" * 70)
        print(f"  Projected half marathon time from this effort: {hm_proj}")
        print()


if __name__ == "__main__":
    aid = int(sys.argv[1]) if len(sys.argv) > 1 else None
    print_review(aid)
