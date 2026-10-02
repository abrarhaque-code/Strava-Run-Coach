#!/usr/bin/env python3
"""Generate a personalised training plan for the active race.

    python3 coach.py plan --from-data                 # entry mileage from your last 4 weeks
    python3 coach.py plan --entry 25 --days 4 --long-day sun --quality strides
    python3 coach.py plan --from-data --ics            # + a subscribable calendar feed

The plan depends on the athlete, not on a template: entry mileage (typed, or
read from the cache), days per week, which day the long run and the key
session fall on, rest and lift days, and a quality level (none | strides |
tempo | full) that turns an injured or returning runner's weeks into easy
running without throwing the plan away. Preferences live in config.json's
`plan` block; flags override them for one run and the resolved set is
recorded in the plan's `_meta.inputs`.

Distance presets (marathon, half, 10k, 5k) pick the block length, taper and
long-run cap from the active race's distance, so one generator serves every
race. The mileage ramp comes from scenario.build_ramp (cutbacks + taper + the
long-run growth guardrail), so plan and projection agree by construction.
Output is the marathon_plan.json schema with `days` and `quality` filled, at
config.plan_path(race): data/<race_id>.generated.json.
"""

import argparse
import json
import math
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import config
import plan_layout
import scenario
import units
from models import PlannedWeek, TrainingPlan

OUT_PATH = config.DATA_DIR / "marathon_plan.generated.json"   # legacy name; see write_plan

_PHASE_META = {
    "base":  {"name": "Base Building", "color": "#A9BCE6"},
    "build": {"name": "Build Block",   "color": "#2F56B3"},
    "peak":  {"name": "Peak",          "color": "#002FA7"},
    "taper": {"name": "Taper + Race",  "color": "#6E8BD0"},
}

# Chosen by nearest race distance. long_run_cap_mi caps the longest run;
# key kinds come from plan_layout.quality_kind.
_DISTANCE_PRESETS = {
    "marathon": {"distance_mi": 26.2, "block_weeks": 16, "taper_weeks": 3, "long_run_cap_mi": 22.0,
                 "long_run_frac": 0.42},
    "half":     {"distance_mi": 13.1, "block_weeks": 12, "taper_weeks": 2, "long_run_cap_mi": 14.0,
                 "long_run_frac": 0.38},
    "10k":      {"distance_mi": 6.2,  "block_weeks": 8,  "taper_weeks": 1, "long_run_cap_mi": 10.0,
                 "long_run_frac": 0.32},
    "5k":       {"distance_mi": 3.1,  "block_weeks": 8,  "taper_weeks": 1, "long_run_cap_mi": 8.0,
                 "long_run_frac": 0.30},
}
MIN_WEEKS = 4


def preset_for(distance_mi: float) -> tuple:
    name = min(_DISTANCE_PRESETS, key=lambda k: abs(_DISTANCE_PRESETS[k]["distance_mi"] - distance_mi))
    return name, dict(_DISTANCE_PRESETS[name])


# ---------------------------------------------------------------------------
# Pace helpers
# ---------------------------------------------------------------------------

def _fmt_pace(p: float) -> str:
    return units.fmt_pace(p, label=False)


def _paces_block(race: dict) -> dict:
    from models import PaceZones
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


def _phase_note(phase: str) -> str:
    if phase == "base":
        return "Aerobic base. All easy. Consistency over heroics."
    if phase == "build":
        return "Race-specific work begins. Absorb the cutback weeks."
    if phase == "peak":
        return "Biggest weeks of the block. The key session is the week."
    return "Taper. Cut volume, keep a little intensity. Sleep and hydrate."


