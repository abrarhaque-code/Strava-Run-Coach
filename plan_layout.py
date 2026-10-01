"""Turn a plan week's targets into seven day-level sessions.

Everything that needs to know "what is on Wednesday" reads from here: the
daily brief, the weekly check-in's 7-day layout, the calendar feed and the
run review's plan context. A generated plan carries its `days` already (the
generator calls distribute_week); a hand-written plan without `days` gets the
same layout computed on the fly from the athlete's `plan` preferences.

The layout rules, in order:
  1. The long run sits on `long_run_day`.
  2. The quality session sits on `quality_day`, never the day before the long
     run (it moves one day earlier if it would), and only when the week has
     quality to do.
  3. The remaining run days fill from a preference order, skipping `rest_days`.
  4. Easy days get at least MIN_EASY_MI; if the pool is too thin, a run day is
     dropped rather than prescribing 1.5-mile runs.
  5. Lifts land on `lift_days`, moved off the day before the long run.
Stdlib only; imports config, units, marathon_plan and models.
"""

import re
from datetime import date, timedelta
from typing import Optional

import config
import units
import marathon_plan as mp

DOWS = mp.DOWS
PREFERENCE = ("tue", "thu", "sun", "mon", "fri", "wed", "sat")
MIN_EASY_MI = 2.0
KEY_FRAC, KEY_MIN_MI, KEY_MAX_MI = 0.30, 3.0, 10.0
QUALITY_LEVELS = ("none", "strides", "tempo", "full")

# quality kind -> key into plan.paces
PACE_FOR_KIND = {"mp": "marathon_pace", "tempo": "tempo", "threshold": "threshold",
                 "intervals": "vo2max", "strides": "easy", "none": "easy"}

# A standing note that says any of these turns quality days easy while it stands.
_NO_QUALITY_RE = re.compile(
    r"no\s+(speed\s*work|quality|tempo|intervals?|hard|workouts?|track)|easy\s+only|injur",
    re.I)


def _dow(d: date) -> str:
    return DOWS[d.weekday()]


def _norm_dow(x) -> Optional[str]:
    if x is None:
        return None
    s = str(x).strip().lower()[:3]
    return s if s in DOWS else None


def resolve_layout(prefs: Optional[dict] = None) -> dict:
    """The athlete's `plan` preferences, validated and defaulted."""
    base = config.plan_cfg()
    if prefs:
        base.update({k: v for k, v in prefs.items() if v is not None})
    days = int(base.get("days_per_week") or 5)
    days = max(3, min(7, days))
    long_day = _norm_dow(base.get("long_run_day")) or "sat"
    quality_day = _norm_dow(base.get("quality_day")) or "wed"
    rest = [d for d in (_norm_dow(x) for x in (base.get("rest_days") or [])) if d]
    lifts = [d for d in (_norm_dow(x) for x in (base.get("lift_days") or [])) if d]
    level = str(base.get("quality") or "full").lower()
    if level not in QUALITY_LEVELS:
        level = "full"
    if quality_day == long_day:
        quality_day = _shift(long_day, -2)
    return {"days_per_week": days, "long_run_day": long_day, "quality_day": quality_day,
            "rest_days": [d for d in rest if d != long_day], "lift_days": lifts,
            "quality": level, "max_long_run_mi": base.get("max_long_run_mi"),
            "long_run_time_cap_min": base.get("long_run_time_cap_min")}


def _shift(dow: str, n: int) -> str:
    return DOWS[(DOWS.index(dow) + n) % 7]


def quality_kind(phase: str, level: str, race_distance_mi: float = 26.2,
                 is_race_week: bool = False) -> str:
    """What the week's key session is, from the phase and the athlete's
    quality level (none | strides | tempo | full)."""
    if level == "none":
        return "none"
    if is_race_week or phase in ("taper", "recovery", "base") or level == "strides":
        return "strides"
    if phase == "peak":
        if level == "tempo":
            return "tempo"
        return "mp" if race_distance_mi >= 20 else ("tempo" if race_distance_mi >= 10 else "intervals")
    # build
    if race_distance_mi < 10 and level == "full":
        return "intervals"
    return "tempo"


