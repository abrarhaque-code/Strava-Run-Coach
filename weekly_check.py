"""Weekly check-in: last week against the plan, this week's sessions, how the
runs went, the next checkpoint, and where fitness sits.

    python3 coach.py week                     # today; prints and writes the report
    python3 coach.py week --date 2026-08-31   # a frozen date (tests, backfill)
    python3 coach.py week --no-write          # print only
    python3 coach.py week --no-reconcile      # actuals already reconciled this run

Reconciled actuals are persisted first (reconcile.reconcile is the one write
path), then the report is printed and written to plan_output/weekly/YYYY-Wnn.md.
Exit code 3 when reconcile raised (the report is still printed and written).
Without a plan the check-in still shows the last 7 days of runs and fitness.
"""

import sys
import textwrap
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import config
import marathon_plan as mp
import metrics
import plan_layout
import plan_tracker
import units

REPORT_DIR = config.PLAN_OUTPUT_DIR / "weekly"
RULE = "=" * 65
BAR = "-" * 65
RECENT_DAYS = 7
MI_M = 1609.34


def _fmt_pct(x: float) -> str:
    return f"{x * 100:.0f}%"


def _fmt_hm(minutes) -> str:
    total = int(round(float(minutes or 0)))
    return f"{total // 60}:{total % 60:02d}"


def _dist(mi) -> str:
    return units.fmt_dist(mi=float(mi or 0))


def _wrap(text: str, indent: str = "  ", width: int = 78) -> list:
    return textwrap.wrap(text, width=width, initial_indent=indent, subsequent_indent=indent + "  ")


def _phase_name(phase_id: str) -> str:
    try:
        ph = mp.phase_by_id(phase_id)
        return ph["name"] if ph else str(phase_id)
    except Exception:
        return str(phase_id)


def _race(today: date, has_plan: bool) -> dict:
    """Name, date and goal of the race the check-in counts down to."""
    if has_plan:
        try:
            r = dict(mp.race_info())
            if r.get("date"):
                return r
        except Exception:
            pass
    try:
        return dict(config.active_race(today))
    except Exception:
        return {"name": "no race set", "date": None, "goal_time": None}


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------

def _last_week_section(lines: list, week_num: int, today: date, runs: list, state: dict) -> Optional[dict]:
    c = plan_tracker.weekly_compliance(week_num, today=today, runs=runs)
    if "error" in c:
        return None

    lines.append(BAR)
    lines.append(f"  1. LAST WEEK (wk {c['week_num']}, {_phase_name(c['phase'])})")
    lines.append(BAR)
    lines.append("")

    # A week reconcile flagged data_stale had no activity data loaded for it:
    # the live numbers below would read zero and call a good week missed.
    rec = (state.get("weeks_actuals") or {}).get(str(week_num)) or {}
    if rec.get("data_stale"):
        lines.append(f"  [DATA STALE] No activity data covering wk {week_num} was loaded this pass.")
        lines.append("  Previous record shown; refresh your Strava data before trusting it.")
        lines.append(f"  Recorded:       {_dist(rec.get('run_mi'))} of {_dist(c['target_miles'])}, "
                     f"{rec.get('runs')} runs, long run {_dist(rec.get('long_run_mi'))}, "
                     f"status {str(rec.get('status', '?')).upper()}")
        lines.append("")
        return c

    lines.append(f"  Run volume:     {_dist(c['miles_actual'])} of {_dist(c['target_miles'])} "
                 f"({_fmt_pct(c['miles_pct'])} of target)")
    lines.append(f"  Runs:           {c['run_count']}")
    lines.append(f"  Long run:       {_dist(c['long_run_actual'])} of {_dist(c['long_run_target'])} "
                 f"({'hit' if c['long_run_hit'] else 'missed'})")
    if c.get("bike_sessions"):
        lines.append(f"  Cross-training: {c['bike_sessions']} session(s), {c['bike_min']:.0f} min "
                     "(credited as load, never as run volume)")
    lines.append(f"  Status:         {c['status'].upper()}")
    lines.append("")

    th = mp.compliance_thresholds()
    short = float(c["target_miles"]) - float(c["miles_actual"])
    if c["status"] == "complete":
        lines.append("  Verdict: executed. Stacking weeks like this is what builds the race.")
    elif c["status"] == "in_progress":
        lines.append("  Verdict: the week is still open; the numbers above are partial.")
    elif c["status"] == "upcoming":
        lines.append("  Verdict: not started yet.")
    elif c["miles_pct"] < th["missed_below_pct"]:
        lines += _wrap(f"Verdict: missed ({_fmt_pct(c['miles_pct'])} of target). Plan rule: repeat these "
                       "targets rather than advance. Two misses in a row slide the plan by a week. "
                       "We adjust and move forward.")
    elif c["miles_pct"] >= th["repeat_below_pct"] and not c["long_run_hit"]:
        lines += _wrap(f"Verdict: the volume was there but the long run was not ({_dist(c['long_run_actual'])} "
                       f"of {_dist(c['long_run_target'])}). The long run is the week's anchor; this week's "
                       "long run matters more than the midweek miles.")
    else:
        lines += _wrap(f"Verdict: short by {_dist(short)}. Under {th['repeat_below_pct']:.0%} the week "
                       "repeats rather than advances. Protect the long run and the key session first; "
                       "easy miles fill in around them.")
    lines.append("")
    return c


