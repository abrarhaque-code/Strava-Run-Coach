#!/usr/bin/env python3
"""Generate a realistic, internally consistent sample training history.

Everything is anchored to date.today(), so a fresh clone on any date gets a
populated dashboard: race countdowns, the CTL 42-day warmup, the last-3-days
brief window, run streaks, the best-efforts table, and the calendar heatmap all
fill in.

What it writes:
  - activities.csv         full Strava-bulk-export header + ~16 weeks of rows
                           (distances in KILOMETERS, matching the real export)
  - data/strava_cache/activities/<id>.json   one enriched JSON per activity for
                           the most recent ~35 days (richer fields + best_efforts)

The generator is deterministic (seeded) and overwrites what it creates, so you
can re-run it freely.

Run it directly:
    python3 scripts/generate_sample_data.py

Stdlib only, plus the project's enrichment module.
"""

import csv
import json
import math
import random
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

# Make the project root importable whether this is run as a module or a script.
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import enrichment  # noqa: E402

CSV_PATH = _ROOT / "activities.csv"
CACHE_DIR = _ROOT / "data" / "strava_cache" / "activities"

MI = 1609.34  # meters per mile
SEED = 20260601
CACHE_DAYS = 35  # write cache JSON for activities within this many days of today

# The exact Strava bulk-export header. Read from activities.csv.example so it
# stays in lockstep with the format the parsers expect.
EXAMPLE_HEADER_PATH = _ROOT / "activities.csv.example"

# Column indices the analysis code reads (0-based). Everything else stays blank.
COL_ID = 0
COL_DATE = 1
COL_NAME = 2
COL_TYPE = 3
COL_DESC = 4
COL_ELAPSED_LIFT = 5     # Weight Training elapsed seconds live here
COL_DISTANCE_KM = 6      # kilometers
COL_MAX_HR_EARLY = 7
COL_REL_EFFORT_EARLY = 8
COL_ELAPSED_S = 15
COL_MOVING_S = 16
COL_MAX_SPEED = 18
COL_AVG_SPEED = 19
COL_ELEV_GAIN = 20
COL_AVG_CADENCE = 29
COL_AVG_HR = 31


def _load_header() -> list:
    """Read the canonical header row from activities.csv.example."""
    with EXAMPLE_HEADER_PATH.open("r", encoding="utf-8") as f:
        reader = csv.reader(f)
        return next(reader)


def _fmt_csv_date(dt: datetime) -> str:
    """Strava bulk-export date format, e.g. 'Jun 01, 2026, 7:30:00 AM'."""
    stamp = dt.strftime("%b %d, %Y, %I:%M:%S %p")
    # Strip a leading zero on the hour so it matches Strava ("7:30" not "07:30").
    parts = stamp.rsplit(", ", 1)
    time_part = parts[1]
    if time_part[0] == "0":
        time_part = time_part[1:]
    return parts[0] + ", " + time_part