def _hr_cap(role: str, kind: Optional[str]) -> Optional[int]:
    if role == "easy":
        return config.easy_hr_cap()
    if role == "long":
        return config.long_run_hr_cap()
    if role == "key":
        pz = config.pace_zones()
        if kind == "mp":
            return config.race_pace_hr_range()[1]
        if kind == "tempo":
            return int((pz.get("tempo") or {}).get("hr_range", [150, 160])[1])
        if kind == "threshold":
            return int((pz.get("threshold") or {}).get("hr_range", [160, 170])[1])
    return None


def _key_description(kind: str, key_mi: float, quality: dict) -> str:
    """One line a runner can act on, in their unit."""
    d = units.fmt_dist(mi=key_mi)
    q_mi = quality.get("miles")
    if kind == "mp":
        return f"{d}: easy warm-up, {units.fmt_dist(mi=q_mi or key_mi * 0.6)} at marathon pace, easy cool-down"
    if kind == "tempo":
        return f"{d}: 1 easy, {units.fmt_dist(mi=q_mi or key_mi * 0.55, label=False)} at tempo, 1 easy"
    if kind == "threshold":
        return f"{d}: easy warm-up, {units.fmt_dist(mi=q_mi or key_mi * 0.5)} at threshold, easy cool-down"
    if kind == "intervals":
        reps, rep_m = quality.get("reps") or 6, quality.get("rep_m") or 800
        return f"{d}: warm-up, {reps} x {int(rep_m)}m at 5k effort with jog recoveries, cool-down"
    if kind == "strides":
        return f"{d} easy with 4-6 x 15 s strides"
    return f"{d} easy"


