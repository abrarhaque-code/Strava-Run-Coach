#!/usr/bin/env python3
"""Parametric marathon plan generator.

Turns a base-build entry point into a full week-by-week marathon block, emitting
the SAME JSON schema as data/marathon_plan.json so every downstream consumer
(marathon_plan.py, plan_tracker.py, weekly_check.py, dashboard.py) reads it
unchanged. Also converts a plan into a TrainingPlan of PlannedWorkouts so
ical_generator.py can push the week to an Outlook calendar.

The mileage ramp comes from scenario.build_ramp (cutbacks + taper + the
long-run growth guardrail), so the plan and the projection agree by
construction.

Usage:
    python3 plan_generator.py --entry 25 --weeks 16 [--ics]
    python3 coach.py plan --entry 25
"""

import json
import sys
from datetime import date, timedelta
from pathlib import Path

import config
import scenario
from models import PlannedWorkout, PlannedWeek, TrainingPlan, PaceZones

OUT_PATH = Path(__file__).parent / "data" / "marathon_plan.generated.json"

_PHASE_META = {
    "base":  {"name": "Base Building", "color": "#A9BCE6"},
    "build": {"name": "Build Block",   "color": "#2F56B3"},
    "peak":  {"name": "Peak",          "color": "#002FA7"},
    "taper": {"name": "Taper + Race",  "color": "#6E8BD0"},
}

_DAY = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


# ---------------------------------------------------------------------------
# Pace helpers
# ---------------------------------------------------------------------------

def _fmt_pace(p: float) -> str:
    m = int(p)
    s = int(round((p - m) * 60))
    if s == 60:
        m, s = m + 1, 0
    return f"{m}:{s:02d}"


def _paces_block(race: dict) -> dict:
    z = PaceZones.from_config()
    mp = float(race["goal_pace_min_per_mi"])
    return {
        "easy": {"min": round(z.easy_ceiling, 2), "max": round(z.easy_floor, 2)},
        "long_run": {"min": round(z.long_run_pace - 0.25, 2), "max": round(z.long_run_pace + 0.25, 2)},
        "marathon_pace": round(mp, 3),
        "tempo": {"min": round(z.tempo_ceiling, 2), "max": round(z.tempo_floor, 2)},
        "threshold": {"min": round(z.threshold_ceiling, 2), "max": round(z.threshold_floor, 2)},
        "vo2max": {"min": round(z.threshold_ceiling - 0.5, 2), "max": round(z.threshold_ceiling - 0.4, 2)},
    }


def _key_workout(phase: str, week_num: int, mp: float) -> str:
    mp_s = _fmt_pace(mp)
    if phase == "base":
        return "Easy week. 4-6 strides on one easy run. No quality yet."
    if phase == "build":
        return f"Tempo / marathon-pace work, e.g. 4-5mi @ {mp_s} inside the long run."
    if phase == "peak":
        return f"Long run with a sustained {mp_s} marathon-pace block (8-12mi)."
    return "Taper: strides only, sharpen the legs without adding fatigue."


def _phase_note(phase: str, week_num: int, total: int) -> str:
    if phase == "base":
        return "Aerobic base. All easy. Consistency over heroics."
    if phase == "build":
        return "Marathon-specific work begins. Absorb the cutback weeks."
    if phase == "peak":
        return "Biggest weeks of the block. The marathon-pace long run is the key session."
    return "Taper. Cut volume, keep a little intensity. Sleep and hydrate."


# ---------------------------------------------------------------------------
# Plan dict (marathon_plan.json schema)
# ---------------------------------------------------------------------------

def _race_week_monday(race_date: date) -> date:
    return race_date - timedelta(days=race_date.weekday())