def _quality_block(kind: str, phase: str, target_mi: float, long_mi: float, distance_mi: float) -> dict:
    """Structured quality for the week: what the layout and the review read."""
    if kind == "none":
        return {"kind": "none"}
    if kind == "strides":
        return {"kind": "strides", "reps": 4, "rep_m": 100}
    key_mi = max(plan_layout.KEY_MIN_MI, min(plan_layout.KEY_MAX_MI,
                                              plan_layout.KEY_FRAC * max(0.0, target_mi - long_mi)))
    frac = 0.65 if phase == "peak" else 0.55
    if kind == "mp":
        # marathon-pace block inside the midweek key session; the long run
        # carries its own MP miles in peak weeks (see key_workout prose)
        return {"kind": "mp", "miles": round(key_mi * frac, 1), "pace": "marathon_pace"}
    if kind == "tempo":
        return {"kind": "tempo", "miles": round(key_mi * frac, 1), "pace": "tempo"}
    if kind == "threshold":
        return {"kind": "threshold", "miles": round(key_mi * 0.5, 1), "pace": "threshold"}
    if kind == "intervals":
        rep_m = 400 if distance_mi < 5 else 800
        reps = max(4, min(10, int(round(key_mi * 0.5 * 1609.34 / rep_m))))
        return {"kind": "intervals", "reps": reps, "rep_m": rep_m, "pace": "vo2max"}
    return {"kind": kind}


def _key_workout_prose(q: dict, phase: str, long_mi: float, distance_mi: float) -> str:
    """Prescription in the first sentence, rationale after."""
    kind = q.get("kind", "none")
    if kind == "none":
        return "All easy this week. No quality: the easy miles are the work."
    if kind == "strides":
        return "Easy week with 4-6 strides on one easy run. No quality yet."
    if kind == "mp":
        if phase == "peak" and long_mi >= 14:
            lr_mp = round(min(long_mi * 0.5, 12), 1)
            return (f"Long run with {units.fmt_dist(mi=lr_mp)} at marathon pace inside it; "
                    f"midweek {units.fmt_dist(mi=q['miles'])} at marathon pace. "
                    "The marathon-pace long run is the key session of the block.")
        return (f"{units.fmt_dist(mi=q['miles'])} at marathon pace inside the midweek run. "
                "Hold the HR band, let the pace be what it is.")
    if kind == "tempo":
        return (f"{units.fmt_dist(mi=q['miles'])} at tempo inside the midweek run. "
                "Comfortably hard: you could say a sentence, not hold a conversation.")
    if kind == "threshold":
        return f"{units.fmt_dist(mi=q['miles'])} at threshold, split in two if needed."
    if kind == "intervals":
        return (f"{q['reps']} x {q['rep_m']}m at 5k effort with jog recoveries. "
                "Even splits beat a fast first rep.")
    return ""


# ---------------------------------------------------------------------------
# Plan dict (marathon_plan.json schema)
# ---------------------------------------------------------------------------

def _race_week_monday(race_date: date) -> date:
    return race_date - timedelta(days=race_date.weekday())


def default_weeks(race_date: date, preset: dict, today: Optional[date] = None) -> int:
    """Block length: the preset, shortened to the weeks left until race week."""
    today = today or date.today()
    weeks_left = (_race_week_monday(race_date) - (today - timedelta(days=today.weekday()))).days // 7 + 1
    return max(MIN_WEEKS, min(int(preset["block_weeks"]), weeks_left))


def _recent_longest_mi(today: date, days: int = 28) -> float:
    """The longest real run in the last `days` days (0 when none)."""
    try:
        from enrichment import is_real_run
        from metrics import load_activities
        since = today - timedelta(days=days)
        best = 0.0
        for a in load_activities(activity_type="Run"):
            if not is_real_run(a):
                continue
            try:
                d = date.fromisoformat(str(a.get("start_date_local") or a.get("start_date"))[:10])
            except (TypeError, ValueError):
                continue
            if since < d <= today:
                best = max(best, (a.get("distance") or 0) / 1609.34)
        return round(best, 1)
    except Exception:
        return 0.0


def entry_from_data(today: Optional[date] = None) -> dict:
    """Entry mileage from the last 4 weeks of real runs: rounded UP to the
    nearest 5, floor 10, with the recent longest run alongside."""
    today = today or date.today()
    inp = scenario.derive_inputs(today)
    run_mpw = float(inp.get("run_mpw") or 0)
    entry = max(10.0, math.ceil(run_mpw / 5.0) * 5.0) if run_mpw > 0 else 10.0
    return {"entry_mi": entry, "run_mpw": run_mpw, "activities_loaded": inp.get("activities_loaded", 0),
            "long_run_recent_mi": _recent_longest_mi(today)}