def _iso_local(dt: datetime) -> str:
    """Naive local ISO timestamp, e.g. '2026-06-01T07:30:00'."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


# ---------------------------------------------------------------------------
# Training plan shape
# ---------------------------------------------------------------------------

def _week_volume(week_idx: int, total_weeks: int) -> float:
    """Target weekly mileage with gentle growth plus a down week every 4th week.

    week_idx 0 is the oldest week; total_weeks-1 is the current week.
    """
    base = 14.0 + week_idx * 1.05          # gradual week-over-week growth
    if week_idx > 0 and week_idx % 4 == 3:  # cutback every 4th week
        base *= 0.8
    return base


def _long_run_miles(week_idx: int, total_weeks: int) -> float:
    """Long run grows from ~6 to ~13 miles across the block."""
    frac = week_idx / max(1, total_weeks - 1)
    miles = 6.0 + frac * 7.0
    if week_idx > 0 and week_idx % 4 == 3:  # shorter long run on cutback weeks
        miles *= 0.82
    return round(miles * 2) / 2.0  # nearest half mile


def _easy_run(rng: random.Random) -> tuple:
    """(distance_mi, pace_min_per_mi, avg_hr, max_hr) for an easy run."""
    dist = round(rng.uniform(3.0, 5.0) * 2) / 2.0
    pace = rng.uniform(10.0, 10.5)
    avg_hr = rng.randint(138, 145)
    max_hr = avg_hr + rng.randint(6, 12)
    return dist, pace, avg_hr, max_hr


def _long_run(week_idx: int, total_weeks: int, rng: random.Random) -> tuple:
    dist = _long_run_miles(week_idx, total_weeks)
    pace = rng.uniform(10.0, 10.4)
    avg_hr = rng.randint(145, 150)
    max_hr = avg_hr + rng.randint(7, 14)
    return dist, pace, avg_hr, max_hr


def _quality_run(week_idx: int, total_weeks: int, rng: random.Random) -> tuple:
    """A tempo / threshold run. Gets slightly longer and a touch faster later."""
    frac = week_idx / max(1, total_weeks - 1)
    dist = round((4.0 + frac * 2.5) * 2) / 2.0   # 4.0 -> ~6.5 mi
    pace = rng.uniform(8.7, 9.05) - frac * 0.15  # gentle improvement
    avg_hr = rng.randint(158, 165)
    max_hr = avg_hr + rng.randint(8, 16)
    return dist, pace, avg_hr, max_hr


def _cadence_for(pace: float, rng: random.Random) -> int:
    """Faster pace -> higher cadence. Reported as single-leg spm."""
    base = 80 + (10.5 - pace) * 3.0
    return int(round(base + rng.uniform(-2, 2)))


# ---------------------------------------------------------------------------
# Activity construction
# ---------------------------------------------------------------------------

def _make_run(dt: datetime, name: str, dist_mi: float, pace: float,
              avg_hr: int, max_hr: int, rng: random.Random,
              with_best_efforts: bool = False) -> dict:
    """Build a Strava-cache-shaped run dict (distances in METERS)."""
    moving_s = int(round(dist_mi * pace * 60))
    elapsed_s = moving_s + rng.randint(20, 180)  # stops at lights etc.
    dist_m = round(dist_mi * MI, 1)
    elev = round(dist_mi * rng.uniform(4.0, 11.0), 1)
    cadence = _cadence_for(pace, rng)

    # Device-recorded runs always carry a max_speed above average. Without it
    # a run reads as a manual entry, and a 10:00/mi easy run then sits exactly
    # on the bike-equivalence speed signature and would be misclassified.
    avg_speed = round(dist_m / moving_s, 3) if moving_s else 0.0
    max_speed = round(avg_speed * rng.uniform(1.15, 1.35), 3)

    d = {
        "id": None,  # filled by caller
        "name": name,
        "type": "Run",
        "start_date": _iso_local(dt),       # treat as naive local for samples
        "start_date_local": _iso_local(dt),
        "distance": dist_m,
        "moving_time": moving_s,
        "elapsed_time": elapsed_s,
        "average_heartrate": float(avg_hr),
        "max_heartrate": float(max_hr),
        "average_speed": avg_speed,
        "max_speed": max_speed,
        "total_elevation_gain": elev,
        "average_cadence": float(cadence),
    }

    if with_best_efforts:
        d["best_efforts"] = _best_efforts_for(dt, dist_m, pace, rng)
    return d


def _best_efforts_for(dt: datetime, dist_m: float, run_pace: float,
                      rng: random.Random) -> list:
    """Best efforts at standard splits the run actually covers.

    The split paces are faster than the whole-run pace (you surge within the
    run), which is what makes the VDOT signal believable. Names match what
    Strava emits and what metrics.compute_best_efforts maps: '1 mile', '5k',
    '10k'.
    """
    splits = [
        ("1 mile", 1609.0, run_pace - 0.55),
        ("5k", 5000.0, run_pace - 0.30),
        ("10k", 10000.0, run_pace - 0.10),
    ]
    out = []
    for label, split_m, split_pace in splits:
        if dist_m < split_m - 50:
            continue  # run wasn't long enough to contain this split
        split_pace = max(7.4, split_pace)  # floor so paces stay realistic
        t = int(round((split_m / MI) * split_pace * 60))
        out.append({
            "name": label,
            "distance": split_m,
            "moving_time": t,
            "elapsed_time": t,
            "start_date_local": _iso_local(dt),
        })
    return out


def _make_lift(dt: datetime, name: str, rng: random.Random) -> dict:
    """Weight Training row. Only date/name/elapsed matter downstream."""
    elapsed_s = rng.randint(30, 50) * 60
    return {
        "id": None,
        "name": name,
        "type": "Weight Training",
        "start_date": _iso_local(dt),
        "start_date_local": _iso_local(dt),
        "elapsed_time": elapsed_s,
        "moving_time": elapsed_s,
        "distance": 0,
    }


def build_history(today: date | None = None) -> list:
    """Build ~16 weeks of activities ending today. Returns a list of dicts."""
    if today is None:
        today = date.today()
    rng = random.Random(SEED)

    total_weeks = 16
    activities = []
    next_id = 90000001

    lift_names = ["Upper Body", "Lower Body", "Arnold Split", "Full Body"]

    # Week 0 = oldest, week total_weeks-1 = current week (contains today).
    monday_this_week = today - timedelta(days=today.weekday())

    for week_idx in range(total_weeks):
        weeks_ago = (total_weeks - 1) - week_idx
        week_monday = monday_this_week - timedelta(weeks=weeks_ago)

        target = _week_volume(week_idx, total_weeks)

        # Decide which weekday slots get runs. 3-4 runs/week.
        # Tue = quality, Sat = long, plus 1-2 easy on Mon/Thu/Sun.
        run_days = {1: "quality", 5: "long"}  # Tue, Sat
        easy_slots = [0, 3, 6]  # Mon, Thu, Sun
        n_easy = rng.choice([1, 2])
        for slot in rng.sample(easy_slots, n_easy):
            run_days[slot] = "easy"

        # Lift 1-2x/week on days without a run (or doubled up occasionally).
        lift_days = set()
        n_lift = rng.choice([1, 2])
        for slot in rng.sample([0, 2, 3, 4], n_lift):
            lift_days.add(slot)

        # Build the week, tracking easy mileage so the week roughly hits target.
        planned = []
        for weekday, kind in sorted(run_days.items()):
            run_date = week_monday + timedelta(days=weekday)
            if run_date > today:
                continue  # don't fabricate future runs
            if kind == "quality":
                dist, pace, ahr, mhr = _quality_run(week_idx, total_weeks, rng)
                nm = "Tempo Run" if pace > 8.85 else "Threshold Run"
            elif kind == "long":
                dist, pace, ahr, mhr = _long_run(week_idx, total_weeks, rng)
                nm = "Long Run"
            else:
                dist, pace, ahr, mhr = _easy_run(rng)
                nm = "Easy Run"
            planned.append((run_date, nm, dist, pace, ahr, mhr, kind))

        # Nudge easy-run distances so weekly volume tracks the target, but keep
        # each easy run realistic. Only spread a positive deficit, cap the per-
        # run bump, and never let an "easy" run rival the long run.
        scheduled_miles = sum(p[2] for p in planned)
        easy_idx = [i for i, p in enumerate(planned) if p[6] == "easy"]
        if easy_idx and scheduled_miles > 0:
            deficit = max(0.0, target - scheduled_miles)
            bump = min(2.0, deficit / len(easy_idx))
            for i in easy_idx:
                rd, nm, dist, pace, ahr, mhr, kind = planned[i]
                dist = max(2.5, min(6.5, round((dist + bump) * 2) / 2.0))
                planned[i] = (rd, nm, dist, pace, ahr, mhr, kind)

        # Pick which recent runs carry best_efforts (2-3 across the last ~3 wks).
        for rd, nm, dist, pace, ahr, mhr, kind in planned:
            hour = 8 if rd.weekday() >= 5 else 20  # weekend mornings, weekday eves
            if rd == today:
                hour = 7  # keep a same-day run safely earlier than "now"
            minute = rng.choice([0, 5, 10, 15, 30, 45])
            dt = datetime(rd.year, rd.month, rd.day, hour, minute, 0)
            days_ago = (today - rd).days
            wants_be = (kind == "quality" and days_ago <= 18)
            run = _make_run(dt, nm, dist, pace, ahr, mhr, rng,
                            with_best_efforts=wants_be)
            run["id"] = next_id
            next_id += 1
            activities.append(run)

        # Lifts
        for weekday in sorted(lift_days):
            lift_date = week_monday + timedelta(days=weekday)
            if lift_date > today:
                continue
            dt = datetime(lift_date.year, lift_date.month, lift_date.day,
                          20, 30, 0)
            lift = _make_lift(dt, rng.choice(lift_names), rng)
            lift["id"] = next_id
            next_id += 1
            activities.append(lift)

    # Guarantee a run inside the last 3 days for the daily brief.
    _ensure_recent_run(activities, today, rng)

    activities.sort(key=lambda a: a["start_date_local"])
    return activities


def _ensure_recent_run(activities: list, today: date, rng: random.Random) -> None:
    """If nothing landed in the last 3 days, add an easy run for yesterday."""
    horizon = today - timedelta(days=3)
    has_recent = any(
        a["type"] == "Run"
        and datetime.fromisoformat(a["start_date_local"]).date() >= horizon
        for a in activities
    )
    if has_recent:
        return
    rd = today - timedelta(days=1)
    dt = datetime(rd.year, rd.month, rd.day, 20, 15, 0)
    dist, pace, ahr, mhr = _easy_run(rng)
    run = _make_run(dt, "Easy Run", dist, pace, ahr, mhr, rng)
    run["id"] = 90009999
    activities.append(run)


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------

def _activity_to_csv_row(a: dict, header_len: int) -> list:
    """Project a cache-shaped dict onto the Strava bulk-export columns."""
    row = [""] * header_len
    dt = datetime.fromisoformat(a["start_date_local"])
    row[COL_ID] = str(a["id"])
    row[COL_DATE] = _fmt_csv_date(dt)
    row[COL_NAME] = a["name"]
    row[COL_TYPE] = a["type"]

    if a["type"] == "Run":
        dist_km = round((a.get("distance", 0) or 0) / 1000.0, 3)
        row[COL_DISTANCE_KM] = f"{dist_km:.3f}"
        row[COL_ELAPSED_S] = f"{float(a.get('elapsed_time', 0) or 0):.1f}"
        row[COL_MOVING_S] = f"{float(a.get('moving_time', 0) or 0):.1f}"
        if a.get("max_heartrate"):
            row[COL_MAX_HR_EARLY] = f"{float(a['max_heartrate']):.1f}"
            row[COL_AVG_HR] = f"{float(a.get('average_heartrate', 0) or 0):.1f}"
        if a.get("max_speed"):
            row[COL_MAX_SPEED] = f"{float(a['max_speed']):.3f}"
        if a.get("average_speed"):
            row[COL_AVG_SPEED] = f"{float(a['average_speed']):.3f}"
        row[COL_ELEV_GAIN] = f"{float(a.get('total_elevation_gain', 0) or 0):.1f}"
        if a.get("average_cadence"):
            row[COL_AVG_CADENCE] = f"{float(a['average_cadence']):.1f}"
        # A simple relative-effort proxy so that column isn't empty.
        dur_hr = (a.get("moving_time", 0) or 0) / 3600.0
        re = int(round(dur_hr * 60))
        row[COL_REL_EFFORT_EARLY] = str(re)
    else:  # Weight Training: elapsed seconds live in index 5
        row[COL_ELAPSED_LIFT] = str(int(a.get("elapsed_time", 0) or 0))
        row[COL_DESC] = a.get("description", "Strength session")
        row[COL_DISTANCE_KM] = "0"

    return row


def write_csv(activities: list) -> int:
    header = _load_header()
    rows = [_activity_to_csv_row(a, len(header)) for a in activities]
    with CSV_PATH.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)
    return len(rows)


def write_cache(activities: list, today: date) -> int:
    """Write enriched cache JSON for activities within CACHE_DAYS of today."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    horizon = today - timedelta(days=CACHE_DAYS)

    # Clean out any cache files we previously generated (the 9000xxxx range)
    # so re-runs stay tidy without nuking a user's real synced data.
    for p in CACHE_DIR.glob("9*.json"):
        try:
            stem_id = int(p.stem)
        except ValueError:
            continue
        if 90000000 <= stem_id <= 90099999:
            p.unlink()

    written = 0
    for a in activities:
        dt = datetime.fromisoformat(a["start_date_local"])
        if dt.date() < horizon:
            continue
        d = dict(a)  # copy so enrichment fields don't leak into the CSV pass
        d = enrichment.enrich(d)
        out = CACHE_DIR / f"{a['id']}.json"
        out.write_text(json.dumps(d, indent=2), encoding="utf-8")
        written += 1
    return written


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    today = date.today()
    activities = build_history(today)

    runs = [a for a in activities if a["type"] == "Run"]
    lifts = [a for a in activities if a["type"] == "Weight Training"]

    n_csv = write_csv(activities)
    n_cache = write_cache(activities, today)

    dates = [datetime.fromisoformat(a["start_date_local"]).date()
             for a in activities]
    start, end = min(dates), max(dates)
    total_mi = sum((a.get("distance", 0) or 0) / MI for a in runs)
    n_be = sum(1 for a in runs if a.get("best_efforts"))

    print("Sample data generated.")
    print(f"  Date range:        {start.isoformat()} -> {end.isoformat()}")
    print(f"  Activities:        {len(activities)} "
          f"({len(runs)} runs, {len(lifts)} lifts)")
    print(f"  Total run mileage: {total_mi:.1f} mi")
    print(f"  Runs w/ splits:    {n_be}")
    print(f"  CSV rows written:  {n_csv}  ->  {CSV_PATH.name}")
    print(f"  Cache JSON files:  {n_cache}  ->  "
          f"data/strava_cache/activities/")
    print()
    print("Next: python3 coach.py")


if __name__ == "__main__":
    main()
