"""Fitness tracker using CTL/ATL/TSB framework.

CTL (Chronic Training Load) — 42-day EWMA of daily TSS. Represents fitness.
ATL (Acute Training Load) — 7-day EWMA of daily TSS. Represents fatigue.
TSB (Training Stress Balance) — CTL minus ATL. Positive = fresh, negative = tired.

Race day target: TSB +10 to +20 (rested but not detrained).

Usage:
    python3 fitness_tracker.py
"""

import csv
import io
import json
import sys
from datetime import datetime, timedelta, date
from pathlib import Path
from collections import defaultdict

import config


CACHE_DIR = Path(__file__).parent / "data" / "strava_cache" / "activities"
CSV_PATH = Path(__file__).parent / "activities.csv"


def _parse_csv_date(s: str):
    try:
        return datetime.strptime(s.strip(), "%b %d, %Y, %I:%M:%S %p")
    except (ValueError, AttributeError):
        return None


def load_runs() -> list:
    """Load real runs as list of dicts with date, distance_mi, moving_min, avg_hr.

    Sources from cache JSON (richer) and falls back to CSV for older runs.
    This is the running-mileage stream: soft-deleted entries, bike sessions
    logged as Run, and zero-distance entries never count (enrichment.is_real_run
    is the single source of truth for that call).
    """
    from enrichment import is_real_run

    runs = []
    seen_keys = set()  # dedupe by (date, distance_mi rounded)

    # Cache JSON first (preferred for richness)
    if CACHE_DIR.exists():
        for p in CACHE_DIR.glob("*.json"):
            try:
                a = json.loads(p.read_text())
            except json.JSONDecodeError:
                continue
            if a.get("type") != "Run" or not is_real_run(a):
                continue
            try:
                dt = datetime.fromisoformat(a["start_date_local"].replace("Z", "+00:00"))
                dt = dt.replace(tzinfo=None)
            except (KeyError, ValueError):
                continue
            dist_mi = (a.get("distance", 0) or 0) / 1609.34
            mt_min = (a.get("moving_time", 0) or 0) / 60
            if dist_mi == 0 or mt_min == 0:
                continue
            avg_hr = a.get("average_heartrate")
            key = (dt.date().isoformat(), round(dist_mi, 1))
            seen_keys.add(key)
            runs.append({
                "date": dt,
                "dist_mi": dist_mi,
                "moving_min": mt_min,
                "avg_hr": avg_hr,
                "src": "cache",
            })

    # CSV fallback for older runs
    if CSV_PATH.exists():
        with CSV_PATH.open("r", encoding="utf-8") as f:
            reader = csv.reader(f)
            try:
                next(reader)
            except StopIteration:
                return runs
            for row in reader:
                if len(row) < 32 or row[3] != "Run":
                    continue

                def _f(idx):
                    try:
                        return float(row[idx]) if row[idx] else None
                    except (ValueError, IndexError):
                        return None

                dist_km = _f(6) or 0
                if dist_km == 0:
                    continue
                dist_mi = dist_km * 0.621371
                mt_min = (_f(16) or 0) / 60
                if mt_min == 0:
                    continue
                dt = _parse_csv_date(row[1])
                if not dt:
                    continue
                probe = {
                    "type": "Run",
                    "distance": dist_km * 1000,
                    "moving_time": mt_min * 60,
                    "max_speed": _f(18),
                    "average_speed": _f(19),
                }
                if not is_real_run(probe):
                    continue
                key = (dt.date().isoformat(), round(dist_mi, 1))
                if key in seen_keys:
                    continue  # already loaded from cache
                seen_keys.add(key)
                runs.append({
                    "date": dt,
                    "dist_mi": dist_mi,
                    "moving_min": mt_min,
                    "avg_hr": _f(31),
                    "src": "csv",
                })

    runs.sort(key=lambda r: r["date"])
    return runs