def generate_plan_dict(entry_mi: float, weeks: int = None, today: date = None) -> dict:
    c = scenario.cfg()
    if weeks:
        c = dict(c)
        c["block_weeks"] = int(weeks)
    weeks = int(c["block_weeks"])
    race = config.active_race(today)
    race_date = date.fromisoformat(race["date"])
    mp = float(race["goal_pace_min_per_mi"])

    ramp = scenario.build_ramp(entry_mi, c)
    week_data = ramp["weeks"]

    week16_monday = _race_week_monday(race_date)
    week1_monday = week16_monday - timedelta(weeks=weeks - 1)

    weeks_out = []
    for i, w in enumerate(week_data):
        start = week1_monday + timedelta(weeks=i)
        phase = w["phase"]
        weeks_out.append({
            "week_num": w["week_num"],
            "start_date": start.isoformat(),
            "phase": phase,
            "target_miles": w["target_miles"],
            "long_run_target": w["long_run_target"],
            "target_tss": int(round(w["target_miles"] * 8)),
            "key_workout": _key_workout(phase, w["week_num"], mp),
            "notes": _phase_note(phase, w["week_num"], weeks),
        })

    # Contiguous phase ranges
    phases = []
    for w in weeks_out:
        if phases and phases[-1]["id"] == w["phase"]:
            phases[-1]["end_week"] = w["week_num"]
        else:
            meta = _PHASE_META.get(w["phase"], {"name": w["phase"].title(), "color": "#6E8BD0"})
            phases.append({
                "id": w["phase"], "name": meta["name"], "color": meta["color"],
                "start_week": w["week_num"], "end_week": w["week_num"],
            })

    decision_points = _decision_points(weeks_out, ramp, race)

    return {
        "_meta": {
            "schema_version": 1,
            "generated": True,
            "entry_mi": entry_mi,
            "peak_mi": ramp["peak_mi"],
            "avg_mi": ramp["avg_mi"],
            "long_run_peak": ramp["long_run_peak"],
            "description": (f"Auto-generated {weeks}-week plan from a {entry_mi:.0f} mpw "
                            f"entry point. Peak {ramp['peak_mi']:.0f} mpw, "
                            f"longest run {ramp['long_run_peak']:.0f}mi."),
        },
        "race": {
            "id": race["id"], "name": race["name"], "date": race["date"],
            "distance_mi": race["distance_mi"],
            "goal_time": race.get("goal_time", ""),
            "goal_pace_min_per_mi": mp,
            "vdot_required": race.get("vdot_required", 0),
        },
        "paces": _paces_block(race),
        "phases": phases,
        "weeks": weeks_out,
        "decision_points": decision_points,
    }


def _decision_points(weeks_out: list, ramp: dict, race: dict) -> list:
    """Checkpoints at end of base, end of build, end of peak."""
    def date_after(week_num):
        w = next((x for x in weeks_out if x["week_num"] == week_num), None)
        if not w:
            return ""
        return (date.fromisoformat(w["start_date"]) + timedelta(days=6)).isoformat()

    last_base = max((w["week_num"] for w in weeks_out if w["phase"] == "base"), default=4)
    last_build = max((w["week_num"] for w in weeks_out if w["phase"] == "build"), default=last_base + 4)
    last_peak = max((w["week_num"] for w in weeks_out if w["phase"] == "peak"), default=last_build + 3)
    peak = ramp["peak_mi"]
    lr_peak = ramp["long_run_peak"]
    return [
        {
            "id": "end_of_base", "after_week": last_base, "evaluate_date": date_after(last_base),
            "name": "End of Base Phase", "description": "Ready to add marathon-specific quality?",
            "criteria": [
                {"metric": "longest_run_30d_mi", "op": ">=", "value": round(lr_peak * 0.55, 0),
                 "label": f"Long run >= {round(lr_peak * 0.55):.0f}mi"},
                {"metric": "weeks_at_4plus_of_4", "op": ">=", "value": 3, "label": "3+ of last 4 weeks at 4+ runs"},
            ],
            "downgrade_action": "Hold base 1-2 more weeks; trim build targets 10%.",
        },
        {
            "id": "end_of_build", "after_week": last_build, "evaluate_date": date_after(last_build),
            "name": "End of Marathon-Specific Block", "description": "Is the goal still realistic?",
            "criteria": [
                {"metric": "longest_run_30d_mi", "op": ">=", "value": round(lr_peak * 0.85, 0),
                 "label": f"Long run >= {round(lr_peak * 0.85):.0f}mi"},
                {"metric": "weekly_mi_4wk_avg", "op": ">=", "value": round(peak * 0.8, 0),
                 "label": f"{round(peak * 0.8):.0f}+ mpw avg over last 4 weeks"},
            ],
            "downgrade_action": "Soften the goal; cap marathon-pace work; reduce peak mileage 10-15%.",
        },
        {
            "id": "end_of_peak", "after_week": last_peak, "evaluate_date": date_after(last_peak),
            "name": "End of Peak Phase", "description": "Race-day execution check",
            "criteria": [
                {"metric": "longest_run_30d_mi", "op": ">=", "value": round(lr_peak, 0),
                 "label": f"Peak long run {round(lr_peak):.0f}mi completed"},
            ],
            "downgrade_action": "Race conservatively; start at the slow end of the projected range.",
        },
    ]


# ---------------------------------------------------------------------------
# Validation + write
# ---------------------------------------------------------------------------

def write_plan(entry_mi: float, weeks: int = None, path: Path = OUT_PATH) -> Path:
    plan = generate_plan_dict(entry_mi, weeks)
    # Validate against the same rules the loader enforces.
    import marathon_plan
    marathon_plan._validate(plan)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plan, indent=2), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# TrainingPlan (for the ical calendar feed)
# ---------------------------------------------------------------------------