def _this_week_section(lines: list, week: dict, today: date, state: dict) -> None:
    lines.append(BAR)
    lines.append(f"  2. THIS WEEK (wk {week['week_num']} of {mp.total_weeks()}, {_phase_name(week['phase'])})")
    lines.append(BAR)
    lines.append("")
    head = f"  Target: {_dist(week['target_miles'])}  |  Long run: {_dist(week['long_run_target'])}"
    if week.get("long_run_time_cap_min"):
        head += f" (clock cap {_fmt_hm(week['long_run_time_cap_min'])})"
    lines.append(head)
    if week.get("key_workout"):
        lines += textwrap.wrap(f"Key workout: {week['key_workout']}", width=78,
                               initial_indent="  ", subsequent_indent="               ")
    if week.get("notes"):
        lines += textwrap.wrap(f"Notes: {week['notes']}", width=78,
                               initial_indent="  ", subsequent_indent="         ")

    standing = mp.standing_notes(today, state)
    sessions, paces = [], {}
    try:
        plan = mp.load_plan()
        paces = plan.get("paces") or {}
        sessions = plan_layout.apply_notes(plan_layout.sessions_for_week(week, plan), standing)
    except Exception:
        sessions = []
    if sessions:
        lines.append("")
        lines.append("  7-DAY LAYOUT:")
        for day in sessions:
            try:
                d = date.fromisoformat(day["date"])
                label = d.strftime("%a %b %d")
            except (KeyError, ValueError, TypeError):
                d, label = None, str(day.get("dow", "")).title()
            mark = ">" if d == today else " "
            lines.append(f"  {mark} {label}:  {plan_layout.format_session(day, paces)}")

    week_notes = [n for n in mp.notes_for_week(week["week_num"], state) if not n.get("until")]
    general = [n for n in mp.general_notes(state) if not n.get("until")]
    if standing:
        lines.append("")
        lines.append("  Standing notes (they shape the layout above):")
        for n in standing:
            lines.append(f"    - {n['text']} (until {n['until']})")
    if week_notes or general:
        lines.append("")
        lines.append("  Logged this week:")
        for n in week_notes:
            lines.append(f"    - {mp.format_note(n)}")
        for n in general:
            lines.append(f"    - (general) {mp.format_note(n)}")
    lines.append("")


def _driver(verdicts: list) -> Optional[str]:
    """The one verdict that decided the grade: the first miss, else the first watch."""
    import post_run_review as prr
    for want in ("miss", "watch"):
        for v in verdicts:
            if v["dim"] in prr.SCORED_DIMS and v["verdict"] == want:
                return f"{v['dim']}: {prr.first_sentence(v['line'])}"
    return None


