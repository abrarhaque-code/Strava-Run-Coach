"""Weekly coaching check-in: compare actual training vs plan, generate coaching output."""

import sys
from datetime import date, datetime, timedelta
from typing import List, Optional

import config
from models import (
    RunActivity, StrengthSession, WeekSummary, PlannedWeek, PlannedWorkout,
    TrainingPlan, PaceZones,
)
from analysis import (
    load_activities, weekly_summaries, compute_actr, fmt_pace,
    race_pace_readiness, polarization_stats,
)

# ---------------------------------------------------------------------------
# 10-week plan (week 1 = Mon Apr 6 2026)
# Phases: build (wk 1-5), sharpen (wk 6-8), taper (wk 9-10)
# ---------------------------------------------------------------------------
PLAN_START = date(2026, 4, 6)  # Monday of week 1

PLAN_WEEKS: List[PlannedWeek] = [
    PlannedWeek(
        week_num=1, week_start=date(2026, 4, 6), phase="build",
        target_miles=14, long_run_target_mi=6,
        key_workout="4mi easy w/ 4x30s strides",
        lift_sessions=2,
        notes="Reestablish consistency. 4 runs minimum.",
    ),
    PlannedWeek(
        week_num=2, week_start=date(2026, 4, 13), phase="build",
        target_miles=17, long_run_target_mi=8,
        key_workout="5mi w/ 2mi at tempo (9:00-9:10)",
        lift_sessions=2,
        notes="First long run at 8mi. Keep long run HR < 148.",
    ),
    PlannedWeek(
        week_num=3, week_start=date(2026, 4, 20), phase="build",
        target_miles=20, long_run_target_mi=9,
        key_workout="6mi w/ 3mi at race pace (9:05)",
        lift_sessions=2,
        notes="Push weekly volume. 4 runs minimum.",
    ),
    PlannedWeek(
        week_num=4, week_start=date(2026, 4, 27), phase="build",
        target_miles=22, long_run_target_mi=10,
        key_workout="7mi w/ 4mi at race pace",
        lift_sessions=2,
        notes="First double-digit long run. Big week.",
    ),
    PlannedWeek(
        week_num=5, week_start=date(2026, 5, 4), phase="sharpen",
        target_miles=20, long_run_target_mi=11,
        key_workout="8mi w/ 5mi at race pace",
        lift_sessions=1,
        lift_notes="Reduce to 1 lift session. Lower body maintenance only.",
        notes="Peak long run. Prove you can hold 9:05 for 5mi.",
    ),
    PlannedWeek(
        week_num=6, week_start=date(2026, 5, 11), phase="taper",
        target_miles=14, long_run_target_mi=6,
        key_workout="4mi shakeout w/ 2mi at race pace",
        lift_sessions=0,
        lift_notes="No lifting race week.",
        notes="RACE WEEK. Taper hard. Sleep > everything.",
    ),
]


# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------

def get_current_week_num(race_date=None) -> int:
    """Which week of the plan we are in (1-indexed). Returns 0 if before plan start."""
    if race_date is None:
        race_date = date.fromisoformat(config.active_race()['date'])
    today = date.today()
    if today < PLAN_START:
        return 0
    days_in = (today - PLAN_START).days
    week_num = days_in // 7 + 1
    return week_num


def _get_current_week_start() -> date:
    """Monday of the current ISO week."""
    today = date.today()
    return today - timedelta(days=today.weekday())


def _get_plan_week(week_num: int) -> Optional[PlannedWeek]:
    """Get the PlannedWeek for a given week number, or None if out of range."""
    for pw in PLAN_WEEKS:
        if pw.week_num == week_num:
            return pw
    return None


