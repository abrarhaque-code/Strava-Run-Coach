"""Short-race (half marathon / 10K) training plan generator.

Builds a config-driven, N-week finishing plan for whatever short goal race is
configured (the race whose `plan` field is "generated_half" / "generated_short").
All race identity, dates, paces, and HR caps come from config.json, and the week
dates are computed backward from the race date, so this works for any runner and
any short race.

For long structured plans (marathons) the source of truth is
data/marathon_plan.json, loaded by marathon_plan.py. This module covers the
procedurally generated short-race plan.
"""

import os
from datetime import date, timedelta
from typing import Optional

import config
from models import (
    PlannedWorkout,
    PlannedWeek,
    TrainingPlan,
    PaceZones,
)
from analysis import fmt_pace


# ---------------------------------------------------------------------------
# Race-aware switching (delegates to config; kept for backward compatibility)
# ---------------------------------------------------------------------------

def active_race(today: Optional[date] = None) -> str:
    """Return the id of the race active for `today` (delegates to config)."""
    return config.active_race_id(today)


def active_race_info(today: Optional[date] = None) -> dict:
    """Return the full race dict active for `today` (delegates to config)."""
    return config.active_race(today)


# ---------------------------------------------------------------------------
# Pace string helpers
# ---------------------------------------------------------------------------

def _range_str(ceiling: float, floor: float) -> str:
    """Format a pace range like '9:00-9:10' (ceiling is the faster end)."""
    return f"{fmt_pace(ceiling)}-{fmt_pace(floor)}"


# ---------------------------------------------------------------------------
# Plan generation
# ---------------------------------------------------------------------------

# 5-week template. Each entry: (phase, long-run fraction of race distance,
# key-workout builder). Long fractions taper into race week.
_WEEK_TEMPLATE = [
    ("build", 0.60),
    ("build", 0.72),
    ("peak", 0.82),
    ("sharpen", 0.58),
    ("taper", 1.00),  # race week; "long" is the race itself
]


def _race_week_monday(race_date: date) -> date:
    """Monday of the week containing race day."""
    return race_date - timedelta(days=race_date.weekday())


def _regular_week_workouts(week_start: date, phase: str, long_mi: float,
                           zones: PaceZones, race: dict, week_num: int) -> list:
    """Build 7 workouts (Mon-Sun) for a non-race week."""
    easy = _range_str(zones.easy_ceiling, zones.easy_floor)
    racep = _range_str(zones.race_pace_ceiling, zones.race_pace_floor)
    longp = f"{fmt_pace(zones.long_run_pace)}+"
    thresh = _range_str(zones.threshold_ceiling, zones.threshold_floor)

    easy_mi = 3.0 + min(week_num, 3) * 0.5  # gentle progression 3.5 -> 4.5

    # Key workout scales with phase
    if phase == "build":
        reps = 3 if week_num == 1 else 4
        key_desc = (f"1mi warmup, {reps}x1mi @ {racep} (race pace) w/ 2min jog, "
                    f"1mi cooldown")
        key_mi = 2.0 + reps
        key_pace = racep
    elif phase == "peak":
        key_desc = (f"{int(easy_mi)+3:.0f}mi: easy then last 3mi @ {racep} "
                    f"(race-pace simulation)")
        key_mi = easy_mi + 3
        key_pace = racep
    else:  # sharpen
        key_desc = f"1mi warmup, 5x800m @ {thresh} w/ 2:30 jog, 1mi cooldown"
        key_mi = 5.0
        key_pace = thresh

    return [
        PlannedWorkout(
            day=week_start, workout_type="rest",
            description="Rest or 20-30min easy walk.", distance_mi=0.0,
            notes="Recovery. Optional lift (upper body).",
        ),
        PlannedWorkout(
            day=week_start + timedelta(days=1), workout_type="key",
            description=f"KEY: {key_desc}", distance_mi=round(key_mi, 1),
            target_pace=key_pace, hr_cap=zones.race_hr_cap,
            notes="The one hard run of the week. Hit the paces, jog the recoveries truly easy.",
        ),
        PlannedWorkout(
            day=week_start + timedelta(days=2), workout_type="easy",
            description=f"{easy_mi:.1f}mi easy @ {easy}", distance_mi=round(easy_mi, 1),
            target_pace=easy, hr_cap=zones.easy_hr_cap,
            notes=f"Keep HR under {zones.easy_hr_cap}. Truly easy.",
        ),
        PlannedWorkout(
            day=week_start + timedelta(days=3), workout_type="easy",
            description=f"{easy_mi:.1f}mi easy @ {easy} (optional lift after)",
            distance_mi=round(easy_mi, 1), target_pace=easy, hr_cap=zones.easy_hr_cap,
            notes="Easy effort. Lift upper body if scheduled.",
        ),
        PlannedWorkout(
            day=week_start + timedelta(days=4), workout_type="rest",
            description="Rest. Stay off the legs before the long run.", distance_mi=0.0,
        ),
        PlannedWorkout(
            day=week_start + timedelta(days=5), workout_type="long",
            description=f"{long_mi:.0f}mi long run @ {longp}", distance_mi=round(long_mi, 1),
            target_pace=longp, hr_cap=zones.long_run_hr_cap,
            notes=(f"Long run of the week. Keep HR under {zones.long_run_hr_cap}; "
                   "walk briefly if it drifts. Practice race fueling on longer ones."),
        ),
        PlannedWorkout(
            day=week_start + timedelta(days=6), workout_type="rest",
            description="Rest or light recovery walk.", distance_mi=0.0,
        ),
    ]


