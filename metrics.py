"""Cross-cutting training metrics.

Single home for derived statistics: Eddington number, streaks, best efforts,
year summaries. One canonical load_activities() that all coach modules import.
This is the chokepoint a future SQLite swap goes through.

Inspired by:
- statistics-for-strava EddingtonCalculator + BestEffort/
- running_page Activity.streak + year_summary_drawer

Usage:
    from metrics import load_activities, eddington_number, current_streak, ...

    runs = load_activities()
    print(eddington_number(runs))
    print(current_streak(runs))
"""

import csv
import io
import json
import math
import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional


CACHE_DIR = Path(__file__).parent / "data" / "strava_cache" / "activities"
CSV_PATH = Path(__file__).parent / "activities.csv"

# Standard distances in meters
DIST_1MI = 1609.34
DIST_5K = 5000
DIST_10K = 10000
DIST_10MI = 16093.4
DIST_HM = 21097.5

STANDARD_EFFORT_DISTANCES = {
    "1mi": DIST_1MI,
    "5K": DIST_5K,
    "10K": DIST_10K,
    "10mi": DIST_10MI,
    "HM": DIST_HM,
}


# ---------------------------------------------------------------------------
# Loader (single chokepoint)
# ---------------------------------------------------------------------------

def load_activities(source: str = "merged",
                    include_deleted: bool = False,
                    activity_type: Optional[str] = None,
                    include_bike_equiv: bool = False) -> list:
    """Load all activities. Returns list of dicts.

    Parameters
    ----------
    source : "merged" | "cache" | "csv"
        "merged" (default): cache JSONs (enriched) + CSV rows whose ID isn't
                            already in cache. Best for analytics across all
                            historical runs.
        "cache": only cache JSONs (recent + enriched).
        "csv": only activities.csv (legacy bulk export).
    include_deleted : bool
        Include soft-deleted activities (those with `_deleted_at`).
    activity_type : str, optional
        Filter to "Run", "Weight Training", etc.
    include_bike_equiv : bool
        When filtering to "Run", also return bike sessions manually logged
        as Run at the configured equivalence speed (class "bike_equiv") and
        zero-distance Run entries (class "invalid"). Default False: "Run"
        means real runs only.
    """
    if source == "csv":
        acts = _load_from_csv(activity_type)
    elif source == "cache":
        acts = _load_from_cache(include_deleted, activity_type)
    else:
        # Merged: cache wins, CSV fills gaps
        cache_acts = _load_from_cache(include_deleted, activity_type) if CACHE_DIR.exists() else []
        cache_ids = {str(a.get("id")) for a in cache_acts}

        csv_acts = _load_from_csv(activity_type)
        csv_only = [a for a in csv_acts if str(a.get("id")) not in cache_ids]

        acts = cache_acts + csv_only
        acts.sort(key=lambda a: a.get("start_date_local", a.get("start_date", "")), reverse=True)

    # Stamp classification on anything that missed enrichment (CSV-only rows)
    from enrichment import classify_activity
    for a in acts:
        if "_activity_class" not in a:
            a["_activity_class"] = classify_activity(a)

    if activity_type == "Run" and not include_bike_equiv:
        acts = [a for a in acts if a["_activity_class"] in ("run", "treadmill_run")]
    return acts


def _load_from_cache(include_deleted: bool, activity_type: Optional[str]) -> list:
    activities = []
    for p in CACHE_DIR.glob("*.json"):
        try:
            a = json.loads(p.read_text())
        except json.JSONDecodeError:
            continue
        if not include_deleted and a.get("_deleted_at"):
            continue
        if activity_type and a.get("type") != activity_type:
            continue
        activities.append(a)
    activities.sort(key=lambda a: a.get("start_date", ""), reverse=True)
    return activities