def compute_tss(run: dict) -> float:
    """Training Stress Score for a single run.

    HR-based when avg_hr available, pace-based fallback otherwise.
    """
    duration_hr = run["moving_min"] / 60.0
    if not duration_hr:
        return 0.0

    if run.get("avg_hr"):
        intensity = run["avg_hr"] / config.threshold_hr()
        tss = duration_hr * (intensity ** 2) * 100
    else:
        # Pace-based fallback
        pace = run["moving_min"] / run["dist_mi"] if run["dist_mi"] > 0 else 0
        if pace > 0:
            intensity = config.threshold_pace() / pace  # faster pace = higher intensity
            tss = duration_hr * (intensity ** 2) * 100
        else:
            tss = duration_hr * 50  # generic estimate

    return min(tss, 200.0)


def crosstrain_tss(a: dict) -> float:
    """Duration-based TSS for an aerobic cross-training session (e.g. Zone-2 bike).

    Cross-training counts toward the aerobic-load stream (fitness/fatigue) but
    never toward running mileage. Intensity factor is configurable; Zone-2 work
    defaults to 0.80.
    """
    mt_min = (a.get("moving_time", 0) or 0) / 60.0
    if mt_min <= 0:
        return 0.0
    intensity = float(config.crosstrain_cfg().get("intensity_factor", 0.80))
    return min((mt_min / 60.0) * (intensity ** 2) * 100, 150.0)


def strength_tss(a: dict = None) -> float:
    """Fixed TSS per strength session so lifting fatigue shows up in TSB."""
    return float(config.strength_cfg().get("tss_per_session", 30))


# Two cross-training records within this window on the same day are treated
# as the same session logged twice (Ride from the bike computer + manual
# bike-equiv "Run") and collapsed to the better record.
CROSS_DEDUPE_SEC = 3 * 3600


def _dedupe_cross(cross: list) -> list:
    """Collapse same-day cross records whose start times overlap.

    A bike session often gets double-logged (device Ride + manual bike-equiv
    "Run"). Crediting both doubles the cross TSS. Keeps the better record:
    HR present first, then the longer duration. Takes and returns
    (datetime, activity) pairs.
    """
    by_day = defaultdict(list)
    for dt, a in cross:
        by_day[dt.date()].append((dt, a))

    def _score(item):
        _, a = item
        return (1 if a.get("average_heartrate") else 0,
                a.get("moving_time", 0) or 0)

    out = []
    for _, group in sorted(by_day.items()):
        group.sort(key=lambda item: item[0])
        kept = []
        for item in group:
            for i, k in enumerate(kept):
                if abs((item[0] - k[0]).total_seconds()) <= CROSS_DEDUPE_SEC:
                    kept[i] = max(k, item, key=_score)
                    break
            else:
                kept.append(item)
        out.extend(kept)
    return out


def load_sessions() -> list:
    """All load-bearing sessions with precomputed TSS.

    Runs (run-specific load) + cross-training + strength. This is the aerobic
    LOAD stream behind CTL/ATL/TSB. Running mileage (a separate stream) comes
    from load_runs() alone, so cross-training and lifting never inflate it.

    Cross-training arrives under several types: mcp_adapter rewrites bike-as-run
    and rides to "CrossTrain" at ingest, while the REST sync keeps raw types
    ("Ride"/"VirtualRide", or a fake "Run" carrying the bike-equiv signature).
    Classification routes all of them into the same crosstrain stream, and
    same-day duplicates (device Ride + manual bike-equiv entry) are collapsed.
    """
    from enrichment import classify_activity

    sessions = [{"date": r["date"], "tss": compute_tss(r), "kind": "run",
                 "moving_min": r["moving_min"]}
                for r in load_runs()]
    xt = config.crosstrain_cfg()
    st = config.strength_cfg()
    cross_raw = []
    if CACHE_DIR.exists():
        for p in CACHE_DIR.glob("*.json"):
            try:
                a = json.loads(p.read_text())
            except json.JSONDecodeError:
                continue
            if a.get("_deleted_at"):
                continue
            t = a.get("type")
            try:
                dt = datetime.fromisoformat(
                    a["start_date_local"].replace("Z", "+00:00")).replace(tzinfo=None)
            except (KeyError, ValueError, AttributeError):
                continue
            if t == "WeightTraining":
                if st.get("include_in_aerobic_load", True):
                    sessions.append({
                        "date": dt, "tss": strength_tss(a), "kind": "strength",
                        "moving_min": (a.get("moving_time", 0) or 0) / 60.0,
                    })
            elif xt.get("include_in_aerobic_load", True) and \
                    classify_activity(a) in ("ride", "bike_equiv"):
                cross_raw.append((dt, a))
    for dt, a in _dedupe_cross(cross_raw):
        sessions.append({
            "date": dt, "tss": crosstrain_tss(a), "kind": "crosstrain",
            "moving_min": (a.get("moving_time", 0) or 0) / 60.0,
        })
    sessions.sort(key=lambda s: s["date"])
    return sessions