def distribute_week(week: dict, layout: Optional[dict] = None,
                    race_distance_mi: float = 26.2, race_date: Optional[date] = None) -> list:
    """Seven day dicts for a plan week: {dow, date, role, distance_mi, pace,
    hr_cap, strides, strength, description}. See the module docstring."""
    layout = layout or resolve_layout()
    start = date.fromisoformat(week["start_date"])
    T = float(week.get("target_miles") or 0)
    L = float(week.get("long_run_target") or 0)
    phase = week.get("phase", "build")
    quality = week.get("quality") or {}
    is_race_week = race_date is not None and start <= race_date < start + timedelta(days=7)
    kind = quality.get("kind") or quality_kind(phase, layout["quality"], race_distance_mi,
                                                is_race_week)
    level = layout["quality"]

    long_day = layout["long_run_day"]
    quality_day = layout["quality_day"]
    if quality_day == _shift(long_day, -1):
        quality_day = _shift(quality_day, -1)
    use_key = kind not in ("none", "strides") and not is_race_week and L >= 0
    race_dow = _dow(race_date) if is_race_week else None

    # --- which days run ---
    run_days = []
    if is_race_week:
        run_days.append(race_dow)
    elif L > 0:
        run_days.append(long_day)
    if use_key and quality_day not in run_days:
        run_days.append(quality_day)
    rest_days = set(layout["rest_days"])
    if race_dow:
        rest_days.add(_shift(race_dow, -1))      # the day before the race is not a run day
    for d in PREFERENCE:
        if len(run_days) >= layout["days_per_week"]:
            break
        if d in run_days or d in rest_days:
            continue
        run_days.append(d)
    if len(run_days) < layout["days_per_week"]:
        for d in PREFERENCE:
            if len(run_days) >= layout["days_per_week"]:
                break
            if d not in run_days and d != (race_dow and _shift(race_dow, -1)):
                run_days.append(d)

    # --- distances ---
    long_mi = 0.0 if is_race_week else L
    key_mi = max(KEY_MIN_MI, min(KEY_MAX_MI, KEY_FRAC * max(0.0, T - long_mi))) if use_key else 0.0
    if use_key and key_mi > T - long_mi:
        key_mi = max(0.0, T - long_mi)
    race_mi = race_distance_mi if is_race_week else 0.0
    easy_days = [d for d in run_days if d not in (long_day if long_mi else None,
                                                   quality_day if use_key else None, race_dow)]
    pool = max(0.0, T - long_mi - key_mi - (0.0 if is_race_week else 0.0))
    if is_race_week:
        pool = max(0.0, min(pool, 0.3 * T))     # race week: short shakeouts only
    while easy_days and len(easy_days) > 1 and pool / len(easy_days) < MIN_EASY_MI:
        # drop the least-preferred easy day rather than prescribe scraps
        drop = sorted(easy_days, key=lambda d: PREFERENCE.index(d))[-1]
        easy_days.remove(drop)
        run_days.remove(drop)
    easy_each = units.round_dist(pool / len(easy_days)) if easy_days else 0.0
    residue = pool - easy_each * len(easy_days)

    # --- strides and lifts ---
    strides_day = None
    if level != "none" and (kind == "strides" or (level == "strides") or phase in ("base", "taper")):
        if easy_days:
            strides_day = max(easy_days, key=lambda d: min((DOWS.index(d) - DOWS.index(long_day)) % 7,
                                                          (DOWS.index(long_day) - DOWS.index(d)) % 7))
    lift_days = set(layout["lift_days"])
    if long_mi and _shift(long_day, -1) in lift_days:
        lift_days.discard(_shift(long_day, -1))
        lift_days.add(_shift(long_day, 1))
    if race_dow:
        lift_days.discard(race_dow)
        lift_days.discard(_shift(race_dow, -1))

    # --- assemble ---
    first_easy = sorted(easy_days, key=lambda d: PREFERENCE.index(d))[0] if easy_days else None
    days = []
    for i, dow in enumerate(DOWS):
        d = {"dow": dow, "date": (start + timedelta(days=i)).isoformat(), "role": "rest",
             "distance_mi": 0.0, "pace": None, "hr_cap": None, "strides": 0,
             "strength": dow in lift_days, "description": "Rest"}
        if dow == race_dow:
            d.update(role="race", distance_mi=round(race_mi, 1), pace="marathon_pace",
                     hr_cap=None, description=f"RACE DAY: {units.fmt_dist(mi=race_mi)}")
        elif race_dow and dow == _shift(race_dow, -1):
            d.update(description="Rest. Feet up, bib pinned, bag packed.")
        elif dow == long_day and long_mi:
            d.update(role="long", distance_mi=round(long_mi, 1), pace="long_run",
                     hr_cap=_hr_cap("long", None),
                     description=f"Long run {units.fmt_dist(mi=long_mi)}"
                                 + (f" with {units.fmt_dist(mi=quality.get('miles'))} at marathon pace"
                                    if kind == "mp" and quality.get("miles") and not use_key else ""))
        elif dow == quality_day and use_key:
            d.update(role="key", distance_mi=round(key_mi, 1), pace=PACE_FOR_KIND.get(kind, "tempo"),
                     hr_cap=_hr_cap("key", kind), description="KEY: " + _key_description(kind, key_mi, quality))
        elif dow in easy_days:
            dist = easy_each + (residue if dow == first_easy else 0.0)
            dist = max(0.0, round(dist, 1))
            role = "shakeout" if is_race_week else "easy"
            d.update(role=role, distance_mi=dist, pace="easy", hr_cap=_hr_cap("easy", None),
                     description=f"{'Shakeout' if is_race_week else 'Easy'} {units.fmt_dist(mi=dist)}")
            if dow == strides_day:
                d["strides"] = 4
                d["description"] += " + 4 x 15 s strides"
        if d["strength"]:
            d["description"] += "  [lift]"
        days.append(d)
    return days


def sessions_for_week(week: dict, plan: Optional[dict] = None, layout: Optional[dict] = None) -> list:
    """The week's `days`, computed when the plan does not carry them."""
    if week.get("days"):
        return week["days"]
    plan = plan or (mp.load_plan() if mp.has_plan() else {})
    race = (plan or {}).get("race") or {}
    rd = None
    try:
        rd = date.fromisoformat(race["date"])
    except (KeyError, ValueError, TypeError):
        pass
    return distribute_week(week, layout, float(race.get("distance_mi") or 26.2), rd)