def _load_from_csv(activity_type: Optional[str]) -> list:
    if not CSV_PATH.exists():
        return []
    activities = []
    with CSV_PATH.open("r", encoding="utf-8") as f:
        reader = csv.reader(f)
        try:
            next(reader)
        except StopIteration:
            return []
        for row in reader:
            if len(row) < 32:
                continue
            atype = row[3]
            if activity_type and atype != activity_type:
                continue
            try:
                dist_km = float(row[6]) if row[6] else 0
            except ValueError:
                continue
            try:
                dt = datetime.strptime(row[1].strip(), "%b %d, %Y, %I:%M:%S %p")
            except ValueError:
                continue
            try:
                avg_hr = float(row[31]) if row[31] else None
            except ValueError:
                avg_hr = None
            try:
                moving_time = float(row[16]) if row[16] else 0
            except ValueError:
                moving_time = 0

            def _row_float(idx):
                try:
                    return float(row[idx]) if len(row) > idx and row[idx] else None
                except ValueError:
                    return None

            activities.append({
                "id": row[0],
                "name": row[2],
                "type": atype,
                "start_date_local": dt.isoformat(),
                "distance": dist_km * 1000,
                "moving_time": moving_time,
                "average_heartrate": avg_hr,
                # Needed by enrichment.classify_activity on CSV-only rows.
                # NB: max_speed 0 serializes as "" in the CSV, which reads
                # back as None here — classify treats both as "no max_speed".
                "max_speed": _row_float(18),
                "average_speed": _row_float(19),
                "total_elevation_gain": _row_float(20),
                # Needed by trends.py (drift/effort lenses) on CSV-only rows.
                "max_heartrate": _row_float(7),
                "relative_effort": _row_float(8),
            })
    return activities


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _activity_date(a: dict) -> date:
    """Extract local date from an activity. Handles both API and CSV formats."""
    iso = a.get("start_date_local") or a.get("start_date") or ""
    if not iso:
        return date.min
    try:
        # "2026-04-29T22:34:00Z" or "2026-04-29T22:34:00"
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.date()
    except ValueError:
        return date.min


def _distance_mi(a: dict) -> float:
    return (a.get("distance", 0) or 0) / DIST_1MI


def _moving_time_sec(a: dict) -> float:
    return a.get("moving_time", 0) or 0


def _is_run(a: dict) -> bool:
    """Real runs only — bike-as-run and zero-distance entries never count."""
    from enrichment import is_real_run
    return a.get("type") == "Run" and is_real_run(a)


def fmt_pace_min_per_mi(pace: float) -> str:
    if pace <= 0:
        return "N/A"
    m = int(pace)
    s = int(round((pace - m) * 60))
    return f"{m}:{s:02d}"


def fmt_time(seconds: float) -> str:
    if seconds <= 0:
        return "N/A"
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


# ---------------------------------------------------------------------------
# Eddington
# ---------------------------------------------------------------------------

def eddington_number(activities: list, sport_type: str = "Run") -> int:
    """Largest N where you have at least N runs of N+ miles.

    Classic running Eddington number. E=12 means 12 runs of 12+ miles.
    Single elegant metric of training depth.
    """
    runs = [a for a in activities if a.get("type") == sport_type and not a.get("_deleted_at")
            and (sport_type != "Run" or _is_run(a))]
    if not runs:
        return 0
    distances_mi = sorted([_distance_mi(r) for r in runs], reverse=True)
    e = 0
    for i, d in enumerate(distances_mi, 1):
        if d >= i:
            e = i
        else:
            break
    return e


def eddington_progress(activities: list, sport_type: str = "Run") -> dict:
    """How close to E+1 are we? Returns {current, next_n, runs_needed, runs_at_or_above}."""
    e = eddington_number(activities, sport_type)
    next_n = e + 1
    runs = [a for a in activities if a.get("type") == sport_type and not a.get("_deleted_at")
            and (sport_type != "Run" or _is_run(a))]
    runs_at_or_above = sum(1 for r in runs if _distance_mi(r) >= next_n)
    runs_needed = max(0, next_n - runs_at_or_above)
    return {
        "current": e,
        "next_n": next_n,
        "runs_at_or_above_next": runs_at_or_above,
        "runs_needed_for_next": runs_needed,
    }


