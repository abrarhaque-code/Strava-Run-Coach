"""Daily brief: today's session, how fresh you are, and the last three days.

    python3 coach.py brief            # print
    python3 coach.py brief --save     # also write plan_output/brief.md
    python3 daily_brief.py --date 2026-10-01

Today's session comes from the plan's 7-day layout (plan_layout) with any
standing note applied: "no speedwork until the calf settles" turns a key day
easy while the note stands. Fatigue is read off the last three days against
this week's targets and the HR caps in config, never against fixed numbers.
"""

import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import config
import enrichment
import marathon_plan as mp
import metrics
import plan_layout
import units

BRIEF_PATH = config.PLAN_OUTPUT_DIR / "brief.md"
RECENT_DAYS = 3
MI_M = 1609.34
HEAVY_BLOCK_FRACTION = 0.40      # of the week's target volume inside 3 days = heavy
LONG_RUN_FRACTION = 0.60         # of the week's long-run target = a long run happened
TRULY_EASY_MARGIN = 5            # bpm under the easy cap


def _run_date(a: dict) -> Optional[date]:
    try:
        return date.fromisoformat(str(a.get("start_date_local") or a.get("start_date"))[:10])
    except (TypeError, ValueError):
        return None


def _load_runs() -> list:
    try:
        return metrics.load_activities(activity_type="Run")
    except Exception:
        return []


def _last_days_load(runs: list, today: Optional[date] = None, days: int = RECENT_DAYS) -> dict:
    """What the last `days` days cost: runs, volume, average HR, longest run."""
    today = today or date.today()
    since = today - timedelta(days=days)
    recent = [a for a in runs if (d := _run_date(a)) is not None and since < d <= today]
    recent.sort(key=lambda a: str(a.get("start_date_local") or a.get("start_date") or ""), reverse=True)
    dists = [(a.get("distance") or 0) / MI_M for a in recent]
    hrs = [a["average_heartrate"] for a in recent if a.get("average_heartrate")]
    return {
        "recent": recent,
        "days": days,
        "total_mi": sum(dists),
        "total_min": sum((a.get("moving_time") or 0) / 60 for a in recent),
        "avg_hr": (sum(hrs) / len(hrs)) if hrs else 0,
        "longest_mi": max(dists, default=0.0),
    }


def _fatigue_status(load: dict, week: Optional[dict] = None) -> tuple:
    """(status, advice). Thresholds come from this week's targets and the HR caps."""
    miles = load["total_mi"]
    n = len(load["recent"])
    avg_hr = load["avg_hr"]
    days = load.get("days", RECENT_DAYS)
    target = float((week or {}).get("target_miles") or 0)
    heavy_mi = max(8.0, HEAVY_BLOCK_FRACTION * target) if target else 12.0
    long_tgt = float((week or {}).get("long_run_target") or 0)
    long_mi = (max(config.long_run_min_mi(), LONG_RUN_FRACTION * long_tgt) if long_tgt
               else config.long_run_min_mi())
    hard = config.hard_hr_floor()
    easy_cap = config.easy_hr_cap()

    if n == 0:
        return "FRESH", f"No runs in {days} days. Legs should be ready."
    if n >= 3 and miles >= heavy_mi:
        return "FATIGUED", (f"Heavy {days}-day block: {units.fmt_dist(mi=miles)} against a "
                            f"{units.fmt_dist(mi=target)} week. Today should be easy or rest."
                            if target else
                            f"Heavy {days}-day block ({units.fmt_dist(mi=miles)}). Today should be easy or rest.")
    if n >= 2 and avg_hr > hard:
        return "ACCUMULATING", (f"Two recent runs averaged above HR {hard:.0f}. Keep today honest: "
                                "easy means easy.")
    if load["longest_mi"] >= long_mi:
        return "RECOVERING", (f"A {units.fmt_dist(mi=load['longest_mi'])} run in the last {days} days. "
                              "Easy effort only.")
    if avg_hr and avg_hr < easy_cap - TRULY_EASY_MARGIN and miles < heavy_mi:
        return "READY", (f"Recent runs were truly easy (avg HR {avg_hr:.0f} under the {easy_cap:.0f} cap). "
                         "You're set up for quality work.")
    return "MODERATE", "Normal training rhythm. Today is what you make it."


def _race_countdown(today: date, week: Optional[dict] = None) -> tuple:
    """(days, phase, race_name). The plan's phase when there is one."""
    try:
        info = config.active_race(today)
        race_day = date.fromisoformat(str(info["date"]))
        name = info.get("name") or "race"
    except Exception:
        return None, "", "no race set"
    days = (race_day - today).days
    if week:
        try:
            ph = mp.phase_by_id(week["phase"])
            phase = ph["name"] if ph else str(week["phase"])
        except Exception:
            phase = str(week.get("phase", ""))
        if days > 0:
            return days, phase, name
    if days < 0:
        return days, "Race done", name
    if days == 0:
        return 0, "RACE DAY", name
    if days <= 7:
        return days, "Race week: protect freshness", name
    if days <= 14:
        return days, "Taper: sharp, not tired", name
    if days <= 28:
        return days, "Peak block: every session counts", name
    return days, "Build", name


def _today_session(today: date, state: Optional[dict] = None) -> tuple:
    """(day, week, standing_notes): today's session after standing notes, or (None, week, notes)."""
    if not mp.has_plan():
        return None, None, []
    try:
        state = state if state is not None else mp.load_state()
        week = mp.current_week(today)
        standing = mp.standing_notes(today, state)
    except Exception:
        return None, None, []
    if not week:
        return None, None, standing
    try:
        plan = mp.load_plan()
        sessions = plan_layout.apply_notes(plan_layout.sessions_for_week(week, plan), standing)
        day = dict(sessions[today.weekday()])
        day["_paces"] = plan.get("paces") or {}
        return day, week, standing
    except Exception:
        return None, week, standing