def _race_week_workouts(week_start: date, zones: PaceZones, race: dict) -> list:
    """Build race-week workouts: light shakeouts, then RACE on race day."""
    easy = _range_str(zones.easy_ceiling, zones.easy_floor)
    race_date = date.fromisoformat(race["date"])
    goal_pace = race["goal_pace_min_per_mi"]
    goal_str = fmt_pace(goal_pace)
    dist = race["distance_mi"]
    workouts = []
    for i in range(7):
        d = week_start + timedelta(days=i)
        if d == race_date:
            workouts.append(PlannedWorkout(
                day=d, workout_type="key",
                description=(f"RACE DAY: {race['name']}. Goal {race['goal_time']}, "
                             f"even splits at {goal_str}/mi."),
                distance_mi=dist, target_pace=goal_str, hr_cap=zones.race_hr_cap,
                is_outdoor=True,
                notes=("Even-split strategy. Do not go out faster than goal pace in "
                       "the first few miles. Negative-split only if you feel strong late."),
            ))
        elif d < race_date and (race_date - d).days <= 3:
            workouts.append(PlannedWorkout(
                day=d, workout_type="shakeout",
                description=f"2-3mi shakeout @ {easy} + 4 strides",
                distance_mi=2.5, target_pace=easy, hr_cap=zones.easy_hr_cap,
                notes="Keep the legs sharp, not tired. Strides are short and relaxed.",
            ))
        elif d < race_date:
            workouts.append(PlannedWorkout(
                day=d, workout_type="easy",
                description=f"3mi easy @ {easy}", distance_mi=3.0,
                target_pace=easy, hr_cap=zones.easy_hr_cap,
                notes="Very easy taper run.",
            ))
        else:
            workouts.append(PlannedWorkout(
                day=d, workout_type="rest",
                description="Rest. Celebrate, then recover.", distance_mi=0.0,
            ))
    return workouts


def generate_half_plan(race: Optional[dict] = None, num_weeks: int = 5) -> TrainingPlan:
    """Generate a config-driven short-race plan ending on the race date.

    `race` defaults to the active race from config. Week dates are computed
    backward from the race date so the plan always lands on race day.
    """
    if race is None:
        race = config.active_race()
    zones = PaceZones.from_config()

    race_date = date.fromisoformat(race["date"])
    dist = race["distance_mi"]
    week5_monday = _race_week_monday(race_date)
    week_starts = [week5_monday - timedelta(weeks=(num_weeks - 1 - i))
                   for i in range(num_weeks)]

    plan = TrainingPlan(
        race_name=race["name"],
        race_date=race_date,
        race_distance_mi=dist,
        goal_time=race.get("goal_time", ""),
        goal_pace=fmt_pace(race["goal_pace_min_per_mi"]),
        travel_windows=[],
    )

    template = _WEEK_TEMPLATE[-num_weeks:] if num_weeks <= len(_WEEK_TEMPLATE) else _WEEK_TEMPLATE
    for i in range(num_weeks):
        phase, long_frac = template[i] if i < len(template) else ("build", 0.7)
        ws = week_starts[i]
        is_race_week = (i == num_weeks - 1)
        long_mi = dist if is_race_week else max(4.0, round(dist * long_frac))

        if is_race_week:
            workouts = _race_week_workouts(ws, zones, race)
            key_workout = f"RACE: {race['name']}"
            phase_label = "taper"
        else:
            workouts = _regular_week_workouts(ws, phase, long_mi, zones, race, i + 1)
            key = next((w for w in workouts if w.workout_type == "key"), None)
            key_workout = key.description.replace("KEY: ", "") if key else ""
            phase_label = phase

        target = round(sum(w.distance_mi for w in workouts), 1)
        plan.weeks.append(PlannedWeek(
            week_num=i + 1,
            week_start=ws,
            phase=phase_label,
            target_miles=target,
            long_run_target_mi=long_mi,
            key_workout=key_workout,
            lift_sessions=1 if not is_race_week else 0,
            lift_notes="Upper body only on easy days." if not is_race_week else "No lifting race week.",
            workouts=workouts,
            notes=_week_note(phase_label, i + 1, num_weeks),
        ))

    return plan