def compute_loads_all(days_back: int = 90) -> list:
    """CTL/ATL/TSB over the aerobic-load stream (runs + cross-training + strength).

    Same 42d/7d EWMA as compute_loads, but the daily series is built from
    load_sessions() so cross-training and lifting contribute to fitness/fatigue.
    """
    today = date.today()
    end = today
    start = end - timedelta(days=days_back - 1)
    warmup_start = start - timedelta(days=42)

    daily = defaultdict(float)
    for s in load_sessions():
        d = s["date"].date()
        if warmup_start <= d <= end:
            daily[d] += s["tss"]

    ctl = 0.0
    atl = 0.0
    series = []
    cur = warmup_start
    while cur <= end:
        tss = daily.get(cur, 0.0)
        tsb_today = ctl - atl
        ctl = ctl + (tss - ctl) / 42.0
        atl = atl + (tss - atl) / 7.0
        if cur >= start:
            series.append({"date": cur, "tss": tss, "ctl": ctl, "atl": atl, "tsb": tsb_today})
        cur += timedelta(days=1)
    return series


def daily_tss_series(runs: list, start_date: date, end_date: date) -> dict:
    """Map date -> total TSS for that day."""
    daily = defaultdict(float)
    for r in runs:
        d = r["date"].date()
        if start_date <= d <= end_date:
            daily[d] += compute_tss(r)
    return dict(daily)


def compute_loads(runs: list, days_back: int = 90) -> list:
    """Compute CTL/ATL/TSB for each day in the window.

    Uses 42-day pre-window warmup so day 0 of the window has converged values.
    Returns list of dicts: {date, tss, ctl, atl, tsb}.
    """
    today = date.today()
    end = today
    start = end - timedelta(days=days_back - 1)
    warmup_start = start - timedelta(days=42)

    all_daily = daily_tss_series(runs, warmup_start, end)

    ctl = 0.0
    atl = 0.0
    series = []

    cur = warmup_start
    while cur <= end:
        tss = all_daily.get(cur, 0.0)
        # TSB reported is yesterday's CTL minus yesterday's ATL — i.e., what
        # state you're in going INTO today.
        tsb_today = ctl - atl

        # Update for end of today
        ctl = ctl + (tss - ctl) / 42.0
        atl = atl + (tss - atl) / 7.0

        if cur >= start:
            series.append({
                "date": cur,
                "tss": tss,
                "ctl": ctl,
                "atl": atl,
                "tsb": tsb_today,
            })
        cur += timedelta(days=1)

    return series


def classify_phase(ctl: float, atl: float, tsb: float, days_to_race: int) -> tuple:
    """Return (phase, advice)."""
    if days_to_race is not None and 0 <= days_to_race <= 21 and tsb > 5:
        return "TAPERING", "Within 3 weeks of race. Freshness building correctly."
    if tsb > 15:
        return "DETRAINING", "TSB way positive. You are losing fitness. Train."
    if tsb > 5:
        return "RECOVERING", "Recovered. This is the time for quality."
    if -10 <= tsb <= 5:
        return "MAINTAINING", "Balanced. Sustainable training rhythm."
    if -20 <= tsb < -10:
        return "BUILDING", "Productive overload. Watch for cumulative fatigue."
    return "OVERREACHING", "Heavy fatigue accumulating. Cut volume or take a day off."