def session_for_date(d: Optional[date] = None, plan_week: Optional[dict] = None) -> Optional[dict]:
    """Today's session (slide-aware via marathon_plan.current_week), or None."""
    d = d or date.today()
    week = plan_week or (mp.current_week(d) if mp.has_plan() else None)
    if not week:
        return None
    return sessions_for_week(week)[d.weekday()]


def role_for_date(week: dict, d: date) -> Optional[str]:
    try:
        return sessions_for_week(week)[d.weekday()]["role"]
    except Exception:
        return None


def _pace_range(paces: dict, key: Optional[str]) -> str:
    if not key or key not in (paces or {}):
        return ""
    p = paces[key]
    if isinstance(p, dict):
        lo, hi = p.get("min"), p.get("max")
        if lo and hi:
            return units.fmt_pace_range(float(lo), float(hi))
        if lo or hi:
            return units.fmt_pace(float(lo or hi))
        return ""
    try:
        return units.fmt_pace(float(p))
    except (TypeError, ValueError):
        return ""


def format_session(day: dict, paces: Optional[dict] = None) -> str:
    """'KEY: 6.0 mi: 1 easy, 3.3 at tempo, 1 easy @ 8:50-9:10/mi, HR < 160  [lift]'"""
    desc = day.get("description") or day.get("role", "").title()
    bits = [desc.replace("  [lift]", "")]
    pr = _pace_range(paces or {}, day.get("pace")) if day.get("role") not in ("rest",) else ""
    if pr:
        bits.append(f"@ {pr}")
    if day.get("hr_cap"):
        bits.append(f"HR < {int(day['hr_cap'])}")
    out = ", ".join(bits[:1] + [", ".join(bits[1:])]) if len(bits) > 1 else bits[0]
    if day.get("strength"):
        out += "  [lift]"
    return out


def apply_notes(sessions: list, notes: list) -> list:
    """A standing note that rules out quality turns key days easy while it
    stands (distance kept). Returns new dicts; the plan file is untouched."""
    hits = [n for n in (notes or []) if _NO_QUALITY_RE.search(str(mp.normalize_note(n)["text"]))]
    if not hits:
        return list(sessions)
    why = mp.normalize_note(hits[0])["text"]
    out = []
    for d in sessions:
        d = dict(d)
        if d.get("role") == "key":
            d.update(role="easy", pace="easy", hr_cap=config.easy_hr_cap(), strides=0,
                     description=f"Easy {units.fmt_dist(mi=d.get('distance_mi') or 0)} "
                                 f"(per your note: {why})" + ("  [lift]" if d.get("strength") else ""))
        out.append(d)
    return out


def to_planned_workouts(day: dict, plan: Optional[dict] = None) -> list:
    """models.PlannedWorkout entries for the calendar feed (a run and/or a lift)."""
    from models import PlannedWorkout
    paces = (plan or {}).get("paces") or {}
    d = date.fromisoformat(day["date"])
    out = []
    role = day.get("role", "rest")
    if role != "rest":
        pace_key = day.get("pace")
        pace_val = None
        p = paces.get(pace_key) if pace_key else None
        if isinstance(p, dict) and p.get("max"):
            pace_val = float(p["max"])
        elif isinstance(p, (int, float)):
            pace_val = float(p)
        out.append(PlannedWorkout(
            day=d, workout_type=role, description=format_session(day, paces),
            distance_mi=float(day.get("distance_mi") or 0), target_pace=_pace_range(paces, pace_key),
            hr_cap=day.get("hr_cap"), notes="", pace_min_per_mi=pace_val))
    if day.get("strength"):
        out.append(PlannedWorkout(day=d, workout_type="lift",
                                  description="Strength: full body, off the legs before the long run.",
                                  distance_mi=0.0, notes=""))
    if role == "rest" and not day.get("strength"):
        out.append(PlannedWorkout(day=d, workout_type="rest", description="Rest or mobility.",
                                  distance_mi=0.0))
    return out
