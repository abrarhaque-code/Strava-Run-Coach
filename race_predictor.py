#!/usr/bin/env python3
"""Race time predictor and goal probability based on Jack Daniels VDOT.

Reads Strava cache JSONs in data/strava_cache/activities/, computes current
fitness VDOT from best recent effort, predicts the active race's finish time,
and estimates the probability of hitting the goal time.

Stdlib only.
"""
import glob
import json
import math
import os
from datetime import date, datetime, timedelta

import config

# ---- Constants ----
HALF_M = 21097.5
ACTIVITIES_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "data",
    "strava_cache",
    "activities",
)
MI_PER_M = 1 / 1609.34


# ---- Core VDOT formulas ----
def compute_vdot(distance_m: float, time_sec: float) -> float:
    """Jack Daniels VDOT from a single performance."""
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
    return vo2 / pct


def predict_race_time(vdot: float, distance_m: float) -> int:
    """Invert VDOT formula via binary search. Returns seconds.

    At a fixed distance, faster time -> higher VDOT. So if compute_vdot(d, mid)
    is HIGHER than the target, mid is too FAST -> we need a LARGER time.
    """
    lo, hi = 60.0, 6 * 3600.0
    for _ in range(80):
        mid = (lo + hi) / 2
        implied = compute_vdot(distance_m, mid)
        if implied > vdot:
            # mid time is too fast (gives higher VDOT than target); slow it down
            lo = mid
        else:
            # mid time is too slow; speed it up
            hi = mid
    return int(round((lo + hi) / 2))


# ---- Activity loading ----
def load_activities() -> list:
    out = []
    for fp in glob.glob(os.path.join(ACTIVITIES_DIR, "*.json")):
        try:
            with open(fp, "r", encoding="utf-8") as f:
                a = json.load(f)
        except Exception:
            continue
        if a.get("type") != "Run":
            continue
        # Real runs only: a VDOT anchored on a bike session logged as a Run
        # (or a soft-deleted entry) would poison every prediction downstream.
        from enrichment import is_real_run
        if not is_real_run(a):
            continue
        dist_m = a.get("distance", 0) or 0
        time_s = a.get("moving_time", 0) or 0
        if dist_m <= 0 or time_s <= 0:
            continue
        date_str = a.get("start_date_local") or a.get("start_date")
        if not date_str:
            continue
        dt = datetime.fromisoformat(date_str.replace("Z", ""))
        dist_mi = dist_m * MI_PER_M
        out.append(
            {
                "id": a.get("id"),
                "name": a.get("name", ""),
                "date": dt,
                "distance_m": dist_m,
                "distance_mi": dist_mi,
                "time_sec": time_s,
                "pace_min_mi": (time_s / 60.0) / dist_mi if dist_mi > 0 else 0,
                "avg_hr": a.get("average_heartrate"),
                "max_hr": a.get("max_heartrate"),
            }
        )
    return sorted(out, key=lambda x: x["date"])


# Race results stay a valid fitness anchor much longer than training-run
# scans: fitness decays slowly, and a race is a maximal, measured effort.
RACE_HISTORY_MAX_AGE_DAYS = 180


