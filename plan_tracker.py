"""Plan compliance + decision point evaluation + slide logic.

Compares actual training (from metrics.load_activities) and fitness (from
fitness_tracker.current_status) to the marathon plan in marathon_plan.py.

Public API:
    weekly_compliance(week_num) -> dict
    evaluate_decision_point(dp_id) -> dict
    slide_plan(weeks=1) -> None
    mark_week_complete(week_num) / mark_week_missed(week_num)
    auto_classify_week_status(week_num) -> str
    metric_value(metric_name) -> any  # for decision point criteria
"""

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import marathon_plan as mp


# ---------------------------------------------------------------------------
# Activity helpers (works with both cache JSONs and CSV-loaded dicts)
# ---------------------------------------------------------------------------

def _activity_date(a: dict) -> Optional[date]:
    iso = a.get("start_date_local") or a.get("start_date") or ""
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _activity_distance_mi(a: dict) -> float:
    return (a.get("distance", 0) or 0) / 1609.34


def _is_run(a: dict) -> bool:
    return a.get("type") == "Run" and not a.get("_deleted_at")


def _runs_in_week(runs: list, week_start: date) -> list:
    end = week_start + timedelta(days=7)
    return [r for r in runs
            if _is_run(r)
            and (d := _activity_date(r)) is not None
            and week_start <= d < end]


# ---------------------------------------------------------------------------
# Weekly compliance
# ---------------------------------------------------------------------------

def weekly_compliance(week_num: int) -> dict:
    """Compare actual training in a plan week to its targets.

    Returns:
        {
          "week_num": int,
          "start_date": iso str,
          "target_miles": float,
          "miles_actual": float,
          "miles_pct": float (0-1+),
          "long_run_target": float,
          "long_run_actual": float,
          "long_run_hit": bool,
          "run_count": int,
          "status": "complete" | "in_progress" | "missed" | "upcoming",
        }
    """
    week = mp.week_by_num(week_num)
    if not week:
        return {"error": f"Week {week_num} not in plan"}

    week_start = date.fromisoformat(week["start_date"])
    today = date.today()

    # Adjust for slide offset (effective_start = original + offset)
    state = mp.load_state()
    offset = state.get("slide_offset_weeks", 0)
    effective_start = week_start + timedelta(weeks=offset)
    effective_end = effective_start + timedelta(days=7)

    # Status from explicit override first
    explicit = state.get("weeks_status", {}).get(str(week_num))

    # Load runs scoped to the effective window
    from metrics import load_activities
    all_runs = load_activities(activity_type="Run")
    week_runs = _runs_in_week(all_runs, effective_start)

    miles_actual = sum(_activity_distance_mi(r) for r in week_runs)
    long_run_actual = max((_activity_distance_mi(r) for r in week_runs), default=0)
    target_mi = week["target_miles"]
    long_target = week["long_run_target"]
    miles_pct = miles_actual / target_mi if target_mi > 0 else 0
    long_run_hit = (long_run_actual >= long_target * 0.9) if long_target > 0 else True

    # Auto-classify status if no override
    if explicit:
        status = explicit
    elif effective_end <= today:
        status = _auto_classify(miles_pct, long_run_hit)
    elif effective_start <= today < effective_end:
        status = "in_progress"
    else:
        status = "upcoming"

    return {
        "week_num": week_num,
        "start_date": effective_start.isoformat(),
        "phase": week["phase"],
        "target_miles": target_mi,
        "miles_actual": round(miles_actual, 1),
        "miles_pct": round(miles_pct, 2),
        "long_run_target": long_target,
        "long_run_actual": round(long_run_actual, 1),
        "long_run_hit": long_run_hit,
        "run_count": len(week_runs),
        "key_workout": week.get("key_workout"),
        "notes": week.get("notes", ""),
        "status": status,
    }


def _auto_classify(miles_pct: float, long_run_hit: bool) -> str:
    """Heuristic: complete if hit 80%+ AND long run hit; missed if below 50%."""
    if miles_pct >= 0.8 and long_run_hit:
        return "complete"
    if miles_pct < 0.5:
        return "missed"
    return "in_progress"


