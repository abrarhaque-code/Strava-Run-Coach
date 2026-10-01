"""Training plan loader + helpers.

Reads the active race's plan JSON (config.plan_path: data/<race_id>.generated.json
by default, written by `coach.py plan`) and data/plan_state.json (slide offset,
per-week status, reconciled actuals, notes). Provides typed helpers that
downstream modules (plan_tracker, reconcile, weekly_check, daily_brief,
dashboard, post_run_review) consume.

Usage:
    from marathon_plan import current_week, current_phase, race_info, all_weeks

    wk = current_week()  # which week am I in (slide-aware)
    print(wk["target_miles"], wk["long_run_target"])

Schema: see docs/examples/plan.example.json. Weeks carry targets and
optionally `days` (seven day-level sessions), `quality` (the week's key
session, structured) and `long_run_time_cap_min`.
"""

import json
import re
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Optional

import config


# Tests point this at a file; None means "resolve from the active race".
PLAN_PATH: Optional[Path] = None
STATE_PATH = config.STATE_PATH

DOWS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DAY_ROLES = ("easy", "key", "long", "rest", "crosstrain", "shakeout", "race")
QUALITY_KINDS = ("mp", "tempo", "threshold", "intervals", "strides", "none")


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def _plan_file() -> Path:
    if PLAN_PATH is not None:
        return Path(PLAN_PATH)
    return config.plan_path(config.active_race())


def has_plan() -> bool:
    """True when a plan JSON exists for the active race. Never raises."""
    try:
        return _plan_file().exists()
    except Exception:
        return False


@lru_cache(maxsize=1)
def load_plan() -> dict:
    """Load and validate the plan JSON. Cached after first call."""
    path = _plan_file()
    if not path.exists():
        raise FileNotFoundError(
            f"No training plan at {path}. Generate one: python3 coach.py plan --from-data")
    plan = json.loads(path.read_text(encoding="utf-8"))
    _validate(plan)
    return plan


STATE_SCHEMA_VERSION = 3

STATE_DEFAULTS = {
    "schema_version": STATE_SCHEMA_VERSION,
    "active_race": "auto",
    "slide_offset_weeks": 0,
    "weeks_status": {},         # week_num -> "complete"|"missed"|...
    "weeks_status_source": {},  # week_num -> "manual"|"auto"
    "weeks_actuals": {},        # week_num -> reconciled actuals dict
    "notes": {},                # week_num|"general" -> [note entries, see normalize_note]
    "last_reconciled": None,
}

# Keys whose defaults are mutable containers: re-created on every load so two
# load_state() calls in one process never share (and silently co-mutate) one.
_CONTAINER_KEYS = ("weeks_status", "weeks_status_source", "weeks_actuals", "notes")


def load_state() -> dict:
    """Load plan state (slide offset, week status, reconciled actuals, notes).

    Returns defaults if the file is missing or corrupt; always contains
    every STATE_DEFAULTS key so readers can .get() without guards. A v2 file
    reads fine and upgrades on its next save.
    """
    state = dict(STATE_DEFAULTS)
    for key in _CONTAINER_KEYS:
        state[key] = {}
    if not STATE_PATH.exists():
        return state
    try:
        loaded = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return state
    state.update(loaded)
    try:
        version = int(loaded.get("schema_version", 2))
    except (TypeError, ValueError):
        version = 2
    state["schema_version"] = max(version, STATE_SCHEMA_VERSION)
    return state


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

    expected_nums = list(range(1, len(weeks) + 1))
    actual_nums = [w["week_num"] for w in weeks]
    if actual_nums != expected_nums:
        raise ValueError(f"Week numbers must be contiguous 1..N, got {actual_nums}")

    paces = plan.get("paces") or {}
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
        _validate_week_extras(w, d, paces)

    for ph in plan["phases"]:
        if ph["start_week"] < 1 or ph["end_week"] > len(weeks):
            raise ValueError(f"Phase {ph['id']} has out-of-range week numbers")


def _validate_week_extras(w: dict, start: date, paces: dict) -> None:
    """Optional day-level sessions, structured quality, and the clock cap."""
    n = w.get("week_num")
    days = w.get("days")
    if days is not None:
        if not isinstance(days, list) or len(days) != 7:
            raise ValueError(f"Week {n}: days must be a list of exactly 7 sessions")
        for i, d in enumerate(days):
            if not isinstance(d, dict):
                raise ValueError(f"Week {n}: day {i + 1} is not an object")
            if d.get("dow") not in DOWS:
                raise ValueError(f"Week {n}: day {i + 1} has bad dow {d.get('dow')!r}")
            if d.get("date"):
                try:
                    dd = date.fromisoformat(d["date"])
                except ValueError as e:
                    raise ValueError(f"Week {n}: day {i + 1} bad date: {e}")
                if dd != start + timedelta(days=i):
                    raise ValueError(f"Week {n}: day {i + 1} date {dd} does not sit on "
                                     f"{start + timedelta(days=i)}")
            if d.get("role") not in DAY_ROLES:
                raise ValueError(f"Week {n}: day {i + 1} has bad role {d.get('role')!r}")
            dist = d.get("distance_mi", 0) or 0
            if not isinstance(dist, (int, float)) or dist < 0:
                raise ValueError(f"Week {n}: day {i + 1} has bad distance_mi {dist!r}")
            pk = d.get("pace")
            if pk is not None and pk not in paces:
                raise ValueError(f"Week {n}: day {i + 1} pace {pk!r} is not a key in plan.paces")
    q = w.get("quality")
    if q is not None:
        if not isinstance(q, dict) or q.get("kind") not in QUALITY_KINDS:
            raise ValueError(f"Week {n}: quality.kind must be one of {QUALITY_KINDS}")
    cap = w.get("long_run_time_cap_min")
    if cap is not None and (not isinstance(cap, (int, float)) or cap <= 0):
        raise ValueError(f"Week {n}: long_run_time_cap_min must be a positive number")


