#!/usr/bin/env python3
"""Race time predictor and goal probability based on Jack Daniels VDOT.

Reads Strava cache JSONs in data/strava_cache/activities/, computes current
fitness VDOT from best recent effort, predicts the active race's finish time,
and estimates the probability of hitting the goal time.

Stdlib only.
"""
import math
from datetime import date, datetime, timedelta
from typing import Optional

import config
import units

# ---- Constants ----
HALF_M = 21097.5
ACTIVITIES_DIR = str(config.ACTIVITIES_DIR)
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
def load_activities(activities: list = None) -> list:
    """Flattened rows for the predictor: {id, name, date, distance_m,
    distance_mi, time_sec, pace_min_mi, avg_hr, max_hr, is_intervals}.

    Reads metrics.load_activities (the one cache reader) unless a pre-loaded
    list is passed, so callers that already hold the runs (the dashboard) do
    not load the cache twice. Real runs only: a VDOT anchored on a bike
    session logged as a Run would poison every prediction downstream.
    """
    if activities is None:
        from metrics import load_activities as _load
        activities = _load(activity_type="Run")
    from enrichment import is_real_run
    out = []
    for a in activities:
        if a.get("type") != "Run" or not is_real_run(a):
            continue
        dist_m = a.get("distance", 0) or 0
        time_s = a.get("moving_time", 0) or 0
        if dist_m <= 0 or time_s <= 0:
            continue
        date_str = a.get("start_date_local") or a.get("start_date")
        if not date_str:
            continue
        try:
            dt = datetime.fromisoformat(str(date_str).replace("Z", ""))
        except ValueError:
            continue
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
                "is_intervals": _is_intervals(a),
            }
        )
    return sorted(out, key=lambda x: x["date"])


def _is_intervals(a: dict) -> bool:
    """A rep session read as one continuous run inflates VDOT (the reps are
    fast, the recoveries are not counted as moving). Keep them out of the
    whole-run scan; their reps are scored by the review instead."""
    try:
        from enrichment import looks_like_intervals
        return bool(looks_like_intervals(a))
    except Exception:
        return False


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
    recent_whole = [a for a in activities if a["date"] >= cutoff and a["distance_m"] >= 3000
                    and not a.get("is_intervals")]
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
    return units.fmt_pace(sec_per_m=time_sec / distance_m, label=False)


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
    """Pace with its unit label ('9:05/mi' or '5:39/km')."""
    return units.fmt_pace(sec_per_m=total_sec / distance_m)


def fmt_pace_min_mi(pace_min_mi: float) -> str:
    """Format a pace given in minutes-per-mile (float) as M:SS in the user's unit."""
    return units.fmt_pace(pace_min_mi, label=False)


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
def _vol(mi_per_wk: float) -> str:
    return f"{units.mi_to_user(mi_per_wk):.0f} {units.volume_label()}"


def _plan_week(today: Optional[date] = None) -> Optional[dict]:
    try:
        import marathon_plan as mp
        return mp.current_week(today) if mp.has_plan() else None
    except Exception:
        return None


def _band(zone: dict) -> Optional[str]:
    try:
        return units.fmt_pace_range(float(zone["ceiling"]), float(zone["floor"]))
    except (KeyError, TypeError, ValueError):
        return None


def _tips(gap: float, vol_14_mi: float, race: dict, week: Optional[dict] = None) -> list:
    """What to do about the gap, in the athlete's numbers: the plan week's
    long run and volume when a plan exists, the config zones otherwise."""
    pz = config.pace_zones()
    days = int(config.plan_cfg().get("days_per_week") or 4)
    long_mi = float((week or {}).get("long_run_target") or 0)
    if not long_mi:
        long_mi = max(float(config.long_run_min_mi()),
                      round(0.5 * float(race.get("distance_mi") or 13.1)))
    tempo_band = _band(pz.get("tempo") or {}) or "tempo pace"
    easy_band = _band(pz.get("easy") or {}) or "easy pace"
    goal_pace = units.fmt_pace(float(race.get("goal_pace_min_per_mi") or 0))
    rep = "1 mi" if units.unit() == "mi" else "1 km"
    lo, hi = units.mi_to_user(3), units.mi_to_user(4)
    tips = []
    if gap < -1.5:
        tips += [f"Hit at least one quality run with HR >= {config.threshold_hr()} in the next 10 days",
                 f"Long run progression: aim for {units.fmt_dist(mi=long_mi)}+ this weekend",
                 f"Maintain consistency: {days}+ runs/week through race week"]
    elif gap < 0:
        tips += [f"One tempo session ({lo:.0f}-{hi:.0f} {units.unit()} @ {tempo_band}) in the next 7 days",
                 f"Long run: {units.fmt_dist(mi=long_mi)} at {easy_band}",
                 f"Hold {days} runs/week, taper the last 5 days"]
    else:
        tips += ["Don't add stress; protect what you have",
                 f"One race-pace tune-up: 3 x {rep} @ {goal_pace} with 90 s rest",
                 "Taper the last 7-10 days, hydrate, sleep"]
    light_mi = 0.6 * float(week["target_miles"]) if week and week.get("target_miles") else 12.0
    if vol_14_mi < light_mi:
        tips.append(f"Volume is light ({_vol(vol_14_mi)}); add one easy "
                    f"{units.fmt_dist(mi=4, nd=0)} this week")
    return tips


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
    print(f"Goal: {race['goal_time']} ({units.fmt_pace(goal_pace)})")
    print()
    print(f"Current fitness VDOT: {current_vdot:.1f}")
    if isinstance(src, dict):
        note = src.get("note", "recent run")
        date_s = src.get("date", "")
        pace_s = src.get("pace_str", "")
        name = src.get("name", "")
        line = f"  Source: {note}"
        if src.get("distance_mi"):
            line += f" ({units.fmt_dist(mi=src['distance_mi'])})"
        if date_s:
            line += f" on {date_s}"
        if pace_s:
            line += f" @ {pace_s}{units.pace_label()}"
        if name:
            line += f" [{name}]"
        print(line)
    else:
        print("  Source: insufficient recent data")
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
    for tip in _tips(gap, vol_14, race, _plan_week()):
        print(f"- {tip}")
    print()
    print("Trend (last 14 days vs prior 30):")
    r = trend["recent"]
    p = trend["prior"]
    vol_arrow = arrow(r["vol_per_wk"], p["vol_per_wk"])
    vdot_arrow = arrow(r["best_vdot"], p["best_vdot"])
    long_arrow = arrow(r["long"], p["long"])
    print(f"  Volume:   {vol_arrow} ({_vol(r['vol_per_wk'])} vs {_vol(p['vol_per_wk'])})")
    print(f"  VDOT:     {vdot_arrow} ({r['best_vdot']:.1f} vs {p['best_vdot']:.1f})")
    print(f"  Long run: {long_arrow} ({units.fmt_dist(mi=r['long'])} vs {units.fmt_dist(mi=p['long'])} peak)")


if __name__ == "__main__":
    print_race_forecast()