def auto_classify_week_status(week_num: int) -> str:
    return weekly_compliance(week_num)["status"]


# ---------------------------------------------------------------------------
# State mutations (slide, mark)
# ---------------------------------------------------------------------------

def slide_plan(weeks: int = 1) -> None:
    """Shift the plan back by N weeks. Useful when life makes you miss a week."""
    state = mp.load_state()
    state["slide_offset_weeks"] = state.get("slide_offset_weeks", 0) + weeks
    mp.save_state(state)
    print(f"  [plan_tracker] Plan slid by {weeks} week(s). New offset: {state['slide_offset_weeks']}.")


def mark_week_complete(week_num: int) -> None:
    state = mp.load_state()
    state.setdefault("weeks_status", {})[str(week_num)] = "complete"
    mp.save_state(state)


def mark_week_missed(week_num: int) -> None:
    state = mp.load_state()
    state.setdefault("weeks_status", {})[str(week_num)] = "missed"
    mp.save_state(state)


# ---------------------------------------------------------------------------
# Metric resolution for decision points
# ---------------------------------------------------------------------------

def metric_value(metric_name: str) -> Optional[float]:
    """Resolve a decision-point metric name to a current value."""
    if metric_name == "ctl":
        try:
            from fitness_tracker import current_status
            return current_status().get("ctl", 0)
        except Exception:
            return None

    if metric_name == "longest_run_30d_mi":
        from metrics import load_activities
        runs = load_activities(activity_type="Run")
        cutoff = date.today() - timedelta(days=30)
        recent = [r for r in runs
                  if _is_run(r) and (d := _activity_date(r)) is not None and d >= cutoff]
        return max((_activity_distance_mi(r) for r in recent), default=0)

    if metric_name == "weeks_at_4plus_of_4":
        from metrics import load_activities
        runs = load_activities(activity_type="Run")
        today = date.today()
        count = 0
        for w in range(4):
            wk_start = today - timedelta(days=today.weekday()) - timedelta(weeks=w)
            wk_runs = _runs_in_week(runs, wk_start)
            if len(wk_runs) >= 4:
                count += 1
        return count

    if metric_name == "weekly_mi_4wk_avg":
        from metrics import load_activities
        runs = load_activities(activity_type="Run")
        today = date.today()
        total_mi = 0
        for w in range(4):
            wk_start = today - timedelta(days=today.weekday()) - timedelta(weeks=w)
            wk_runs = _runs_in_week(runs, wk_start)
            total_mi += sum(_activity_distance_mi(r) for r in wk_runs)
        return total_mi / 4

    if metric_name == "tempo_4mi_test_pace":
        # Placeholder: would scan for a recent ~4mi run at sub-9:00 pace
        return None

    if metric_name == "no_recent_injury_sidelined":
        # Hard to determine from data alone; default optimistic
        return True

    if metric_name == "mp_segment_completed_mi":
        # Find any run with an MP-paced segment of 12+ miles
        from metrics import load_activities
        runs = load_activities(activity_type="Run")
        cutoff = date.today() - timedelta(days=45)
        best = 0
        for r in runs:
            if not _is_run(r):
                continue
            d = _activity_date(r)
            if not d or d < cutoff:
                continue
            # Crude proxy: distance >= 12mi AND avg pace <= 9:00 (close to MP 8:35)
            dist = _activity_distance_mi(r)
            mt = (r.get("moving_time", 0) or 0) / 60
            if dist >= 12 and mt > 0:
                pace = mt / dist
                if pace <= 9.0:
                    # Estimate MP-paced segment as full distance
                    if dist > best:
                        best = dist
        return best

    if metric_name == "recovery_quality":
        # Placeholder
        return True

    return None


# ---------------------------------------------------------------------------
# Decision point evaluation
# ---------------------------------------------------------------------------