def _week_note(phase: str, week_num: int, total: int) -> str:
    if phase == "taper":
        return "Race week. Reduce volume, keep a little intensity with strides. Sleep and hydrate."
    if phase == "peak":
        return "Biggest week. The race-pace simulation is the key session of the block."
    if phase == "sharpen":
        return "Sharpen with shorter, faster work. Volume comes down, intensity stays."
    return f"Build week {week_num} of {total}. Consistency over heroics."


# ---------------------------------------------------------------------------
# Adaptation heuristics (generic)
# ---------------------------------------------------------------------------

def adjust_for_missed_long_run(plan: TrainingPlan, actual_week: int) -> TrainingPlan:
    """Extend the next available long run if a week's long run was missed."""
    for w in plan.weeks:
        if w.week_num <= actual_week:
            continue
        for wk in w.workouts:
            if wk.workout_type == "long" and wk.distance_mi > 0:
                bump = min(wk.distance_mi + 1, plan.race_distance_mi)
                wk.distance_mi = bump
                w.long_run_target_mi = bump
                w.notes += f" (Adjusted: long run extended to {bump:.0f}mi to compensate for a missed long run.)"
                return plan
    return plan


def adjust_for_low_mileage(plan: TrainingPlan, actual_week: int) -> TrainingPlan:
    """Reduce next week's target 10% and convert a rest day to an easy run."""
    if actual_week < 1 or actual_week >= len(plan.weeks):
        return plan
    nxt = plan.weeks[actual_week]
    nxt.target_miles = round(nxt.target_miles * 0.9, 1)
    nxt.notes += f" (Adjusted: target reduced 10% to rebuild gently after a low week.)"
    for wk in nxt.workouts:
        if wk.workout_type == "rest" and wk.distance_mi == 0.0:
            wk.workout_type = "easy"
            wk.description = "2mi easy (added to rebuild volume gently)"
            wk.distance_mi = 2.0
            wk.notes = "Added easy run to rebuild after a low-mileage week."
            break
    return plan


def adjust_for_high_actr(plan: TrainingPlan, actr_value: float) -> TrainingPlan:
    """Annotate key workouts if ACTR is dangerously high, or add load if too low."""
    if actr_value > 1.5:
        for week in plan.weeks:
            for w in week.workouts:
                if w.workout_type == "key" and w.distance_mi > 0:
                    w.notes += (f" WARNING: ACTR {actr_value:.2f} (>1.5). "
                                "Consider easing this workout.")
        if plan.weeks:
            plan.weeks[0].notes += (f" ACTR WARNING: {actr_value:.2f} above safe range. "
                                    "Prioritize recovery.")
    elif actr_value < 0.5 and plan.weeks:
        for w in plan.weeks[0].workouts:
            if w.workout_type == "rest" and w.distance_mi == 0.0:
                w.notes += (f" ACTR {actr_value:.2f} (<0.5). Consider a 2mi easy run here "
                            "to rebuild training load.")
                break
    return plan


def adjust_for_travel(plan: TrainingPlan, week_num: int) -> TrainingPlan:
    """Cap a week at 8mi, convert key->easy and long->7mi for travel."""
    if week_num < 1 or week_num > len(plan.weeks):
        return plan
    week = plan.weeks[week_num - 1]
    week.phase = "travel"
    week.target_miles = min(week.target_miles, 8.0)
    week.key_workout = "None -- travel week"
    week.notes += " (Travel-adjusted: reduced to easy/rest, maintenance only.)"
    for w in week.workouts:
        if w.workout_type == "key":
            w.workout_type = "easy"
            w.description = f"Easy alternative: {w.distance_mi}mi easy if possible"
            w.notes = "Converted from key workout due to travel."
        elif w.workout_type == "long" and w.distance_mi > 7:
            w.distance_mi = 7.0
            w.description = "6-7mi easy (capped for travel week)"
    return plan