def generate_plan_dict(entry_mi: float, weeks: int = None, today: date = None,
                       prefs: Optional[dict] = None, long_run_entry_mi: Optional[float] = None) -> dict:
    today = today or date.today()
    race = config.active_race(today)
    race_date = date.fromisoformat(race["date"])
    if race_date < today:
        raise ValueError(f"{race['name']} was on {race['date']}; add your next race to config.json first")
    distance_mi = float(race.get("distance_mi") or 26.2)
    preset_name, preset = preset_for(distance_mi)
    layout = plan_layout.resolve_layout(prefs)

    c = scenario.cfg()
    c["block_weeks"] = int(weeks or layout.get("block_weeks") or default_weeks(race_date, preset, today))
    # The taper keeps its length as the block shrinks (a marathon five weeks out
    # still tapers for three), and the build is whatever is left.
    c["taper_weeks"] = int(layout.get("taper_weeks")
                           or min(preset["taper_weeks"], max(1, c["block_weeks"] - 2)))
    build_weeks = max(1, c["block_weeks"] - c["taper_weeks"])
    full_build = max(1, int(preset["block_weeks"]) - int(preset["taper_weeks"]))
    build_frac = min(1.0, build_weeks / full_build)
    # A short build cannot carry the full peak: scale the multiplier by the share
    # of the preset's build weeks actually available (two of thirteen -> ~1.14x).
    if layout.get("peak_multiplier"):
        c["peak_multiplier"] = float(layout["peak_multiplier"])
    else:
        c["peak_multiplier"] = 1.0 + (float(c["peak_multiplier"]) - 1.0) * build_frac
    long_entry = float(long_run_entry_mi or 0) or max(4.0, entry_mi * 0.30)
    cap = float(layout.get("max_long_run_mi") or preset["long_run_cap_mi"])
    if not layout.get("max_long_run_mi"):
        # The long run only grows as far as the available build allows.
        cap = min(cap, max(long_entry, long_entry + (cap - long_entry) * build_frac))
    c["long_run_cap_mi"] = cap
    c["long_run_frac"] = preset["long_run_frac"]
    c["long_run_entry_mi"] = long_entry
    weeks = c["block_weeks"]
    mp_pace = float(race["goal_pace_min_per_mi"])

    ramp = scenario.build_ramp(entry_mi, c)
    week1_monday = _race_week_monday(race_date) - timedelta(weeks=weeks - 1)

    weeks_out = []
    for i, w in enumerate(ramp["weeks"]):
        start = week1_monday + timedelta(weeks=i)
        phase = w["phase"]
        is_race_week = start <= race_date < start + timedelta(days=7)
        kind = plan_layout.quality_kind(phase, layout["quality"], distance_mi, is_race_week)
        q = _quality_block(kind, phase, w["target_miles"], w["long_run_target"], distance_mi)
        week = {
            "week_num": w["week_num"],
            "start_date": start.isoformat(),
            "phase": phase,
            "target_miles": w["target_miles"],
            "long_run_target": 0.0 if is_race_week else w["long_run_target"],
            "target_tss": int(round(w["target_miles"] * 8)),
            "key_workout": _key_workout_prose(q, phase, w["long_run_target"], distance_mi),
            "quality": q,
            "notes": ("RACE WEEK. Trust the training. Sleep. Execute." if is_race_week
                      else _phase_note(phase)),
        }
        if layout.get("long_run_time_cap_min"):
            week["long_run_time_cap_min"] = int(layout["long_run_time_cap_min"])
        week["days"] = plan_layout.distribute_week(week, layout, distance_mi, race_date)
        weeks_out.append(week)

    phases = []
    for w in weeks_out:
        if phases and phases[-1]["id"] == w["phase"]:
            phases[-1]["end_week"] = w["week_num"]
        else:
            meta = _PHASE_META.get(w["phase"], {"name": w["phase"].title(), "color": "#6E8BD0"})
            phases.append({"id": w["phase"], "name": meta["name"], "color": meta["color"],
                           "start_week": w["week_num"], "end_week": w["week_num"]})

    decision_points = _decision_points(weeks_out, ramp, race, layout, distance_mi)

    return {
        "_meta": {
            "schema_version": 2,
            "generated": True,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "entry_mi": entry_mi,
            "peak_mi": ramp["peak_mi"],
            "avg_mi": ramp["avg_mi"],
            "long_run_peak": ramp["long_run_peak"],
            "distance_preset": preset_name,
            "inputs": {
                "entry_mi": entry_mi, "weeks": weeks, "units": units.unit(),
                "days_per_week": layout["days_per_week"], "long_run_day": layout["long_run_day"],
                "quality_day": layout["quality_day"], "rest_days": layout["rest_days"],
                "lift_days": layout["lift_days"], "quality": layout["quality"],
                "max_long_run_mi": c["long_run_cap_mi"],
                "long_run_entry_mi": round(long_entry, 1),
                "build_weeks": build_weeks, "taper_weeks": c["taper_weeks"],
                "long_run_time_cap_min": layout.get("long_run_time_cap_min"),
            },
            "compliance_thresholds": dict(__import__("marathon_plan").COMPLIANCE_DEFAULTS),
            "description": (f"Generated {weeks}-week {preset_name} plan from "
                            f"{units.fmt_dist(mi=entry_mi, label=False)} {units.volume_label()}, "
                            f"{layout['days_per_week']} days/week, long run {layout['long_run_day'].title()}, "
                            f"quality: {layout['quality']}. Peak {units.fmt_dist(mi=ramp['peak_mi'], label=False)} "
                            f"{units.volume_label()}, longest run {units.fmt_dist(mi=ramp['long_run_peak'])}."),
        },
        "race": {
            "id": race["id"], "name": race["name"], "date": race["date"],
            "distance_mi": distance_mi,
            "goal_time": race.get("goal_time", ""),
            "goal_pace_min_per_mi": mp_pace,
            "vdot_required": race.get("vdot_required", 0),
        },
        "paces": _paces_block(race),
        "phases": phases,
        "weeks": weeks_out,
        "decision_points": decision_points,
    }


