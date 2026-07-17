"""Shared fixtures for the test suite.

Everything builds synthetic data on tmp paths and points the modules at them.
All state swaps go through context managers so a failing test can't leak
paths into the next one.
"""

import contextlib
import json
from datetime import date, timedelta
from pathlib import Path

import marathon_plan as mp


# ---------------------------------------------------------------------------
# Plan fixtures
# ---------------------------------------------------------------------------

def make_plan(start: str = "2026-05-18", n_weeks: int = 4,
              target_miles: float = 20, long_run: float = 8) -> dict:
    """Minimal valid marathon-plan dict with n contiguous weeks."""
    start_d = date.fromisoformat(start)
    weeks = []
    for i in range(n_weeks):
        weeks.append({
            "week_num": i + 1,
            "start_date": (start_d + timedelta(weeks=i)).isoformat(),
            "phase": "base",
            "target_miles": target_miles,
            "long_run_target": long_run,
            "target_tss": 150,
            "key_workout": None,
            "notes": "",
        })
    return {
        "race": {
            "id": "city_marathon",
            "name": "City Marathon",
            "date": "2026-11-01",
            "distance_mi": 26.2,
            "goal_time": "3:45:00",
            "goal_pace_min_per_mi": 8.583,
            "vdot_required": 40.0,
        },
        "paces": {"easy": {"min": 10.0, "max": 10.5}, "marathon_pace": 8.583},
        "phases": [{"id": "base", "name": "Base", "color": "#000",
                    "start_week": 1, "end_week": n_weeks}],
        "weeks": weeks,
        "decision_points": [
            {
                "id": "dp1",
                "after_week": 2,
                "evaluate_date": (start_d + timedelta(weeks=2)).isoformat(),
                "name": "Test DP",
                "description": "",
                "criteria": [
                    {"metric": "weekly_mi_4wk_avg", "op": ">=", "value": 10,
                     "label": "10+ mpw avg"},
                ],
                "downgrade_action": "downgrade",
            }
        ],
    }


@contextlib.contextmanager
def temp_plan(tmp_path: Path, plan: dict = None, state: dict = None):
    """Point marathon_plan at tmp files for the duration of the block."""
    old_plan_path, old_state_path = mp.PLAN_PATH, mp.STATE_PATH
    mp.PLAN_PATH = Path(tmp_path) / "marathon_plan.json"
    mp.STATE_PATH = Path(tmp_path) / "plan_state.json"
    if plan is not None:
        mp.PLAN_PATH.write_text(json.dumps(plan), encoding="utf-8")
    if state is not None:
        mp.STATE_PATH.write_text(json.dumps(state), encoding="utf-8")
    mp.load_plan.cache_clear()
    try:
        yield mp
    finally:
        mp.PLAN_PATH, mp.STATE_PATH = old_plan_path, old_state_path
        mp.load_plan.cache_clear()


# ---------------------------------------------------------------------------
# Activity fixtures (Strava-API-shaped dicts)
# ---------------------------------------------------------------------------

_NEXT_ID = [10_000_000]


def make_activity(**overrides) -> dict:
    """Strava-API-shaped activity dict. Defaults to a real outdoor easy run."""
    _NEXT_ID[0] += 1
    a = {
        "id": _NEXT_ID[0],
        "name": "Test Run",
        "type": "Run",
        "start_date": "2026-07-13T20:30:00Z",
        "start_date_local": "2026-07-13T20:30:00",
        "distance": 8046.7,           # 5 mi
        "moving_time": 3000,          # 50 min -> 10:00/mi
        "elapsed_time": 3100,
        "average_speed": 2.682,
        "max_speed": 3.9,             # real runs always have max_speed > 0
        "average_heartrate": 140.0,
        "max_heartrate": 155.0,
        "total_elevation_gain": 20.0,
        "trainer": False,
        "manual": False,
    }
    a.update(overrides)
    return a


def make_bike_equiv(**overrides) -> dict:
    """A manual 'Run' logged at exactly 6.0 mph implementing 10min-bike = 1mi."""
    base = make_activity(
        name="Zone 2 spin",
        distance=4023.4,              # 2.5 mi
        moving_time=1500,             # 25 min at exactly 6.0 mph
        elapsed_time=1500,
        average_speed=2.6822666,
        max_speed=0,
        average_heartrate=None,
        max_heartrate=None,
        total_elevation_gain=0,
        manual=True,
    )
    base.update(overrides)
    return base


def write_cache_activity(cache_dir: Path, activity: dict) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    p = Path(cache_dir) / f"{activity['id']}.json"
    p.write_text(json.dumps(activity), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# CSV fixtures (rows matching strava_sync.CSV_HEADER layout)
# ---------------------------------------------------------------------------

def csv_row(activity: dict, n_cols: int) -> list:
    """Build a CSV row from an API-shaped activity dict.

    Mirrors strava_sync._activity_to_csv_row for the columns the loaders read:
    0=id, 1=date, 2=name, 3=type, 6=dist km, 15=elapsed s, 16=moving s,
    17=dist m, 18=max speed, 19=avg speed, 20=elev gain, 31=avg HR.
    """
    from datetime import datetime
    row = [""] * n_cols
    dt = datetime.fromisoformat(activity["start_date_local"].replace("Z", ""))
    dist_m = activity.get("distance", 0) or 0
    row[0] = str(activity["id"])
    row[1] = dt.strftime("%b %d, %Y, %I:%M:%S %p")
    row[2] = activity.get("name", "")
    row[3] = activity.get("type", "")
    row[6] = f"{dist_m / 1000:.2f}" if dist_m else ""
    row[15] = str(activity.get("elapsed_time", "") or "")
    row[16] = str(activity.get("moving_time", "") or "")
    row[17] = f"{dist_m:.1f}" if dist_m else ""
    row[18] = str(activity.get("max_speed", "")) if activity.get("max_speed") else ""
    row[19] = str(activity.get("average_speed", "")) if activity.get("average_speed") else ""
    row[20] = str(activity.get("total_elevation_gain", "")) if activity.get("total_elevation_gain") else ""
    row[31] = str(activity.get("average_heartrate", "")) if activity.get("average_heartrate") else ""
    return row


def write_csv(path: Path, activities: list) -> Path:
    import csv as _csv
    from strava_sync import CSV_HEADER
    header = CSV_HEADER.split(",")
    path = Path(path)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = _csv.writer(f)
        w.writerow(header)
        for a in activities:
            w.writerow(csv_row(a, len(header)))
    return path


@contextlib.contextmanager
def temp_activity_data(tmp_path: Path, cache_activities: list = (),
                       csv_activities: list = ()):
    """Point metrics + fitness_tracker at tmp cache/CSV for the block."""
    import metrics
    import fitness_tracker

    cache_dir = Path(tmp_path) / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    csv_path = Path(tmp_path) / "activities.csv"

    for a in cache_activities:
        write_cache_activity(cache_dir, a)
    write_csv(csv_path, list(csv_activities))

    saved = (metrics.CACHE_DIR, metrics.CSV_PATH,
             fitness_tracker.CACHE_DIR, fitness_tracker.CSV_PATH)
    metrics.CACHE_DIR = cache_dir
    metrics.CSV_PATH = csv_path
    fitness_tracker.CACHE_DIR = cache_dir
    fitness_tracker.CSV_PATH = csv_path
    try:
        yield {"cache_dir": cache_dir, "csv_path": csv_path}
    finally:
        (metrics.CACHE_DIR, metrics.CSV_PATH,
         fitness_tracker.CACHE_DIR, fitness_tracker.CSV_PATH) = saved