# ---- Best recent effort -> VDOT ----
def current_fitness_vdot(activities: list, today: datetime = None) -> tuple:
    """Best fitness signal in last 30 days (plus recent race results).

    Computes VDOT from:
    1. Whole-run VDOT for each recent run (>= 3km)
    2. Strava-provided best_efforts at standard distances (1mi, 5K, 10K, etc)
    3. Actual race results from config race_history (<= 180 days old) — the
       strongest anchor, and the fix for the cold-start default on a fresh
       install with a known recent result.
    Then returns the MAX VDOT across all signals.

    The MAX is the right choice because we're asking: "what's the best fitness
    this athlete has shown recently?" Lower VDOTs from sub-maximal efforts
    underestimate fitness.
    """
    if today is None:
        today = datetime.now()
    cutoff = today - timedelta(days=30)
    candidates = []

    # 1. Whole-run VDOT scan
    recent_whole = [a for a in activities if a["date"] >= cutoff and a["distance_m"] >= 3000]
    for a in recent_whole:
        v = compute_vdot(a["distance_m"], a["time_sec"])
        if v > 0:
            candidates.append({
                "vdot": v,
                "note": "whole run",
                "name": a.get("name", ""),
                "date": a["date"].strftime("%Y-%m-%d"),
                "distance_mi": a.get("distance_mi", a["distance_m"] / 1609.34),
                "pace_str": _pace_str(a["time_sec"], a["distance_m"]),
            })

    # 2. Strava best_efforts within recent activities
    try:
        from metrics import load_activities as _load_metrics, compute_best_efforts
        all_runs = _load_metrics(activity_type="Run")
        recent_runs = [r for r in all_runs
                        if r.get("start_date_local", "") >= cutoff.isoformat()]
        be = compute_best_efforts(recent_runs)
        for label, b in be.items():
            if b.get("vdot", 0) > 0:
                candidates.append({
                    "vdot": b["vdot"],
                    "note": f"best {label} effort",
                    "name": b.get("activity_name", ""),
                    "date": b["date"],
                    "pace_str": b["pace_str"],
                    "distance_mi": 0,  # not relevant here
                })
    except Exception:
        pass

    # 3. Actual race results from config (a race beats any training inference)
    try:
        import config as _config
        for h in _config.race_history():
            d = datetime.fromisoformat(h["date"])
            if d > today or (today - d).days > RACE_HISTORY_MAX_AGE_DAYS:
                continue
            t_sec = _config.goal_time_to_sec(h.get("result_time", ""))
            dist_m = (h.get("distance_mi") or 0) * 1609.34
            if t_sec <= 0 or dist_m <= 0:
                continue
            v = compute_vdot(dist_m, t_sec)
            if v > 0:
                candidates.append({
                    "vdot": v,
                    "note": "race result",
                    "name": h.get("name", h.get("id", "")),
                    "date": h["date"],
                    "distance_mi": h.get("distance_mi", 0),
                    "pace_str": _pace_str(t_sec, dist_m),
                })
    except Exception:
        pass

    if not candidates:
        return (35.0, {"note": "no recent qualifying efforts"})

    # Take the MAX VDOT — best fitness signal
    best = max(candidates, key=lambda c: c["vdot"])
    return (best["vdot"], best)