def evaluate_decision_point(dp_id: str) -> dict:
    """Evaluate a decision point against current data.

    Returns:
        {
          "id": str,
          "name": str,
          "status": "on_track" | "at_risk" | "off_track" | "future",
          "evaluate_date": iso str,
          "criteria_results": [
              {"label": str, "metric": str, "target": value,
               "actual": value, "met": bool, "optional": bool}
          ],
          "downgrade_action": str,
        }
    """
    dp = next((d for d in mp.all_decision_points() if d["id"] == dp_id), None)
    if not dp:
        return {"error": f"Decision point {dp_id} not found"}

    today = date.today()
    eval_date = date.fromisoformat(dp["evaluate_date"])

    # Apply slide offset to evaluation date
    state = mp.load_state()
    offset = state.get("slide_offset_weeks", 0)
    effective_eval_date = eval_date + timedelta(weeks=offset)

    if today < effective_eval_date:
        # Future evaluation; still compute for preview but mark status differently
        is_future = True
    else:
        is_future = False

    results = []
    required_met = 0
    required_total = 0
    optional_met = 0
    optional_total = 0

    for crit in dp["criteria"]:
        actual = metric_value(crit["metric"])
        target = crit["value"]
        op = crit["op"]
        is_optional = crit.get("optional", False)

        met = False
        if actual is None:
            met = False
        elif op == ">=":
            met = actual >= target
        elif op == "<=":
            met = actual <= target
        elif op == "==":
            met = actual == target
        elif op == ">":
            met = actual > target
        elif op == "<":
            met = actual < target

        if is_optional:
            optional_total += 1
            if met:
                optional_met += 1
        else:
            required_total += 1
            if met:
                required_met += 1

        results.append({
            "label": crit.get("label", crit["metric"]),
            "metric": crit["metric"],
            "target": target,
            "op": op,
            "actual": actual,
            "met": met,
            "optional": is_optional,
        })

    # Status logic
    if is_future:
        status = "future"
    elif required_met == required_total:
        status = "on_track"
    elif required_met >= required_total - 1:
        status = "at_risk"
    else:
        status = "off_track"

    return {
        "id": dp["id"],
        "name": dp["name"],
        "description": dp.get("description", ""),
        "status": status,
        "is_future": is_future,
        "evaluate_date": effective_eval_date.isoformat(),
        "after_week": dp["after_week"],
        "criteria_results": results,
        "required_met": required_met,
        "required_total": required_total,
        "optional_met": optional_met,
        "optional_total": optional_total,
        "downgrade_action": dp.get("downgrade_action", ""),
    }


def evaluate_all_decision_points() -> list:
    return [evaluate_decision_point(d["id"]) for d in mp.all_decision_points()]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli():
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    print("PLAN COMPLIANCE")
    print("=" * 60)
    cw = mp.current_week()
    if cw:
        c = weekly_compliance(cw["week_num"])
        print(f"Week #{c['week_num']} ({c['phase']}) starting {c['start_date']}")
        print(f"  Status: {c['status']}")
        print(f"  Miles: {c['miles_actual']}/{c['target_miles']} ({c['miles_pct']*100:.0f}%)")
        print(f"  Long run: {c['long_run_actual']}/{c['long_run_target']} mi "
              f"({'hit' if c['long_run_hit'] else 'missed'})")
        print(f"  Run count: {c['run_count']}")
    else:
        print("Today is outside the marathon plan window.")

    print()
    print("DECISION POINTS")
    print("=" * 60)
    for dp in evaluate_all_decision_points():
        print(f"\n{dp['name']} (eval {dp['evaluate_date']})")
        print(f"  Status: {dp['status'].upper()}")
        for c in dp["criteria_results"]:
            mark = "[x]" if c["met"] else "[ ]"
            opt = " (optional)" if c["optional"] else ""
            actual_s = f"{c['actual']:.1f}" if isinstance(c['actual'], (int, float)) else str(c['actual'])
            print(f"    {mark} {c['label']}{opt} -- target {c['target']}, actual {actual_s}")
        if dp["status"] in ("at_risk", "off_track"):
            print(f"  -> {dp['downgrade_action']}")


if __name__ == "__main__":
    _cli()