# ---------------------------------------------------------------------------
# Streaks
# ---------------------------------------------------------------------------

def _run_dates(activities: list) -> set:
    """Set of unique local dates that have at least one run."""
    return {_activity_date(a) for a in activities
            if _is_run(a) and not a.get("_deleted_at")
            and _activity_date(a) != date.min}


def current_streak(activities: list, ref_date: Optional[date] = None) -> int:
    """Consecutive days with a run, ending today (or ref_date).

    Allows a 1-day grace: if you didn't run today but ran yesterday, count
    from yesterday. Drops to 0 if no run in last 2 days.
    """
    if ref_date is None:
        ref_date = date.today()
    run_days = _run_dates(activities)
    if not run_days:
        return 0

    # Allow grace: if no run today, start from yesterday
    cur = ref_date
    if cur not in run_days:
        cur = ref_date - timedelta(days=1)
        if cur not in run_days:
            return 0

    streak = 0
    while cur in run_days:
        streak += 1
        cur -= timedelta(days=1)
    return streak


def longest_streak(activities: list) -> dict:
    """Longest run-day streak ever. Returns {length, start, end}."""
    run_days = sorted(_run_dates(activities))
    if not run_days:
        return {"length": 0, "start": None, "end": None}

    best = {"length": 1, "start": run_days[0], "end": run_days[0]}
    cur_start = run_days[0]
    cur_len = 1
    for prev, cur in zip(run_days, run_days[1:]):
        if (cur - prev).days == 1:
            cur_len += 1
            if cur_len > best["length"]:
                best = {"length": cur_len, "start": cur_start, "end": cur}
        else:
            cur_start = cur
            cur_len = 1
    return best


def weeks_with_3plus_runs(activities: list, since: Optional[date] = None,
                          weeks_window: Optional[int] = None) -> dict:
    """Count weeks (Mon-Sun) with 3+ runs.

    Returns {weeks_3plus, total_weeks, pct, recent_4_weeks_3plus}.
    """
    if since is None and weeks_window is None:
        weeks_window = 52  # last year by default

    today = date.today()
    if since is None:
        since = today - timedelta(weeks=weeks_window)
    # Align to Monday
    since = since - timedelta(days=since.weekday())

    runs_per_week = defaultdict(int)
    for a in activities:
        if not _is_run(a) or a.get("_deleted_at"):
            continue
        d = _activity_date(a)
        if d < since:
            continue
        wk_start = d - timedelta(days=d.weekday())
        runs_per_week[wk_start] += 1

    # Walk every week from `since` to today even if 0 runs (so denominator is correct)
    total_weeks = 0
    weeks_3plus = 0
    cur = since
    week_starts = []
    while cur <= today:
        total_weeks += 1
        n = runs_per_week.get(cur, 0)
        if n >= 3:
            weeks_3plus += 1
        week_starts.append((cur, n))
        cur += timedelta(weeks=1)

    # Last 4 weeks
    recent_4 = sum(1 for wk, n in week_starts[-4:] if n >= 3)

    return {
        "weeks_3plus": weeks_3plus,
        "total_weeks": total_weeks,
        "pct": (weeks_3plus / total_weeks * 100) if total_weeks else 0,
        "recent_4_weeks_3plus": recent_4,
    }


# ---------------------------------------------------------------------------
# Best efforts (PRs at standard distances)
# ---------------------------------------------------------------------------

