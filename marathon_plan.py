"""Marathon plan loader + helpers.

Reads data/marathon_plan.json (the source of truth for the 24-week plan) and
data/plan_state.json (slide offset + per-week status). Provides typed helpers
that downstream modules (plan_tracker, dashboard, daily_brief) consume.

Usage:
    from marathon_plan import current_week, current_phase, race_info, all_weeks

    wk = current_week()  # which week am I in (slide-aware)
    print(wk["target_miles"], wk["long_run_target"])
"""

import json
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Optional


PLAN_PATH = Path(__file__).parent / "data" / "marathon_plan.json"
STATE_PATH = Path(__file__).parent / "data" / "plan_state.json"


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def load_plan() -> dict:
    """Load and validate the marathon plan JSON. Cached after first call."""
    if not PLAN_PATH.exists():
        raise FileNotFoundError(f"Marathon plan not found at {PLAN_PATH}")
    plan = json.loads(PLAN_PATH.read_text(encoding="utf-8"))
    _validate(plan)
    return plan


def load_state() -> dict:
    """Load plan state (slide offset, week status). Returns defaults if missing."""
    if not STATE_PATH.exists():
        return {
            "active_race": "auto",
            "slide_offset_weeks": 0,
            "weeks_status": {},
        }
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {
            "active_race": "auto",
            "slide_offset_weeks": 0,
            "weeks_status": {},
        }


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _validate(plan: dict) -> None:
    """Sanity-check the plan structure. Raises ValueError if malformed."""
    required_top = {"race", "paces", "phases", "weeks", "decision_points"}
    missing = required_top - set(plan.keys())
    if missing:
        raise ValueError(f"Plan missing top-level keys: {missing}")

    weeks = plan["weeks"]
    if not weeks:
        raise ValueError("Plan has no weeks")

    # Check week numbering is contiguous starting at 1
    expected_nums = list(range(1, len(weeks) + 1))
    actual_nums = [w["week_num"] for w in weeks]
    if actual_nums != expected_nums:
        raise ValueError(f"Week numbers must be contiguous 1..N, got {actual_nums}")

    # Check start dates are 7 days apart
    prev = None
    for w in weeks:
        try:
            d = date.fromisoformat(w["start_date"])
        except (ValueError, KeyError) as e:
            raise ValueError(f"Week {w.get('week_num')}: bad start_date: {e}")
        if prev is not None:
            gap = (d - prev).days
            if gap != 7:
                raise ValueError(
                    f"Week {w['week_num']} start_date {d} not 7 days after previous {prev}"
                )
        prev = d

    # Check phases reference valid week ranges
    for ph in plan["phases"]:
        if ph["start_week"] < 1 or ph["end_week"] > len(weeks):
            raise ValueError(f"Phase {ph['id']} has out-of-range week numbers")


# ---------------------------------------------------------------------------
# Slide-aware lookup
# ---------------------------------------------------------------------------

def race_info() -> dict:
    return load_plan()["race"]


def race_date() -> date:
    return date.fromisoformat(race_info()["date"])


def paces() -> dict:
    return load_plan()["paces"]


def all_weeks() -> list:
    return load_plan()["weeks"]


def all_phases() -> list:
    return load_plan()["phases"]


def all_decision_points() -> list:
    return load_plan()["decision_points"]


def week_by_num(n: int) -> Optional[dict]:
    """Return the plan week with the given week_num, or None."""
    for w in all_weeks():
        if w["week_num"] == n:
            return w
    return None


def phase_by_id(phase_id: str) -> Optional[dict]:
    for ph in all_phases():
        if ph["id"] == phase_id:
            return ph
    return None


def current_week(today: Optional[date] = None) -> Optional[dict]:
    """Which plan week is today in?

    Slide-aware: if state has slide_offset_weeks > 0, the calendar lookup
    shifts by that offset (you are "behind" by N weeks vs. the original plan).
    Returns None if today is before plan start or after race day.
    """
    if today is None:
        today = date.today()
    state = load_state()
    offset = state.get("slide_offset_weeks", 0)

    weeks = all_weeks()
    if not weeks:
        return None

    plan_start = date.fromisoformat(weeks[0]["start_date"])
    # Effective today: subtract slide offset (so a slide pulls today "back")
    effective_today = today - timedelta(weeks=offset)

    if effective_today < plan_start:
        return None  # before plan start

    days_in = (effective_today - plan_start).days
    week_idx = days_in // 7
    if week_idx >= len(weeks):
        return None  # past plan end

    return weeks[week_idx]


def current_phase(today: Optional[date] = None) -> Optional[dict]:
    wk = current_week(today)
    if not wk:
        return None
    return phase_by_id(wk["phase"])


def total_weeks() -> int:
    return len(all_weeks())


def days_to_race(today: Optional[date] = None) -> int:
    if today is None:
        today = date.today()
    return (race_date() - today).days


# ---------------------------------------------------------------------------
# CLI sanity check
# ---------------------------------------------------------------------------

def _cli():
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    plan = load_plan()
    print(f"Race: {plan['race']['name']} on {plan['race']['date']}")
    print(f"Goal: {plan['race']['goal_time']} ({plan['race']['goal_pace_min_per_mi']:.2f}/mi)")
    print(f"Total weeks: {len(plan['weeks'])}")
    print(f"Phases: {len(plan['phases'])}")
    print(f"Decision points: {len(plan['decision_points'])}")
    print()
    cw = current_week()
    if cw:
        ph = phase_by_id(cw["phase"])
        print(f"Today's plan week: #{cw['week_num']} ({ph['name']})")
        print(f"  Target: {cw['target_miles']} mi, long run {cw['long_run_target']} mi")
        if cw.get("key_workout"):
            print(f"  Key workout: {cw['key_workout']}")
        print(f"  Notes: {cw.get('notes', '')}")
    else:
        print("Today is outside the marathon plan window")
        print(f"  Plan starts: {plan['weeks'][0]['start_date']}")
        print(f"  Plan ends: {plan['weeks'][-1]['start_date']} (week 24)")
        print(f"  Days to race: {days_to_race()}")


if __name__ == "__main__":
    _cli()