def _summarize_week(runs: List[RunActivity], strength: List[StrengthSession],
                    week_start: date) -> WeekSummary:
    """Build a WeekSummary for one specific week."""
    week_end = week_start + timedelta(days=7)
    week_runs = [r for r in runs if week_start <= r.date.date() < week_end]
    week_str = [s for s in strength if week_start <= s.date.date() < week_end]

    if not week_runs:
        return WeekSummary(
            week_start=week_start,
            strength_sessions=len(week_str),
            runs=[],
        )

    total_mi = sum(r.distance_mi for r in week_runs)
    longest = max(r.distance_mi for r in week_runs)
    paces = [r.pace_min_per_mi for r in week_runs if r.pace_min_per_mi > 0]
    import statistics
    avg_pace = statistics.mean(paces) if paces else 0.0
    cap = config.easy_hr_cap()
    easy = sum(1 for r in week_runs if r.avg_hr and r.avg_hr < cap)
    hard = sum(1 for r in week_runs if r.avg_hr and r.avg_hr >= cap)
    total_re = sum(r.relative_effort or 0 for r in week_runs)

    return WeekSummary(
        week_start=week_start,
        total_miles=total_mi,
        run_count=len(week_runs),
        longest_run_mi=longest,
        avg_pace=avg_pace,
        total_relative_effort=total_re,
        strength_sessions=len(week_str),
        easy_run_count=easy,
        hard_run_count=hard,
        runs=week_runs,
    )


def check_milestones(runs: List[RunActivity]) -> dict:
    """Which race readiness milestones have been achieved."""
    milestones = {
        "8mi long run": False,
        "10mi long run": False,
        "race pace session (5mi+ at 9:00-9:10)": False,
        "11mi long run": False,
        "sub-9:10 for 8mi+": False,
    }
    for r in runs:
        if r.distance_mi >= 8:
            milestones["8mi long run"] = True
        if r.distance_mi >= 10:
            milestones["10mi long run"] = True
        if r.distance_mi >= 11:
            milestones["11mi long run"] = True
        if (r.distance_mi >= 5 and 8.9 <= r.pace_min_per_mi <= 9.3
                and r.avg_hr):
            milestones["race pace session (5mi+ at 9:00-9:10)"] = True
        if r.distance_mi >= 8 and r.pace_min_per_mi <= 9.17 and r.avg_hr:
            milestones["sub-9:10 for 8mi+"] = True
    return milestones


def adjust_next_week(actual: WeekSummary, planned: PlannedWeek,
                     actr: float) -> PlannedWeek:
    """Return an adjusted PlannedWeek based on what actually happened."""
    next_week_num = planned.week_num + 1
    next_planned = _get_plan_week(next_week_num)

    if next_planned is None:
        # Past end of plan or race week -- return a default taper week
        return PlannedWeek(
            week_num=next_week_num,
            week_start=planned.week_start + timedelta(days=7),
            phase="taper",
            target_miles=12,
            long_run_target_mi=5,
            key_workout="3mi shakeout + strides",
            lift_sessions=0,
            notes="Beyond plan. Keep easy, stay healthy.",
        )

    # Start from next week's plan and adjust
    adj_miles = next_planned.target_miles
    adj_long = next_planned.long_run_target_mi
    adj_key = next_planned.key_workout
    adj_notes = next_planned.notes
    adj_lifts = next_planned.lift_sessions
    adjustments = []

    # --- Heuristic 1: big miss -> gradual rebuild ---
    if planned.target_miles > 0 and actual.total_miles < 0.7 * planned.target_miles:
        rebuild_target = actual.total_miles * 1.15
        if rebuild_target < adj_miles:
            adj_miles = round(rebuild_target, 1)
            adjustments.append(
                f"Volume cut to {adj_miles:.0f}mi (gradual rebuild from {actual.total_miles:.1f}mi actual)"
            )

    # --- Heuristic 2: missed long run ---
    missed_long = actual.longest_run_mi < planned.long_run_target_mi * 0.7
    if missed_long:
        adj_long = min(planned.long_run_target_mi + 1, 12)
        adjustments.append(
            f"Long run set to {adj_long:.0f}mi (missed last week's {planned.long_run_target_mi:.0f}mi target)"
        )

    # --- Heuristic 3: ACTR too high -> reduce load ---
    if actr > 1.5:
        adj_miles = round(adj_miles * 0.8, 1)
        adj_key = f"Easy run only (ACTR {actr:.2f} -- injury risk, dropping tempo)"
        adj_lifts = min(adj_lifts, 1)
        adjustments.append(
            f"Reduced 20% due to high ACTR ({actr:.2f}). Tempo dropped. Long run kept."
        )

    # --- Heuristic 4: ACTR too low -> detraining warning ---
    if actr < 0.8:
        adjustments.append(
            f"ACTR is {actr:.2f} -- detraining territory. Every run matters. "
            f"Prioritize consistency over any single workout."
        )

    adj_notes_full = next_planned.notes
    if adjustments:
        adj_notes_full += " | ADJUSTMENTS: " + "; ".join(adjustments)

    return PlannedWeek(
        week_num=next_week_num,
        week_start=next_planned.week_start,
        phase=next_planned.phase,
        target_miles=adj_miles,
        long_run_target_mi=adj_long,
        key_workout=adj_key if actr <= 1.5 else adj_key,
        lift_sessions=adj_lifts,
        lift_notes=next_planned.lift_notes,
        notes=adj_notes_full,
    )


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