def ascii_chart(series: list, metric: str = "ctl", height: int = 12, width: int = 70) -> str:
    """Render ASCII line chart."""
    if not series:
        return "(no data)"
    values = [d[metric] for d in series]
    if not values or max(values) == min(values):
        return "(no variation)"

    vmin, vmax = min(values), max(values)
    span = vmax - vmin or 1.0

    # Sample to width
    if len(values) > width:
        step = len(values) / width
        sampled = [values[int(i * step)] for i in range(width)]
    else:
        sampled = values + [values[-1]] * (width - len(values))

    grid = [[" " for _ in range(width)] for _ in range(height)]
    for x, v in enumerate(sampled):
        y = height - 1 - int((v - vmin) / span * (height - 1))
        grid[y][x] = "*"

    # Mark today (last col) with @
    grid[height - 1 - int((sampled[-1] - vmin) / span * (height - 1))][-1] = "@"

    lines = []
    for y, row in enumerate(grid):
        # Y-axis label every 3 rows
        if y == 0:
            label = f"{vmax:5.0f} "
        elif y == height - 1:
            label = f"{vmin:5.0f} "
        else:
            label = "      "
        lines.append(label + "".join(row))
    return "\n".join(lines)


def current_status() -> dict:
    """Quick snapshot of current fitness state."""
    runs = load_runs()
    # Aerobic-load stream: runs + cross-training + strength feed CTL/ATL/TSB.
    series = compute_loads_all(days_back=90)
    if not series:
        return {"error": "No data"}

    today_entry = series[-1]
    ctl = today_entry["ctl"]
    atl = today_entry["atl"]
    tsb = today_entry["tsb"]
    days_to_race = (date.fromisoformat(config.active_race()["date"]) - date.today()).days
    phase, advice = classify_phase(ctl, atl, tsb, days_to_race)

    # Trends
    ctl_30d_ago = series[-30]["ctl"] if len(series) >= 30 else series[0]["ctl"]
    ctl_7d_ago = series[-7]["ctl"] if len(series) >= 7 else series[0]["ctl"]
    last_run_date = max((r["date"] for r in runs), default=None)
    days_since_run = (datetime.now() - last_run_date).days if last_run_date else 999

    return {
        "ctl": ctl,
        "atl": atl,
        "tsb": tsb,
        "phase": phase,
        "advice": advice,
        "ctl_change_7d": ctl - ctl_7d_ago,
        "ctl_change_30d": ctl - ctl_30d_ago,
        "days_to_race": days_to_race,
        "days_since_run": days_since_run,
        "series": series,
    }


def print_fitness_report():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    s = current_status()
    if "error" in s:
        print(s["error"])
        return

    print()
    print("=" * 70)
    print(f"  FITNESS TRACKER  |  {date.today().strftime('%a %b %d %Y')}")
    print("=" * 70)
    print()
    print(f"  CTL (fitness, 42d): {s['ctl']:6.1f}")
    print(f"  ATL (fatigue, 7d):  {s['atl']:6.1f}")
    print(f"  TSB (form):         {s['tsb']:+6.1f}")
    print()
    print(f"  Phase: {s['phase']}")
    print(f"  -> {s['advice']}")
    print()
    print(f"  Trend:")
    print(f"    CTL 7d change:   {s['ctl_change_7d']:+.1f}")
    print(f"    CTL 30d change:  {s['ctl_change_30d']:+.1f}")
    print(f"    Days since run:  {s['days_since_run']}")
    print()
    race = config.active_race()
    print(f"  Race: {race['name']} in {s['days_to_race']} days")
    print(f"        Target TSB on race day: +10 to +20")
    if s['days_to_race'] <= 21:
        if s['tsb'] < 0:
            print(f"        STATUS: Currently {s['tsb']:+.0f}. Need to lighten load.")
        elif s['tsb'] > 25:
            print(f"        STATUS: Currently {s['tsb']:+.0f}. Detraining risk.")
        else:
            print(f"        STATUS: Currently {s['tsb']:+.0f}. On track for taper window.")
    print()
    print("  CTL trend (last 90 days):")
    print()
    print(ascii_chart(s["series"], metric="ctl", height=10, width=70))
    print()


if __name__ == "__main__":
    print_fitness_report()