def _pace_str(time_sec: float, distance_m: float) -> str:
    if not distance_m:
        return ""
    pace_sec = time_sec / (distance_m / 1609.34)
    m = int(pace_sec // 60)
    s = int(round(pace_sec - m * 60))
    if s == 60:
        m += 1
        s = 0
    return f"{m}:{s:02d}"


# ---- Trend & volume ----
def weekly_volume_mi(activities: list, days: int = 14, today: datetime = None) -> float:
    if today is None:
        today = datetime.now()
    cutoff = today - timedelta(days=days)
    miles = sum(a["distance_mi"] for a in activities if a["date"] >= cutoff)
    return miles / (days / 7.0)


def fitness_trend(activities: list, today: datetime = None) -> dict:
    if today is None:
        today = datetime.now()
    last14 = [a for a in activities if a["date"] >= today - timedelta(days=14)]
    prior30 = [
        a
        for a in activities
        if today - timedelta(days=44) <= a["date"] < today - timedelta(days=14)
    ]

    def stats(runs, window_days):
        if not runs:
            return {"vol_per_wk": 0.0, "long": 0.0, "best_vdot": 0.0}
        vol = sum(r["distance_mi"] for r in runs) / (window_days / 7.0)
        long = max(r["distance_mi"] for r in runs)
        vdots = [
            compute_vdot(r["distance_m"], r["time_sec"])
            for r in runs
            if r["distance_m"] >= 3000
        ]
        return {
            "vol_per_wk": vol,
            "long": long,
            "best_vdot": max(vdots) if vdots else 0.0,
        }

    return {
        "recent": stats(last14, 14),
        "prior": stats(prior30, 30),
    }


# ---- Probability ----
def race_probability(
    current_vdot: float,
    goal_vdot: float,
    trend: dict,
    weekly_vol_mi: float,
) -> float:
    gap = current_vdot - goal_vdot
    p = 1.0 / (1.0 + math.exp(-1.2 * gap))
    if trend["recent"]["best_vdot"] > trend["prior"]["best_vdot"] > 0:
        p *= 1.1
    if weekly_vol_mi < 12:
        p *= 0.6
    elif weekly_vol_mi < 18:
        p *= 0.85
    return max(0.01, min(0.99, p))


# ---- Formatting ----
def fmt_time(sec: int) -> str:
    sec = int(sec)
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_pace_per_mi(total_sec: int, distance_m: float) -> str:
    miles = distance_m * MI_PER_M
    sec_per_mi = total_sec / miles
    m = int(sec_per_mi // 60)
    s = int(round(sec_per_mi - m * 60))
    if s == 60:
        m += 1
        s = 0
    return f"{m}:{s:02d}/mi"


def fmt_pace_min_mi(pace_min_mi: float) -> str:
    """Format a pace given in minutes-per-mile (float) as M:SS."""
    m = int(pace_min_mi)
    s = int(round((pace_min_mi - m) * 60))
    if s == 60:
        m += 1
        s = 0
    return f"{m}:{s:02d}"


def arrow(recent_val, prior_val, higher_is_better=True):
    if prior_val == 0 and recent_val == 0:
        return "stable"
    if prior_val == 0:
        return "improving" if recent_val > 0 else "stable"
    diff = recent_val - prior_val
    rel = diff / prior_val if prior_val else 0
    if abs(rel) < 0.05:
        return "stable"
    improving = (diff > 0) if higher_is_better else (diff < 0)
    return "improving" if improving else "declining"


# ---- Report ----
def print_race_forecast():
    race = config.active_race()
    race_m = race["distance_mi"] * 1609.34
    goal_sec = config.goal_time_to_sec(race["goal_time"])
    goal_pace = race["goal_pace_min_per_mi"]

    activities = load_activities()
    current_vdot, src = current_fitness_vdot(activities)
    goal_vdot = compute_vdot(race_m, goal_sec)
    trend = fitness_trend(activities)
    vol_14 = weekly_volume_mi(activities, days=14)
    pred_sec = predict_race_time(current_vdot, race_m)
    prob = race_probability(current_vdot, goal_vdot, trend, vol_14)
    days_to = (date.fromisoformat(race["date"]) - date.today()).days

    if prob < 0.40:
        verdict = "LOW - fitness gap"
    elif prob < 0.65:
        verdict = "MODERATE - within reach"
    else:
        verdict = "STRONG - on track"

    print("=" * 60)
    print(f"  RACE FORECAST  |  {race['name']}, "
          f"{date.fromisoformat(race['date']).strftime('%b %d %Y')}")
    print("=" * 60)
    print()
    print(f"Days to race: {days_to}")
    print(f"Goal: {race['goal_time']} ({fmt_pace_min_mi(goal_pace)}/mi)")
    print()
    print(f"Current fitness VDOT: {current_vdot:.1f}")
    if isinstance(src, dict):
        note = src.get("note", "recent run")
        date_s = src.get("date", "")
        pace_s = src.get("pace_str", "")
        name = src.get("name", "")
        dist_s = ""
        if src.get("distance_mi"):
            dist_s = f"{src['distance_mi']:.1f}mi "
        line = f"  Source: {note}"
        if dist_s:
            line += f" ({dist_s.strip()})"
        if date_s:
            line += f" on {date_s}"
        if pace_s:
            line += f" @ {pace_s}/mi"
        if name:
            line += f" [{name}]"
        print(line)
    else:
        print(f"  Source: insufficient recent data")
    print()
    print(f"Goal VDOT ({race['goal_time']}): {goal_vdot:.1f}")
    print(f"Gap: {current_vdot - goal_vdot:+.1f} VDOT")
    print()
    print(
        f"Predicted finish time: {fmt_time(pred_sec)} "
        f"({fmt_pace_per_mi(pred_sec, race_m)})"
    )
    print()
    print(f"Goal ({race['goal_time']}) probability: "
          f"{int(round(prob * 100))}%  [{verdict}]")
    print()
    print("What you need to do:")
    gap = current_vdot - goal_vdot
    if gap < -1.5:
        print("- Hit at least one quality run with HR >= 165 in next 10 days")
        print("- Long run progression: aim for 11+ mi this weekend")
        print("- Maintain consistency: 4+ runs/week through race week")
    elif gap < 0:
        print("- One tempo session (3-4mi @ 8:50-9:00) in next 7 days")
        print("- Long run: 11-12 mi at 10:00-10:30")
        print("- Hold 4 runs/week, taper last 5 days")
    else:
        print("- Don't add stress; protect what you have")
        print("- One race-pace tune-up: 3 x 1mi @ 9:00 with 90s rest")
        print("- Taper last 7-10 days, hydrate, sleep")
    if vol_14 < 12:
        print(f"- Volume is light ({vol_14:.0f}mi/wk); add 1 easy 4mi this week")
    print()
    print("Trend (last 14 days vs prior 30):")
    r = trend["recent"]
    p = trend["prior"]
    vol_arrow = arrow(r["vol_per_wk"], p["vol_per_wk"])
    vdot_arrow = arrow(r["best_vdot"], p["best_vdot"])
    long_arrow = arrow(r["long"], p["long"])
    print(
        f"  Volume:   {vol_arrow} ({r['vol_per_wk']:.0f}mi/wk vs "
        f"{p['vol_per_wk']:.0f}mi/wk)"
    )
    print(f"  VDOT:     {vdot_arrow} ({r['best_vdot']:.1f} vs {p['best_vdot']:.1f})")
    print(f"  Long run: {long_arrow} ({r['long']:.1f}mi vs {p['long']:.1f}mi peak)")


if __name__ == "__main__":
    print_race_forecast()