def _consistency_line(runs: list) -> Optional[str]:
    try:
        streak = metrics.current_streak(runs)
        ep = metrics.eddington_progress(runs)
        w = metrics.weeks_with_3plus_runs(runs, weeks_window=8)
        return (f"  Consistency: streak {streak}d  |  Eddington {ep['current']} "
                f"({ep['runs_needed_for_next']} more runs of {units.fmt_dist(mi=ep['next_n'], nd=0)} "
                f"for E{ep['next_n']})  |  weeks with 3+ runs: {w['recent_4_weeks_3plus']}/4")
    except Exception:
        return None


def build_brief(today: Optional[date] = None, runs: Optional[list] = None) -> str:
    """Assemble the daily brief as a single string."""
    today = today or date.today()
    runs = _load_runs() if runs is None else runs
    day, week, standing = _today_session(today)
    load = _last_days_load(runs, today)
    fatigue, fatigue_note = _fatigue_status(load, week)
    days_to_race, phase, race_name = _race_countdown(today, week)
    consistency = _consistency_line(runs)

    out = ["", "=" * 60, f"  DAILY BRIEF  |  {today.strftime('%A, %B %d, %Y')}", "=" * 60, ""]
    if days_to_race is None:
        out.append(f"  Race:       {race_name}")
    else:
        out.append(f"  Race:       {race_name}  |  {days_to_race} days  |  {phase}")
    hr_txt = f", avg HR {load['avg_hr']:.0f}" if load["avg_hr"] else ""
    out.append(f"  Fatigue:    {fatigue}  ({units.fmt_dist(mi=load['total_mi'])} in the last "
               f"{load['days']} days{hr_txt})")
    out.append(f"  -> {fatigue_note}")
    if consistency:
        out.append("")
        out.append(consistency)
    out.append("")

    out.append("-" * 60)
    head = f"  TODAY  |  {today.strftime('%a %b %d')}"
    if week:
        head += f"  |  plan wk{week['week_num']} of {mp.total_weeks()}"
    out.append(head)
    out.append("-" * 60)
    if day:
        paces = day.pop("_paces", {})
        out.append(f"  Type:       {str(day.get('role', '')).upper()}")
        out.append(f"  Session:    {plan_layout.format_session(day, paces)}")
        if day.get("strides"):
            out.append(f"  Strides:    {day['strides']} x 15-20 s, relaxed and fast, full recovery")
        if day.get("strength"):
            out.append("  Strength:   lift today (after the run, or on its own)")
        if week:
            wk = (f"  This week:  {units.fmt_dist(mi=week['target_miles'])}, long run "
                  f"{units.fmt_dist(mi=week['long_run_target'])}")
            if week.get("key_workout"):
                wk += f".  Key: {week['key_workout']}"
            out.append(wk)
    elif not mp.has_plan():
        out.append("  No plan yet. Say \"build me a plan\" (or: python3 coach.py plan --from-data).")
        out.append("  Until then the brief tracks fatigue and consistency only.")
    elif week is None:
        try:
            weeks = mp.all_weeks()
            start = date.fromisoformat(weeks[0]["start_date"])
            end = date.fromisoformat(weeks[-1]["start_date"]) + timedelta(days=6)
            out.append(f"  Outside the plan window ({start.isoformat()} to {end.isoformat()}).")
        except Exception:
            out.append("  Outside the plan window.")
    else:
        out.append("  No session found for today in the plan.")
    if standing:
        out.append("")
        out.append("  Standing notes:")
        for n in standing:
            out.append(f"    - {n['text']} (until {n['until']})")
    out.append("")

    out.append("-" * 60)
    out.append(f"  LAST {load['days']} DAYS")
    out.append("-" * 60)
    if load["recent"]:
        for a in load["recent"]:
            d = _run_date(a)
            dist_mi = (a.get("distance") or 0) / MI_M
            mt = (a.get("moving_time") or 0) / 60
            pace = mt / dist_mi if dist_mi > 0 else 0
            hr = a.get("average_heartrate")
            try:
                kind = enrichment.classify_run(a).replace("_", " ")
            except Exception:
                kind = ""
            out.append(f"  {d.isoformat() if d else '?'} | {units.fmt_dist(mi=dist_mi):>8} | "
                       f"{units.fmt_pace(pace):>10} | {('HR %.0f' % hr) if hr else 'no HR':<7} | {kind}")
    else:
        out.append(f"  No runs in the last {load['days']} days.")
    out.append("")
    out.append("=" * 60)
    out.append("  Show up. Run the plan. Easy days easy.")
    out.append("=" * 60)
    out.append("")
    return "\n".join(out)


def save_brief(text: Optional[str] = None, today: Optional[date] = None) -> Path:
    """Write the brief to plan_output/brief.md and return the path."""
    if text is None:
        text = build_brief(today)
    BRIEF_PATH.parent.mkdir(parents=True, exist_ok=True)
    BRIEF_PATH.write_text(text, encoding="utf-8")
    return BRIEF_PATH


def print_brief(save: bool = False, today: Optional[date] = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    text = build_brief(today)
    print(text)
    if save:
        save_brief(text)
    return 0


def main(argv: Optional[list] = None) -> int:
    args = sys.argv[1:] if argv is None else list(argv)
    today = None
    if "--date" in args:
        today = date.fromisoformat(args[args.index("--date") + 1])
    return print_brief(save="--save" in args, today=today)


if __name__ == "__main__":
    sys.exit(main())