def weekly_report(runs: List[RunActivity], strength: List[StrengthSession],
                  plan_week: PlannedWeek) -> str:
    """Generate the full coaching report string."""
    race = config.active_race()
    race_name = race['name']
    race_date = date.fromisoformat(race['date'])
    goal_time = race['goal_time']
    goal_pace_min_mi = race['goal_pace_min_per_mi']

    lines = []
    week_start = plan_week.week_start
    actual = _summarize_week(runs, strength, week_start)

    # ACTR from last 4 weeks
    summaries = weekly_summaries(runs, strength, weeks_back=8)
    actr = compute_actr(summaries)

    days_to_race = (race_date - date.today()).days

    # ===== HEADER =====
    lines.append("=" * 65)
    lines.append(f"  WEEKLY COACHING CHECK-IN  |  {race_name}")
    lines.append(f"  Week {plan_week.week_num} ({plan_week.phase.upper()})  |  "
                 f"{week_start.strftime('%b %d')} - "
                 f"{(week_start + timedelta(days=6)).strftime('%b %d, %Y')}")
    lines.append(f"  Race: {race_date.strftime('%b %d, %Y')}  |  "
                 f"{days_to_race} days out  |  Goal: {goal_time}")
    lines.append("=" * 65)
    lines.append("")

    # ===== SECTION 1: LAST WEEK =====
    lines.append("-" * 65)
    lines.append("  1. LAST WEEK -- WHAT ACTUALLY HAPPENED")
    lines.append("-" * 65)
    lines.append("")

    compliance = actual.compliance
    compliance_str = f"{compliance * 100:.0f}%"

    lines.append(f"  Total miles:    {actual.total_miles:.1f} / {plan_week.target_miles:.0f} "
                 f"({compliance_str} of target)")
    lines.append(f"  Runs:           {actual.run_count}")
    lines.append(f"  Longest run:    {actual.longest_run_mi:.1f}mi "
                 f"(target: {plan_week.long_run_target_mi:.0f}mi)")

    if actual.avg_pace > 0:
        lines.append(f"  Avg pace:       {fmt_pace(actual.avg_pace)}/mi")

    # Polarization
    total_hr_runs = actual.easy_run_count + actual.hard_run_count
    if total_hr_runs > 0:
        easy_pct = actual.easy_run_count / total_hr_runs * 100
        lines.append(f"  Polarization:   {actual.easy_run_count} easy / "
                     f"{actual.hard_run_count} hard "
                     f"({easy_pct:.0f}% easy, target >= 75%)")
    else:
        lines.append("  Polarization:   No HR data available")

    # Strength
    lines.append(f"  Strength:       {actual.strength_sessions} sessions "
                 f"(target: {plan_week.lift_sessions})")
    lines.append("                  (Strava strength data is sparse -- "
                 "count only, no volume detail)")
    lines.append("")

    # Individual runs
    if actual.runs:
        lines.append("  Runs this week:")
        for r in sorted(actual.runs, key=lambda x: x.date):
            hr_str = f"HR {r.avg_hr:.0f}" if r.avg_hr else "no HR"
            zone_str = r.hr_zone if r.avg_hr else ""
            lines.append(f"    {r.date.strftime('%a %b %d')} | "
                         f"{r.distance_mi:.1f}mi | {r.pace_str()}/mi | "
                         f"{hr_str} {zone_str}")
        lines.append("")

    # ===== SECTION 2: ASSESSMENT =====
    lines.append("-" * 65)
    lines.append("  2. ASSESSMENT -- HONEST EVALUATION")
    lines.append("-" * 65)
    lines.append("")

    # ACTR
    if actr > 1.5:
        lines.append(f"  ACTR: {actr:.2f} -- DANGER ZONE. Injury risk is elevated.")
        lines.append("  You ramped too fast. Pull back this week or you won't make "
                     "it to the start line.")
    elif actr > 1.3:
        lines.append(f"  ACTR: {actr:.2f} -- Yellow flag. You're pushing the upper limit.")
        lines.append("  Manageable if this week is slightly easier.")
    elif actr >= 0.8:
        lines.append(f"  ACTR: {actr:.2f} -- In the sweet spot (0.8-1.3). Good.")
    elif actr > 0:
        lines.append(f"  ACTR: {actr:.2f} -- DETRAINING TERRITORY. You are losing fitness.")
        lines.append("  Every run you skip now costs you minutes on race day.")
    else:
        lines.append("  ACTR: Insufficient data (need 4 weeks of history).")

    lines.append("")

    # Volume assessment
    if compliance >= 0.95:
        lines.append(f"  Volume: You hit {actual.total_miles:.1f}mi against "
                     f"{plan_week.target_miles:.0f}mi target. On track.")
    elif compliance >= 0.7:
        gap = plan_week.target_miles - actual.total_miles
        lines.append(f"  Volume: {actual.total_miles:.1f}mi vs {plan_week.target_miles:.0f}mi "
                     f"target. Short by {gap:.1f}mi.")
        lines.append("  Not a disaster, but this cannot become a pattern.")
    elif compliance >= 0.4:
        lines.append(f"  Volume: {actual.total_miles:.1f}mi. Plan said "
                     f"{plan_week.target_miles:.0f}mi. {goal_time} is slipping.")
        lines.append("  You need to double your volume next week or this race "
                     "becomes a survival exercise, not a performance.")
    elif actual.total_miles > 0:
        lines.append(f"  Volume: {actual.total_miles:.1f}mi this week. "
                     f"The plan called for {plan_week.target_miles:.0f}. "
                     "That is not enough.")
        lines.append(f"  At this rate you are training for a DNF, not a {goal_time}.")
    else:
        lines.append("  Volume: ZERO miles this week. You did not run.")
        lines.append("  There is no plan that accounts for zero. Get out the door.")

    # Run count
    if actual.run_count < 3:
        lines.append(f"  Frequency: You only ran {actual.run_count} time(s) this week. "
                     "That is not enough.")
        lines.append("  Half marathon prep needs 3-4 runs per week minimum.")

    # Long run
    long_ratio = actual.longest_run_mi / plan_week.long_run_target_mi if plan_week.long_run_target_mi > 0 else 0
    if long_ratio < 0.7 and plan_week.long_run_target_mi > 0:
        lines.append(f"  Long run: {actual.longest_run_mi:.1f}mi vs "
                     f"{plan_week.long_run_target_mi:.0f}mi target. Missed.")
        lines.append("  The long run is non-negotiable in half marathon training.")
    elif long_ratio >= 0.9:
        lines.append(f"  Long run: {actual.longest_run_mi:.1f}mi. Hit the target.")

    # Overall goal-time assessment
    lines.append("")
    if compliance >= 0.9 and actual.run_count >= 3:
        lines.append(f"  {goal_time} status: ON TRACK. Keep executing.")
    elif compliance >= 0.7:
        lines.append(f"  {goal_time} status: POSSIBLE but you have no margin for error.")
    elif actual.total_miles > 0:
        lines.append(f"  {goal_time} status: AT RISK. The next two weeks will determine "
                     "if this is realistic.")
    else:
        lines.append(f"  {goal_time} status: UNLIKELY at current effort level.")

    lines.append("")

    # ===== SECTION 3: THIS WEEK =====
    lines.append("-" * 65)
    lines.append("  3. THIS WEEK -- WHAT TO DO")
    lines.append("-" * 65)
    lines.append("")

    next_week = adjust_next_week(actual, plan_week, actr)

    lines.append(f"  Week {next_week.week_num} ({next_week.phase.upper()})  |  "
                 f"Target: {next_week.target_miles:.0f}mi  |  "
                 f"Long run: {next_week.long_run_target_mi:.0f}mi")
    lines.append(f"  Key workout: {next_week.key_workout}")
    lines.append(f"  Lift sessions: {next_week.lift_sessions}")
    if next_week.lift_notes:
        lines.append(f"  Lift notes: {next_week.lift_notes}")
    lines.append("")

    if next_week.notes:
        lines.append(f"  Notes: {next_week.notes}")
        lines.append("")

    # Generate a 7-day layout
    ws = next_week.week_start
    remaining_easy = next_week.target_miles - next_week.long_run_target_mi
    # Distribute: key workout ~5mi, rest as easy runs
    key_mi = min(5.0, remaining_easy * 0.4)
    easy_pool = remaining_easy - key_mi
    easy_per = round(easy_pool / 2, 1) if easy_pool > 0 else 3.0

    day_plan = [
        (ws, "REST or 30min walk"),
        (ws + timedelta(days=1), f"{easy_per:.1f}mi easy @ 10:00-10:15, HR < 148"),
        (ws + timedelta(days=2), f"{next_week.key_workout}"),
        (ws + timedelta(days=3), "REST" + (f" + Lift" if next_week.lift_sessions >= 1 else "")),
        (ws + timedelta(days=4), f"{easy_per:.1f}mi easy @ 10:00-10:15, HR < 148"),
        (ws + timedelta(days=5), f"{next_week.long_run_target_mi:.0f}mi long run @ 10:15, HR < 145"),
        (ws + timedelta(days=6), "REST" + (f" + Lift" if next_week.lift_sessions >= 2 else "")),
    ]

    lines.append("  7-DAY PLAN:")
    for d, workout in day_plan:
        day_name = d.strftime("%a %b %d")
        lines.append(f"    {day_name}:  {workout}")
    lines.append("")

    # ===== SECTION 4: RACE COUNTDOWN =====
    lines.append("-" * 65)
    lines.append("  4. RACE COUNTDOWN")
    lines.append("-" * 65)
    lines.append("")

    lines.append(f"  {race_name}  |  {race_date.strftime('%b %d, %Y')}  |  "
                 f"{days_to_race} days to go")
    lines.append(f"  Goal: {goal_time}  |  Pace: ~{fmt_pace(goal_pace_min_mi)}/mi")
    lines.append("")

    milestones = check_milestones(runs)
    lines.append("  Milestones:")
    for name, hit in milestones.items():
        mark = "x" if hit else " "
        lines.append(f"    [{mark}] {name}")
    lines.append("")

    # Key remaining workouts
    remaining = [name for name, hit in milestones.items() if not hit]
    if remaining:
        lines.append("  Key remaining before race day:")
        for r in remaining:
            lines.append(f"    - {r}")
    else:
        lines.append("  All major milestones hit. Execute the taper and race smart.")
    lines.append("")

    # Closing
    lines.append("=" * 65)
    if days_to_race <= 7:
        lines.append("  RACE WEEK. Trust the training. Sleep. Hydrate. Execute.")
    elif days_to_race <= 14:
        lines.append("  Two weeks out. This is where discipline matters most.")
    elif compliance < 0.5 and actual.total_miles > 0:
        lines.append("  Stop planning. Start running. The clock does not care "
                     "about your intentions.")
    elif actual.total_miles == 0:
        lines.append("  You have the plan. Now do the work.")
    else:
        lines.append("  Stay consistent. The race is won in the weeks, "
                     "not on race day.")
    lines.append("=" * 65)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    sys.stdout.reconfigure(encoding='utf-8')

    # Load data
    runs, strength = load_activities()
    print(f"Loaded {len(runs)} runs, {len(strength)} strength sessions.\n")

    race = config.active_race()
    race_date = date.fromisoformat(race['date'])

    # Determine current week
    week_num = get_current_week_num()
    if week_num == 0:
        print(f"Plan has not started yet. Plan starts {PLAN_START.strftime('%b %d, %Y')}.")
        print(f"Race: {race_date.strftime('%b %d, %Y')} ({(race_date - date.today()).days} days out).")
        return

    plan_week = _get_plan_week(week_num)
    if plan_week is None:
        # Past the last defined plan week -- use the last one as reference
        plan_week = PLAN_WEEKS[-1]
        print(f"Week {week_num} is beyond the defined plan. "
              f"Using week {plan_week.week_num} as reference.\n")

    print(f"Current plan week: {week_num} ({plan_week.phase})\n")

    # Generate and print report
    report = weekly_report(runs, strength, plan_week)
    print(report)


if __name__ == "__main__":
    main()