def _week_workouts(w: dict, start: date, paces: dict, race: dict, zones: PaceZones) -> list:
    """Mon-Sun workouts: long run Sat, key Wed, easy Tue/Thu, lifts on easy days."""
    easy = f"{_fmt_pace(zones.easy_ceiling)}-{_fmt_pace(zones.easy_floor)}"
    longp = f"{_fmt_pace(zones.long_run_pace)}+"
    target = w["target_miles"]
    long_mi = w["long_run_target"]

    # Spread non-long mileage across easy/key days
    remaining = max(0.0, target - long_mi)
    easy_each = round(remaining / 3.0, 1) if remaining > 0 else 0.0

    wk = []
    wk.append(PlannedWorkout(day=start, workout_type="rest",
              description="Rest or mobility.", distance_mi=0.0,
              notes="Recovery day. Optional easy lift (upper body)."))
    wk.append(PlannedWorkout(day=start + timedelta(days=1), workout_type="easy",
              description=f"{easy_each:.1f}mi easy @ {easy}", distance_mi=easy_each,
              target_pace=easy, hr_cap=zones.easy_hr_cap,
              notes="Truly easy. Z2 bike is a fine substitute (~10 min = 1 mi) if managing a niggle."))
    wk.append(PlannedWorkout(day=start + timedelta(days=2), workout_type="key",
              description=f"KEY: {w['key_workout']}", distance_mi=easy_each,
              hr_cap=zones.race_hr_cap, notes="The one quality session of the week."))
    wk.append(PlannedWorkout(day=start + timedelta(days=3), workout_type="easy",
              description=f"{easy_each:.1f}mi easy @ {easy}", distance_mi=easy_each,
              target_pace=easy, hr_cap=zones.easy_hr_cap, notes="Easy effort."))
    wk.append(PlannedWorkout(day=start + timedelta(days=4), workout_type="lift",
              description="Strength: full body, away from key run.", distance_mi=0.0,
              notes="Keep it off the legs the day before the long run."))
    wk.append(PlannedWorkout(day=start + timedelta(days=5), workout_type="long",
              description=f"{long_mi:.0f}mi long run @ {longp}", distance_mi=long_mi,
              target_pace=longp, hr_cap=zones.long_run_hr_cap,
              notes="Long run. Practice race fueling on the longer ones."))
    wk.append(PlannedWorkout(day=start + timedelta(days=6), workout_type="rest",
              description="Rest or recovery walk.", distance_mi=0.0))
    return wk


def to_training_plan(plan: dict) -> TrainingPlan:
    race = plan["race"]
    zones = PaceZones.from_config()
    tp = TrainingPlan(
        race_name=race["name"],
        race_date=date.fromisoformat(race["date"]),
        race_distance_mi=race["distance_mi"],
        goal_time=race.get("goal_time", ""),
        goal_pace=_fmt_pace(race["goal_pace_min_per_mi"]),
    )
    for w in plan["weeks"]:
        start = date.fromisoformat(w["start_date"])
        workouts = _week_workouts(w, start, plan["paces"], race, zones)
        tp.weeks.append(PlannedWeek(
            week_num=w["week_num"], week_start=start, phase=w["phase"],
            target_miles=w["target_miles"], long_run_target_mi=w["long_run_target"],
            key_workout=w["key_workout"], lift_sessions=1,
            lift_notes="Full body, off the legs before the long run.",
            workouts=workouts, notes=w["notes"],
        ))
    return tp


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args(argv):
    entry, weeks, want_ics = 25.0, None, False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a.startswith("--entry"):
            val = a.split("=", 1)[1] if "=" in a else argv[i + 1]
            if "=" not in a:
                i += 1
            entry = float(val)
        elif a.startswith("--weeks"):
            val = a.split("=", 1)[1] if "=" in a else argv[i + 1]
            if "=" not in a:
                i += 1
            weeks = int(val)
        elif a == "--ics":
            want_ics = True
        i += 1
    return entry, weeks, want_ics


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    entry, weeks, want_ics = _parse_args(argv)
    path = write_plan(entry, weeks)
    plan = json.loads(path.read_text(encoding="utf-8"))
    m = plan["_meta"]
    print(f"Generated {len(plan['weeks'])}-week plan -> {path}")
    print(f"  Entry {m['entry_mi']:.0f} mpw  ->  peak {m['peak_mi']:.0f} mpw  "
          f"(avg {m['avg_mi']:.0f}), longest run {m['long_run_peak']:.0f}mi")
    print(f"  Race: {plan['race']['name']} on {plan['race']['date']}")
    print(f"  Phases: {', '.join(p['name'] for p in plan['phases'])}")
    if want_ics:
        from ical_generator import write_plan_ics
        tp = to_training_plan(plan)
        write_plan_ics(tp, output_dir="plan_output")
        print("  Subscribe to plan_output/training.ics in Outlook (re-generate to update).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