def _run_lines(a: dict, today: date) -> list:
    """One run: date, distance, pace, HR, then its review grade and what drove it."""
    import post_run_review as prr
    d = plan_tracker._activity_date(a)
    dist_mi = (a.get("distance") or 0) / MI_M
    mt = (a.get("moving_time") or 0) / 60
    pace = mt / dist_mi if dist_mi > 0 else 0
    hr = a.get("average_heartrate")
    hr_txt = f"HR {hr:.0f}" if hr else "no HR"
    head = (f"  {d.strftime('%a %b %d') if d else '?':<10} {_dist(dist_mi):>9}  "
            f"{units.fmt_pace(pace):>10}  {hr_txt:<7}")
    try:
        week = mp.current_week(d) if (mp.has_plan() and d) else None
        r = prr.review(a, streams=metrics.load_streams(a.get("id")), plan_week=week, lookup_plan=False)
    except Exception:
        return [head + f"  {a.get('name') or ''}".rstrip()]
    sc = r["score"]
    out = [head + f"  {r['classification']:<15} {sc['grade']} ({sc['score']})"]
    drv = _driver(r["verdicts"])
    if drv:
        out += textwrap.wrap(drv, width=78, initial_indent="      ", subsequent_indent="        ")
    return out


def _runs_section(lines: list, today: date, runs: list, days: int = RECENT_DAYS) -> None:
    since = today - timedelta(days=days)
    recent = [a for a in runs
              if (d := plan_tracker._activity_date(a)) is not None and since < d <= today]
    if not recent:
        return
    recent.sort(key=lambda a: str(a.get("start_date_local") or a.get("start_date") or ""))
    lines.append(BAR)
    lines.append(f"  3. RUNS, LAST {days} DAYS")
    lines.append(BAR)
    lines.append("")
    for a in recent:
        lines += _run_lines(a, today)
    lines.append("")
    lines += _wrap("Grades score the easy cap, workout execution, the finish and the clock cap "
                   "(A 90+, B 75+, C 60+). `python3 coach.py review <id>` has the full debrief.")
    lines.append("")


def _checkpoint_section(lines: list, today: date) -> None:
    """The next checkpoint, or one that fell due in the last week, read live."""
    try:
        dps = [d for d in plan_tracker.evaluate_all_decision_points(today=today) if "error" not in d]
    except Exception:
        return
    if not dps:
        return

    def _days_until(iso: str) -> int:
        try:
            return (date.fromisoformat(iso) - today).days
        except (TypeError, ValueError):
            return 10 ** 6

    just = [d for d in dps if not d.get("is_future") and -7 <= _days_until(d["evaluate_date"]) <= 0]
    upcoming = [d for d in dps if d.get("is_future")]
    show = just + upcoming[:1]
    if not show:
        show = [dps[-1]]

    lines.append(BAR)
    lines.append("  4. CHECKPOINT")
    lines.append(BAR)
    for dp in show:
        n = _days_until(dp["evaluate_date"])
        when = f"in {n} days" if n > 0 else ("today" if n == 0 else f"{-n} days ago")
        if dp.get("is_future"):
            status = f"{dp['required_met']}/{dp['required_total']} criteria met so far"
        else:
            status = dp["status"].replace("_", " ").upper()
        label = "JUST PASSED" if dp in just else ("NEXT" if n >= 0 else "LAST")
        lines.append("")
        lines.append(f"  {label}: {dp['name']} ({dp['evaluate_date']}, {when})  ->  {status}")
        for c in dp["criteria_results"]:
            mark = "x" if c["met"] else " "
            opt = " (advisory)" if c.get("optional") else ""
            actual = c["actual"]
            if isinstance(actual, bool) or actual is None:
                actual_s = "n/a" if actual is None else str(actual)
            elif isinstance(actual, (int, float)):
                actual_s = f"{actual:.1f}"
            else:
                actual_s = str(actual)
            lines.append(f"    [{mark}] {c['label']}{opt}: {c['op']} {c['target']}, now {actual_s}")
        if dp["status"] in ("at_risk", "off_track") and dp.get("downgrade_action"):
            lines += _wrap(f"If it stays that way: {dp['downgrade_action']}")
        elif dp.get("is_future") and dp.get("downgrade_action"):
            lines += _wrap(f"If it is missed: {dp['downgrade_action']}", indent="    ")
    lines.append("")