def _decision_points(weeks_out: list, ramp: dict, race: dict, layout: dict, distance_mi: float) -> list:
    """Checkpoints at the end of base, build and peak: live reads, printed by
    the weekly check-in as on track / at risk with the plan's own adjustment."""
    def date_after(week_num):
        w = next((x for x in weeks_out if x["week_num"] == week_num), None)
        if not w:
            return ""
        return (date.fromisoformat(w["start_date"]) + timedelta(days=6)).isoformat()

    nums = [w["week_num"] for w in weeks_out]
    last_base = max((w["week_num"] for w in weeks_out if w["phase"] == "base"), default=nums[0])
    last_build = max((w["week_num"] for w in weeks_out if w["phase"] == "build"), default=last_base)
    last_peak = max((w["week_num"] for w in weeks_out if w["phase"] == "peak"), default=last_build)
    peak = ramp["peak_mi"]
    lr_peak = ramp["long_run_peak"]
    min_runs = max(2, layout["days_per_week"] - 1)
    unit = units.unit_label()
    out = [
        {
            "id": "end_of_base", "after_week": last_base, "evaluate_date": date_after(last_base),
            "name": "End of Base", "description": "Ready to add race-specific quality?",
            "criteria": [
                {"metric": "longest_run_30d_mi", "op": ">=", "value": round(lr_peak * 0.55, 0),
                 "label": f"Long run >= {units.fmt_dist(mi=round(lr_peak * 0.55))}"},
                {"metric": "weeks_with_min_runs_of_4", "op": ">=", "value": 3, "min_runs": min_runs,
                 "label": f"3+ of the last 4 weeks with {min_runs}+ runs"},
            ],
            "downgrade_action": "Hold base 1-2 more weeks; trim build targets 10%.",
        },
        {
            "id": "end_of_build", "after_week": last_build, "evaluate_date": date_after(last_build),
            "name": "End of Build", "description": "Is the goal still realistic?",
            "criteria": [
                {"metric": "longest_run_30d_mi", "op": ">=", "value": round(lr_peak * 0.85, 0),
                 "label": f"Long run >= {units.fmt_dist(mi=round(lr_peak * 0.85))}"},
                {"metric": "weekly_mi_4wk_avg", "op": ">=", "value": round(peak * 0.8, 0),
                 "label": f"{units.fmt_dist(mi=round(peak * 0.8), label=False)}+ {units.volume_label()} "
                          f"average over the last 4 weeks"},
            ],
            "downgrade_action": "Soften the goal; cap race-pace work; reduce peak mileage 10-15%.",
        },
        {
            "id": "end_of_peak", "after_week": last_peak, "evaluate_date": date_after(last_peak),
            "name": "End of Peak", "description": "Race-day execution check",
            "criteria": [
                {"metric": "longest_run_30d_mi", "op": ">=", "value": round(lr_peak, 0),
                 "label": f"Peak long run {units.fmt_dist(mi=round(lr_peak))} completed"},
            ],
            "downgrade_action": "Race conservatively; start at the slow end of the projected range.",
        },
    ]
    if distance_mi >= 20 and layout["quality"] in ("tempo", "full"):
        mp_mi = max((w.get("quality", {}).get("miles") or 0 for w in weeks_out
                     if w.get("quality", {}).get("kind") == "mp"), default=0)
        if mp_mi:
            out[2]["criteria"].append(
                {"metric": "mp_segment_completed_mi", "op": ">=", "value": mp_mi, "optional": True,
                 "label": f"{units.fmt_dist(mi=mp_mi)} at marathon pace inside one run (lap-verified)"})
    del unit
    return out


