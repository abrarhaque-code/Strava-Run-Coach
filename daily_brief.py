"""Daily brief — what to do today + how you should feel.

Pulls today's planned workout from the plan, current fatigue from the fitness
tracker, and gives a focused 5-line brief. Run in the morning before training.

Usage:
    python3 daily_brief.py            # print to stdout
    python3 daily_brief.py --save     # also write plan_output/brief.md
"""

import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import config


BRIEF_PATH = Path(__file__).parent / "plan_output" / "brief.md"


def _today_workout_from_plan():
    """Find today's workout from the active plan."""
    today = date.today()
    race = config.active_race(today)

    if not config.has_structured_plan(race):
        from planner import generate_half_plan
        plan = generate_half_plan(race)
        for week in plan.weeks:
            for w in week.workouts:
                if w.day == today:
                    return w, week
        return None, None

    # Structured (marathon) plan active: synthesize a workout from current week
    try:
        import marathon_plan as mp
        cw = mp.current_week(today)
        if not cw:
            return None, None
        # Determine day-of-week role
        weekday = today.weekday()  # 0=Mon, 5=Sat, 6=Sun
        # Synthetic workout dict masquerading as the planner.PlannedWorkout shape
        class _Synthetic:
            pass
        w = _Synthetic()
        w.day = today
        if weekday == 5 and cw["long_run_target"] > 0:
            w.workout_type = "long"
            w.description = f"Long run: {cw['long_run_target']}mi"
            w.target_pace = "10:00-10:30 (long run pace)"
            w.hr_cap = config.long_run_hr_cap()
            w.notes = cw.get("notes", "")
        elif weekday == 2 and cw.get("key_workout"):
            w.workout_type = "key"
            w.description = cw["key_workout"]
            w.target_pace = ""
            w.hr_cap = None
            w.notes = cw.get("notes", "")
        elif weekday in (0, 3):
            w.workout_type = "easy"
            w.description = "Easy run"
            w.target_pace = "10:00-10:30"
            w.hr_cap = config.easy_hr_cap()
            w.notes = ""
        elif weekday == 4:
            w.workout_type = "rest"
            w.description = "Rest"
            w.target_pace = ""
            w.hr_cap = None
            w.notes = "Friday rest before Saturday long run"
        elif weekday == 6:
            w.workout_type = "rest"
            w.description = "Rest"
            w.target_pace = ""
            w.hr_cap = None
            w.notes = "Sunday rest after Saturday long run"
        else:
            w.workout_type = "easy"
            w.description = "Easy run"
            w.target_pace = "10:00-10:30"
            w.hr_cap = config.easy_hr_cap()
            w.notes = ""
        return w, cw
    except Exception:
        return None, None


def _last_3_days_load() -> dict:
    """Look at last 3 days of training to gauge fatigue."""
    cache = Path(__file__).parent / "data" / "strava_cache" / "activities"
    if not cache.exists():
        return {"recent": [], "total_mi": 0, "total_min": 0, "avg_hr": 0}

    cutoff = datetime.now().astimezone() - timedelta(days=3)
    recent = []
    for p in cache.glob("*.json"):
        try:
            a = json.loads(p.read_text())
            dt = datetime.fromisoformat(a["start_date_local"].replace("Z", "+00:00"))
            # Compare without timezone for simplicity (start_date_local is naive)
            cutoff_naive = (datetime.now() - timedelta(days=3))
            dt_naive = dt.replace(tzinfo=None)
            if dt_naive >= cutoff_naive and a.get("type") == "Run":
                recent.append(a)
        except (json.JSONDecodeError, KeyError, ValueError):
            continue

    recent.sort(key=lambda x: x["start_date"], reverse=True)
    total_mi = sum((a.get("distance", 0) or 0) / 1609.34 for a in recent)
    total_min = sum((a.get("moving_time", 0) or 0) / 60 for a in recent)
    hrs = [a["average_heartrate"] for a in recent if a.get("average_heartrate")]
    avg_hr = sum(hrs) / len(hrs) if hrs else 0

    return {
        "recent": recent,
        "total_mi": total_mi,
        "total_min": total_min,
        "avg_hr": avg_hr,
    }


def _fatigue_status(load_3d: dict) -> tuple[str, str]:
    """Classify fatigue based on last 3 days. Returns (status, advice)."""
    miles = load_3d["total_mi"]
    runs = len(load_3d["recent"])
    avg_hr = load_3d["avg_hr"]

    if runs == 0:
        return "FRESH", "No runs in 3 days. Legs should be ready."
    if runs >= 3 and miles >= 12:
        return "FATIGUED", "Heavy 3-day block. Today should be easy or rest."
    if runs >= 2 and avg_hr > 155:
        return "ACCUMULATING", "Two recent hard efforts. Be honest with yourself today."
    if runs == 1 and miles >= 7:
        return "RECOVERING", "Long run in last 3 days. Easy effort only."
    if avg_hr < 140 and miles < 8:
        return "READY", "Recent runs were truly easy. You're set up for quality work."
    return "MODERATE", "Normal training rhythm. Today is what you make it."