# ---------------------------------------------------------------------------
# Plan accessors
# ---------------------------------------------------------------------------

def race_info() -> dict:
    return load_plan()["race"]


def race_date() -> date:
    return date.fromisoformat(race_info()["date"])


def paces() -> dict:
    return load_plan()["paces"]


def goal_mp_pace() -> float:
    """Goal race pace in min/mi: `paces.marathon_pace`, else
    `race.goal_pace_min_per_mi`. Raises if neither exists (no silent literal)."""
    p = paces().get("marathon_pace")
    if p:
        return float(p)
    return float(race_info()["goal_pace_min_per_mi"])


COMPLIANCE_DEFAULTS = {
    "repeat_below_pct": 0.8,    # a week under this share of target_miles repeats
    "missed_below_pct": 0.5,    # under this it is a MISS
    "long_run_hit_pct": 0.9,    # the long run counts as hit at this share of its target
}


def compliance_thresholds() -> dict:
    """COMPLIANCE_DEFAULTS overridden by the plan's `_meta.compliance_thresholds`."""
    out = dict(COMPLIANCE_DEFAULTS)
    try:
        override = load_plan().get("_meta", {}).get("compliance_thresholds") or {}
        for k, v in override.items():
            if k in out:
                out[k] = float(v)
    except Exception:
        pass
    return out


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
    Returns None if today is before plan start or after the last week.
    """
    if today is None:
        today = date.today()
    state = load_state()
    offset = state.get("slide_offset_weeks", 0)

    weeks = all_weeks()
    if not weeks:
        return None

    plan_start = date.fromisoformat(weeks[0]["start_date"])
    effective_today = today - timedelta(weeks=offset)

    if effective_today < plan_start:
        return None

    days_in = (effective_today - plan_start).days
    week_idx = days_in // 7
    if week_idx >= len(weeks):
        return None

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
# Notes (in-the-moment adjustments, optionally standing until a date)
# ---------------------------------------------------------------------------

_LEGACY_NOTE = re.compile(r"^(\d{4}-\d{2}-\d{2}): (.*)$", re.S)


def normalize_note(entry) -> dict:
    """Legacy '2026-05-19: text' strings and {date, text, until} dicts both
    read as {date, text, until}."""
    if isinstance(entry, dict):
        return {"date": entry.get("date"), "text": str(entry.get("text", "")),
                "until": entry.get("until")}
    s = str(entry)
    m = _LEGACY_NOTE.match(s)
    if m:
        return {"date": m.group(1), "text": m.group(2), "until": None}
    return {"date": None, "text": s, "until": None}


def format_note(n: dict) -> str:
    n = normalize_note(n)
    out = f"{n['date']}: {n['text']}" if n.get("date") else n["text"]
    if n.get("until"):
        out += f" (until {n['until']})"
    return out


def notes_for_week(week_num, state: Optional[dict] = None) -> list:
    state = state if state is not None else load_state()
    return [normalize_note(x) for x in (state.get("notes", {}) or {}).get(str(week_num), [])]


def general_notes(state: Optional[dict] = None) -> list:
    return notes_for_week("general", state)


def standing_notes(today: Optional[date] = None, state: Optional[dict] = None) -> list:
    """Every note with an `until` date that has not passed, newest expiry last.
    This is how an injury or a travel week shapes the plan without regenerating it:
    `coach.py note "no speedwork, calf" --until 2026-11-15`."""
    today = today or date.today()
    state = state if state is not None else load_state()
    out = []
    for key, entries in (state.get("notes", {}) or {}).items():
        for e in entries or []:
            n = normalize_note(e)
            if not n.get("until"):
                continue
            try:
                until = date.fromisoformat(str(n["until"]))
            except ValueError:
                continue
            if until >= today:
                n["week"] = key
                out.append(n)
    out.sort(key=lambda n: n["until"])
    return out


# ---------------------------------------------------------------------------
# CLI sanity check
# ---------------------------------------------------------------------------

def _cli():
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    if not has_plan():
        print(f"No training plan at {_plan_file()}.")
        print("Generate one: python3 coach.py plan --from-data")
        return
    plan = load_plan()
    import units
    print(f"Race: {plan['race']['name']} on {plan['race']['date']}")
    print(f"Goal: {plan['race'].get('goal_time', '?')} "
          f"({units.fmt_pace(plan['race']['goal_pace_min_per_mi'])})")
    print(f"Total weeks: {len(plan['weeks'])}")
    print(f"Phases: {len(plan['phases'])}")
    print(f"Decision points: {len(plan['decision_points'])}")
    print()
    cw = current_week()
    if cw:
        ph = phase_by_id(cw["phase"])
        print(f"Today's plan week: #{cw['week_num']} ({ph['name'] if ph else cw['phase']})")
        print(f"  Target: {units.fmt_dist(mi=cw['target_miles'])}, "
              f"long run {units.fmt_dist(mi=cw['long_run_target'])}")
        if cw.get("key_workout"):
            print(f"  Key workout: {cw['key_workout']}")
        if cw.get("notes"):
            print(f"  Notes: {cw.get('notes', '')}")
    else:
        print("Today is outside the plan window")
        print(f"  Plan starts: {plan['weeks'][0]['start_date']}")
        print(f"  Last week starts: {plan['weeks'][-1]['start_date']} (week {len(plan['weeks'])})")
        print(f"  Days to race: {days_to_race()}")


if __name__ == "__main__":
    _cli()