# ---------------------------------------------------------------------------
# Validation + write
# ---------------------------------------------------------------------------

def write_plan(entry_mi: float, weeks: int = None, path: Path = None,
               prefs: Optional[dict] = None, today: date = None,
               long_run_entry_mi: Optional[float] = None) -> Path:
    """Generate, validate and write the plan for the active race. Returns the path."""
    import marathon_plan
    plan = generate_plan_dict(entry_mi, weeks, today=today, prefs=prefs,
                              long_run_entry_mi=long_run_entry_mi)
    marathon_plan._validate(plan)
    if path is None:
        path = config.plan_path(config.active_race(today))
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plan, indent=2), encoding="utf-8")
    marathon_plan.load_plan.cache_clear()
    return path


def generate_from_data(prefs: Optional[dict] = None, weeks: int = None, today: date = None) -> tuple:
    """Entry mileage from the cache, then write_plan. -> (path, entry_info)."""
    info = entry_from_data(today)
    return write_plan(info["entry_mi"], weeks, prefs=prefs, today=today,
                      long_run_entry_mi=info.get("long_run_recent_mi") or None), info


# ---------------------------------------------------------------------------
# TrainingPlan (for the ical calendar feed)
# ---------------------------------------------------------------------------

def to_training_plan(plan: dict) -> TrainingPlan:
    race = plan["race"]
    tp = TrainingPlan(
        race_name=race["name"],
        race_date=date.fromisoformat(race["date"]),
        race_distance_mi=race["distance_mi"],
        goal_time=race.get("goal_time", ""),
        goal_pace=_fmt_pace(race["goal_pace_min_per_mi"]),
    )
    for w in plan["weeks"]:
        start = date.fromisoformat(w["start_date"])
        days = plan_layout.sessions_for_week(w, plan)
        workouts = []
        for d in days:
            workouts.extend(plan_layout.to_planned_workouts(d, plan))
        tp.weeks.append(PlannedWeek(
            week_num=w["week_num"], week_start=start, phase=w["phase"],
            target_miles=w["target_miles"], long_run_target_mi=w["long_run_target"],
            key_workout=w.get("key_workout") or "", lift_sessions=sum(1 for d in days if d.get("strength")),
            lift_notes="", workouts=workouts, notes=w.get("notes", ""),
        ))
    return tp


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args(argv):
    p = argparse.ArgumentParser(prog="coach.py plan", description=__doc__.split("\n\n")[1])
    g = p.add_mutually_exclusive_group()
    g.add_argument("--entry", help="current weekly volume (e.g. 25, 40km); default: --from-data")
    g.add_argument("--from-data", action="store_true", help="entry volume from the last 4 weeks of runs")
    p.add_argument("--weeks", type=int, help="block length (default: the distance preset, cut to the runway)")
    p.add_argument("--days", type=int, help="run days per week, 3-7")
    p.add_argument("--long-day", help="long-run day (mon..sun)")
    p.add_argument("--quality-day", help="key-session day (mon..sun)")
    p.add_argument("--quality", choices=plan_layout.QUALITY_LEVELS,
                   help="none | strides | tempo | full (none/strides for injury or return-to-run)")
    p.add_argument("--no-quality", action="store_true", help="same as --quality none")
    p.add_argument("--max-long", help="cap the longest run (e.g. 18, 30km)")
    p.add_argument("--lift-days", help="comma-separated lift days")
    p.add_argument("--rest-days", help="comma-separated rest days")
    p.add_argument("--ics", action="store_true", help="also write the calendar feed")
    p.add_argument("--json", action="store_true", help="print the plan JSON instead of the summary")
    return p.parse_args(argv)