def _fitness_section(lines: list) -> None:
    try:
        from fitness_tracker import current_status
        s = current_status()
    except Exception:
        return
    if "error" in s:
        return
    lines.append(BAR)
    lines.append("  5. FITNESS")
    lines.append(BAR)
    lines.append("")
    lines.append(f"  Fitness (CTL) {s['ctl']:.1f} | Fatigue (ATL) {s['atl']:.1f} | "
                 f"Form (TSB) {s['tsb']:+.1f} | {s['phase']}")
    if not s.get("history_sufficient", True):
        lines.append("")
        lines += _wrap(f"[!] Only {s['history_days']}d of history loaded: CTL has not warmed up. "
                       "Do not read fitness or fatigue off these numbers; load more history first.")
    lines.append(f"  Days since last run: {s['days_since_run']}")
    lines.append("")


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def build_weekly_report(today: Optional[date] = None, state: Optional[dict] = None) -> str:
    if today is None:
        today = date.today()
    has_plan = mp.has_plan()
    if state is None:
        try:
            state = mp.load_state() if has_plan else {}
        except Exception:
            state = {}
    try:
        runs = metrics.load_activities(activity_type="Run")
    except Exception:
        runs = []

    race = _race(today, has_plan)
    days_to_race = None
    try:
        days_to_race = (date.fromisoformat(str(race["date"])) - today).days
    except (KeyError, TypeError, ValueError):
        pass

    lines = [RULE, f"  WEEKLY CHECK-IN  |  {race.get('name') or 'no race set'}"]
    head = f"  {today.isoformat()}"
    if days_to_race is not None:
        head += f"  |  {days_to_race} days to race"
    if race.get("goal_time"):
        head += f"  |  Goal: {race['goal_time']}"
    lines += [head, RULE, ""]

    current = None
    if not has_plan:
        lines.append("  No training plan yet. Ask for one (\"build me a plan, I can run 4 days a week\")")
        lines.append("  or run: python3 coach.py plan --from-data")
        lines.append("")
    else:
        current = mp.current_week(today)
        if current is None:
            weeks = mp.all_weeks()
            start = date.fromisoformat(weeks[0]["start_date"]) if weeks else None
            if start and today < start:
                lines.append(f"  The plan starts {start.isoformat()} ({(start - today).days} days). "
                             "Until then: easy running, nothing heroic.")
            elif days_to_race is not None and days_to_race < 0:
                lines.append(f"  Race was {-days_to_race} days ago. Plan complete; build the next one "
                             "when the next goal is set.")
            else:
                lines.append("  Today is outside the plan window.")
            lines.append("")
        else:
            if current["week_num"] > 1:
                _last_week_section(lines, current["week_num"] - 1, today, runs, state)
            _this_week_section(lines, current, today, state)

    _runs_section(lines, today, runs)
    if current is not None:
        _checkpoint_section(lines, today)
    _fitness_section(lines)

    lines.append(RULE)
    if days_to_race is not None and 0 <= days_to_race <= 7:
        lines.append("  Race week. Trust the training. Sleep. Execute.")
    elif current is None:
        lines.append("  Consistency first; the plan gives the weeks a shape.")
    else:
        lines.append("  The race is won in the weeks, not on race day. Run the plan.")
    lines.append(RULE)
    return "\n".join(lines)


def report_path(today: Optional[date] = None) -> Path:
    """plan_output/weekly/<ISO year>-W<ISO week>.md for the given day."""
    if today is None:
        today = date.today()
    iso_year, iso_week, _ = today.isocalendar()
    return REPORT_DIR / f"{iso_year}-W{iso_week:02d}.md"


def write_report(text: str, today: Optional[date] = None) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = report_path(today)
    path.write_text(text + "\n", encoding="utf-8")
    return path


def main(argv: Optional[list] = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    args = sys.argv[1:] if argv is None else list(argv)
    today = None
    if "--date" in args:
        today = date.fromisoformat(args[args.index("--date") + 1])
    no_write = "--no-write" in args

    # Persist reconciled actuals first (the one write path), then report. A
    # failed reconcile still gets a report, but the exit code says so.
    rc = 0
    if "--no-reconcile" not in args:
        try:
            from reconcile import reconcile
            reconcile(today=today, verbose=False)
        except Exception as e:
            print(f"  [weekly_check] reconcile FAILED: {e}")
            rc = 3

    report = build_weekly_report(today=today)
    print(report)
    if not no_write:
        path = write_report(report, today=today)
        print(f"\n  Report written: {path}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