# ---------------------------------------------------------------------------
# Markdown output
# ---------------------------------------------------------------------------

_DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

_PHASE_LABELS = {
    "build": "Build",
    "peak": "Peak",
    "sharpen": "Sharpen",
    "taper": "Taper + Race",
    "travel": "Travel (Reduced)",
}


def generate_plan_markdown(plan: Optional[TrainingPlan] = None) -> str:
    """Render the training plan as a formatted markdown string."""
    if plan is None:
        plan = generate_half_plan()

    zones = PaceZones.from_config()
    lines = []
    lines.append(f"# {plan.race_name} Training Plan")
    lines.append("")
    lines.append(f"**Race date:** {plan.race_date.strftime('%A, %B %d, %Y')}")
    lines.append(f"**Distance:** {plan.race_distance_mi} miles")
    lines.append(f"**Goal time:** {plan.goal_time}")
    lines.append(f"**Goal pace:** {plan.goal_pace}/mi")
    lines.append("")
    lines.append("**Race strategy:** Even splits at goal pace. Hold back early, "
                 "and only push the close if you feel strong.")
    lines.append("")

    # Pace zones (from config)
    lines.append("## Pace Zones")
    lines.append("")
    lines.append("| Zone | Pace | HR |")
    lines.append("|------|------|----|")
    lines.append(f"| Easy | {_range_str(zones.easy_ceiling, zones.easy_floor)}/mi | < {zones.easy_hr_cap} bpm |")
    lines.append(f"| Tempo | {_range_str(zones.tempo_ceiling, zones.tempo_floor)}/mi | {zones.tempo_hr_range[0]}-{zones.tempo_hr_range[1]} bpm |")
    lines.append(f"| Race Pace | {_range_str(zones.race_pace_ceiling, zones.race_pace_floor)}/mi | < {zones.race_hr_cap} bpm |")
    lines.append(f"| Threshold | {_range_str(zones.threshold_ceiling, zones.threshold_floor)}/mi | {zones.threshold_hr_range[0]}-{zones.threshold_hr_range[1]} bpm |")
    lines.append(f"| Long Run | {fmt_pace(zones.long_run_pace)}+/mi | < {zones.long_run_hr_cap} bpm |")
    lines.append("")
    lines.append("---")
    lines.append("")

    for week in plan.weeks:
        phase_label = _PHASE_LABELS.get(week.phase, week.phase.title())
        lines.append(
            f"## Week {week.week_num}: "
            f"{week.week_start.strftime('%b %d')} - "
            f"{(week.week_start + timedelta(days=6)).strftime('%b %d')} "
            f"| {phase_label} | {week.target_miles:.0f}mi target"
        )
        lines.append("")
        if week.notes:
            lines.append(f"> {week.notes}")
            lines.append("")
        lines.append(f"**Key workout:** {week.key_workout}")
        lines.append(f"**Long run target:** {week.long_run_target_mi:.1f}mi")
        lines.append("")
        for w in week.workouts:
            day_name = _DAY_NAMES[w.day.weekday()]
            date_str = w.day.strftime("%b %d")
            badge = w.workout_type.upper()
            line = f"- **{day_name} {date_str}** [{badge}]: {w.description}"
            details = []
            if w.target_pace:
                details.append(f"Pace: {w.target_pace}")
            if w.hr_cap:
                details.append(f"HR cap: {w.hr_cap}")
            if w.distance_mi > 0:
                details.append(f"{w.distance_mi:.1f}mi")
            if details:
                line += f"  \n  *{' | '.join(details)}*"
            if w.notes:
                line += f"  \n  {w.notes}"
            lines.append(line)
        lines.append("")
        lines.append("---")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    """Generate the active short-race plan and write it to plan_output/."""
    race = config.active_race()
    if config.has_structured_plan(race):
        print(f"Active race '{race['name']}' uses a structured JSON plan "
              "(data/marathon_plan.json). This generator is for short races.")
        print("Nothing to generate. Edit config.json -> active_race to a generated_half race.")
        return

    plan = generate_half_plan(race)
    md = generate_plan_markdown(plan)

    output_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "plan_output")
    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, f"{race['id']}.md")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(md)

    print(f"Plan written to {output_path}")
    print(f"  Weeks: {len(plan.weeks)}")
    print(f"  Total planned miles: {sum(w.target_miles for w in plan.weeks):.0f}")
    print(f"  Race: {plan.race_name} on {plan.race_date}")


if __name__ == "__main__":
    main()