def compute_best_efforts(activities: list) -> dict:
    """Find best time at each standard distance across all runs.

    Order of preference:
    1. Strava-provided `best_efforts` array on the activity (free, accurate).
       Strava names them: "1 mile", "5k", "10k", "Half Marathon", etc.
    2. Lap-based scanning if Strava didn't provide.

    Returns {distance_label: {time_sec, pace, date, activity_id, vdot}}.
    """
    best = {}
    name_map = {
        "1 mile": "1mi",
        "5k": "5K",
        "10k": "10K",
        "15k": "15K",
        "10 mile": "10mi",
        "half marathon": "HM",
        "marathon": "Marathon",
    }

    for a in activities:
        if not _is_run(a) or a.get("_deleted_at"):
            continue
        efforts = a.get("best_efforts") or []
        for e in efforts:
            label = name_map.get((e.get("name") or "").lower())
            if not label:
                continue
            time_sec = e.get("elapsed_time") or e.get("moving_time") or 0
            if time_sec <= 0:
                continue
            distance_m = e.get("distance", 0) or STANDARD_EFFORT_DISTANCES.get(label, 0)
            if distance_m <= 0:
                continue
            cur = best.get(label)
            if cur is None or time_sec < cur["time_sec"]:
                pace = (time_sec / 60) / (distance_m / DIST_1MI) if distance_m else 0
                best[label] = {
                    "time_sec": time_sec,
                    "time_str": fmt_time(time_sec),
                    "pace_min_per_mi": pace,
                    "pace_str": fmt_pace_min_per_mi(pace),
                    "date": _activity_date(a).isoformat(),
                    "activity_id": a.get("id"),
                    "activity_name": a.get("name"),
                    "vdot": _vdot_from_effort(distance_m, time_sec),
                    "source": "strava_best_efforts",
                }

    # Fallback: scan lap data for any missing standard distances
    for label, target_m in STANDARD_EFFORT_DISTANCES.items():
        if label in best:
            continue
        candidate = _scan_laps_for_best(activities, target_m, tolerance=0.05)
        if candidate:
            best[label] = candidate

    return best


def _scan_laps_for_best(activities: list, target_m: float, tolerance: float = 0.05) -> Optional[dict]:
    """Scan all activities' lap data for the fastest contiguous segment ~= target_m.

    Simple version: looks at single laps within (1-tolerance, 1+tolerance) of target.
    Doesn't do multi-lap stitching (that requires stream-level analysis).
    """
    best = None
    lo = target_m * (1 - tolerance)
    hi = target_m * (1 + tolerance)
    for a in activities:
        if not _is_run(a) or a.get("_deleted_at"):
            continue
        for lap in (a.get("laps") or []):
            d = lap.get("distance", 0) or 0
            if not (lo <= d <= hi):
                continue
            t = lap.get("moving_time", 0) or 0
            if t <= 0:
                continue
            # Normalize to target distance
            scaled_t = t * (target_m / d)
            if best is None or scaled_t < best["time_sec"]:
                pace = (scaled_t / 60) / (target_m / DIST_1MI)
                best = {
                    "time_sec": scaled_t,
                    "time_str": fmt_time(scaled_t),
                    "pace_min_per_mi": pace,
                    "pace_str": fmt_pace_min_per_mi(pace),
                    "date": _activity_date(a).isoformat(),
                    "activity_id": a.get("id"),
                    "activity_name": a.get("name"),
                    "vdot": _vdot_from_effort(target_m, scaled_t),
                    "source": "lap_scan",
                }
    return best


def _vdot_from_effort(distance_m: float, time_sec: float) -> float:
    """Jack Daniels VDOT (Eq. 4.1 + 4.2)."""
    if distance_m <= 0 or time_sec <= 0:
        return 0.0
    v = distance_m / (time_sec / 60.0)  # m/min
    t = time_sec / 60.0  # min
    vo2 = -4.6 + 0.182258 * v + 0.000104 * v * v
    pct = (
        0.8
        + 0.1894393 * math.exp(-0.012778 * t)
        + 0.2989558 * math.exp(-0.1932605 * t)
    )
    return round(vo2 / pct, 2) if pct > 0 else 0.0


# ---------------------------------------------------------------------------
# Year summaries
# ---------------------------------------------------------------------------