def _prefs_from_args(a) -> dict:
    prefs = {}
    if a.days:
        prefs["days_per_week"] = a.days
    if a.long_day:
        prefs["long_run_day"] = a.long_day
    if a.quality_day:
        prefs["quality_day"] = a.quality_day
    if a.no_quality:
        prefs["quality"] = "none"
    elif a.quality:
        prefs["quality"] = a.quality
    if a.max_long:
        prefs["max_long_run_mi"] = units.parse_dist(a.max_long)
    if a.lift_days is not None:
        prefs["lift_days"] = [x.strip() for x in a.lift_days.split(",") if x.strip()]
    if a.rest_days is not None:
        prefs["rest_days"] = [x.strip() for x in a.rest_days.split(",") if x.strip()]
    return prefs


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    a = _parse_args(argv)
    prefs = _prefs_from_args(a)
    try:
        if a.entry:
            entry = units.parse_dist(a.entry)
            info = None
            path = write_plan(entry, a.weeks, prefs=prefs)
        else:
            if not config.ACTIVITIES_DIR.exists() or not any(config.ACTIVITIES_DIR.glob("*.json")):
                if not config.CSV_PATH.exists():
                    print("No training data to read an entry volume from. Pass --entry <volume> "
                          "(e.g. --entry 25) or ingest your Strava history first.")
                    return 1
            path, info = generate_from_data(prefs, a.weeks)
            entry = info["entry_mi"]
    except ValueError as e:
        print(f"  [plan] {e}")
        return 1
    plan = json.loads(path.read_text(encoding="utf-8"))
    if a.json:
        print(json.dumps(plan, indent=2))
    else:
        m = plan["_meta"]
        print(f"Generated {len(plan['weeks'])}-week {m['distance_preset']} plan -> {path}")
        if info:
            print(f"  Entry {units.fmt_dist(mi=entry, label=False)} {units.volume_label()} "
                  f"(last 4 weeks averaged {units.fmt_dist(mi=info['run_mpw'], label=False)}, rounded up)")
        else:
            print(f"  Entry {units.fmt_dist(mi=entry, label=False)} {units.volume_label()}")
        print(f"  Peak {units.fmt_dist(mi=m['peak_mi'], label=False)} {units.volume_label()} "
              f"(avg {units.fmt_dist(mi=m['avg_mi'], label=False)}), "
              f"longest run {units.fmt_dist(mi=m['long_run_peak'])}")
        inp = m["inputs"]
        print(f"  {inp['days_per_week']} days/week, long run {inp['long_run_day'].title()}, "
              f"key session {inp['quality_day'].title()}, quality: {inp['quality']}")
        print(f"  Race: {plan['race']['name']} on {plan['race']['date']}")
        print(f"  Phases: {', '.join(p['name'] for p in plan['phases'])}")
    if a.ics:
        from ical_generator import write_plan_ics
        out = write_plan_ics(to_training_plan(plan))
        print(f"  Calendar feed: {out} (subscribe once; regenerate to update).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