def _race_countdown() -> tuple[int, str, str]:
    """Days to active race. Returns (days, phase, race_name)."""
    info = config.active_race()
    race = date.fromisoformat(info["date"])
    race_name = info["name"]

    days = (race - date.today()).days
    if days < 0:
        return days, "Race already happened", race_name
    if days == 0:
        return 0, "RACE DAY", race_name
    if days <= 7:
        return days, "TAPER WEEK — protect freshness", race_name
    if days <= 14:
        return days, "Sharpening — quality over volume", race_name
    if days <= 28:
        return days, "Peak block — every session matters", race_name
    return days, "Build phase", race_name


def build_brief() -> str:
    """Assemble the daily brief as a single string."""
    today = date.today()
    load_3d = _last_3_days_load()
    fatigue, fatigue_note = _fatigue_status(load_3d)
    days_to_race, phase, race_name = _race_countdown()
    workout, week = _today_workout_from_plan()

    # Consistency snapshot from metrics
    try:
        from metrics import (load_activities, current_streak,
                              eddington_progress, weeks_with_3plus_runs)
        runs = load_activities(activity_type="Run")
        streak = current_streak(runs)
        ep = eddington_progress(runs)
        w = weeks_with_3plus_runs(runs, weeks_window=8)
        consistency_line = (f"  Consistency: streak {streak}d  |  "
                             f"E={ep['current']} (need {ep['runs_needed_for_next']} "
                             f"more {ep['next_n']}+mi for E{ep['next_n']})  |  "
                             f"weeks @ 3+: {w['recent_4_weeks_3plus']}/4")
    except Exception:
        consistency_line = None

    out = []
    out.append("")
    out.append("=" * 60)
    out.append(f"  DAILY BRIEF  |  {today.strftime('%A, %B %d, %Y')}")
    out.append("=" * 60)
    out.append("")
    out.append(f"  Race:       {race_name}  |  {days_to_race} days  |  {phase}")
    out.append(f"  Fatigue:    {fatigue}  ({load_3d['total_mi']:.1f}mi in last 3 days, "
               f"avg HR {load_3d['avg_hr']:.0f})")
    out.append(f"  -> {fatigue_note}")
    if consistency_line:
        out.append("")
        out.append(consistency_line)
    out.append("")
    out.append("-" * 60)
    out.append("  TODAY'S WORKOUT")
    out.append("-" * 60)
    if workout:
        wt = workout.workout_type.upper()
        out.append(f"  Type:       {wt}")
        out.append(f"  Plan:       {workout.description}")
        if workout.target_pace:
            out.append(f"  Pace:       {workout.target_pace}")
        if workout.hr_cap:
            out.append(f"  HR cap:     {workout.hr_cap}")
        if workout.notes:
            out.append(f"  Notes:      {workout.notes}")
    else:
        out.append(f"  No workout scheduled for {today}.")
        out.append(f"  Today may be outside the current plan window.")
        out.append(f"  Re-run python3 planner.py to regenerate the short-race plan.")
    out.append("")
    out.append("-" * 60)
    out.append("  LAST 3 DAYS")
    out.append("-" * 60)
    if load_3d["recent"]:
        for r in load_3d["recent"]:
            dt = r["start_date_local"][:10]
            dist = (r.get("distance", 0) or 0) / 1609.34
            mt = (r.get("moving_time", 0) or 0) / 60
            pace = mt / dist if dist > 0 else 0
            pace_str = f"{int(pace)}:{int((pace%1)*60):02d}/mi" if pace > 0 else "N/A"
            hr = r.get("average_heartrate", "—")
            out.append(f"  {dt} | {dist:.1f}mi | {pace_str:>9} | HR {hr}")
    else:
        out.append("  No runs in last 3 days.")
    out.append("")
    out.append("=" * 60)
    out.append("  Honor the work. Show up. Run the plan.")
    out.append("=" * 60)
    out.append("")
    return "\n".join(out)


def save_brief(text: str | None = None) -> Path:
    """Write the brief to plan_output/brief.md and return the path."""
    if text is None:
        text = build_brief()
    BRIEF_PATH.parent.mkdir(parents=True, exist_ok=True)
    BRIEF_PATH.write_text(text, encoding="utf-8")
    return BRIEF_PATH


def print_brief(save: bool = False):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    text = build_brief()
    print(text)
    if save:
        save_brief(text)


if __name__ == "__main__":
    save_flag = "--save" in sys.argv[1:]
    print_brief(save=save_flag)