def year_summary(activities: list) -> list:
    """Per-year roll-up. Returns list ordered newest-first."""
    by_year = defaultdict(list)
    for a in activities:
        if not _is_run(a) or a.get("_deleted_at"):
            continue
        d = _activity_date(a)
        if d == date.min:
            continue
        by_year[d.year].append(a)

    out = []
    for y in sorted(by_year.keys(), reverse=True):
        runs = by_year[y]
        miles = sum(_distance_mi(r) for r in runs)
        run_count = len(runs)
        longest = max((_distance_mi(r) for r in runs), default=0)
        paces = [
            (_moving_time_sec(r) / 60) / _distance_mi(r)
            for r in runs if _distance_mi(r) > 0 and _moving_time_sec(r) > 0
        ]
        avg_pace = statistics.mean(paces) if paces else 0
        be = compute_best_efforts(runs)
        out.append({
            "year": y,
            "total_mi": round(miles, 1),
            "run_count": run_count,
            "longest_mi": round(longest, 1),
            "avg_pace_min_per_mi": round(avg_pace, 2),
            "avg_pace_str": fmt_pace_min_per_mi(avg_pace),
            "best_5k": be.get("5K", {}).get("time_str", "N/A"),
            "best_10k": be.get("10K", {}).get("time_str", "N/A"),
            "best_hm": be.get("HM", {}).get("time_str", "N/A"),
        })
    return out


def rolling_year_summary(activities: list, end: Optional[date] = None) -> dict:
    """Last 365 days roll-up."""
    if end is None:
        end = date.today()
    start = end - timedelta(days=365)
    runs = [a for a in activities if _is_run(a) and not a.get("_deleted_at")
            and start <= _activity_date(a) <= end]
    miles = sum(_distance_mi(r) for r in runs)
    longest = max((_distance_mi(r) for r in runs), default=0)
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "total_mi": round(miles, 1),
        "run_count": len(runs),
        "longest_mi": round(longest, 1),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def print_summary():
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    runs = load_activities(activity_type="Run")
    print(f"Loaded {len(runs)} runs")
    print()

    e = eddington_progress(runs)
    print("EDDINGTON")
    print(f"  Current: E={e['current']} (you have {e['current']}+ runs of {e['current']}+ miles)")
    print(f"  Next:    E={e['next_n']} needs {e['runs_needed_for_next']} more runs of "
          f"{e['next_n']}+ miles ({e['runs_at_or_above_next']} so far)")
    print()

    s = current_streak(runs)
    ls = longest_streak(runs)
    print("STREAKS")
    print(f"  Current: {s} days")
    if ls['start']:
        print(f"  Longest: {ls['length']} days ({ls['start']} to {ls['end']})")
    print()

    w = weeks_with_3plus_runs(runs, weeks_window=52)
    print("CONSISTENCY (last 52 weeks)")
    print(f"  Weeks at 3+ runs: {w['weeks_3plus']}/{w['total_weeks']} ({w['pct']:.0f}%)")
    print(f"  Last 4 weeks at 3+ runs: {w['recent_4_weeks_3plus']}/4")
    print()

    be = compute_best_efforts(runs)
    print("BEST EFFORTS")
    for label in ["1mi", "5K", "10K", "10mi", "HM"]:
        if label in be:
            b = be[label]
            print(f"  {label:5s}: {b['time_str']:>9s} @ {b['pace_str']}/mi | "
                  f"VDOT {b['vdot']:.1f} | {b['date']} | {b['activity_name']}")
        else:
            print(f"  {label:5s}: -- (no qualifying effort)")
    print()

    print("YEAR SUMMARY")
    for y in year_summary(runs):
        print(f"  {y['year']}: {y['run_count']:3d} runs | {y['total_mi']:6.1f} mi | "
              f"longest {y['longest_mi']:5.1f} mi | avg pace {y['avg_pace_str']}/mi | "
              f"5K {y['best_5k']} | 10K {y['best_10k']} | HM {y['best_hm']}")

    r = rolling_year_summary(runs)
    print(f"  Last 365: {r['run_count']:3d} runs | {r['total_mi']:6.1f} mi | "
          f"longest {r['longest_mi']:5.1f} mi")
    print()


if __name__ == "__main__":
    print_summary()
