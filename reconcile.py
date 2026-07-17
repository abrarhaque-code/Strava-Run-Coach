"""Reconcile actual training against the plan and persist per-week actuals.

This is the feedback loop the system was missing: after every sync (or on
demand), each elapsed plan week's actuals (real-run miles, run count, long
run, cross-training credit) are computed via plan_tracker and written into
data/plan_state.json, with deltas printed. Manual week-status overrides are
never touched; auto entries self-correct as late data arrives.

Usage:
    python3 reconcile.py               # reconcile through today
    python3 reconcile.py --date 2026-08-31   # frozen date (tests/backfill)
    from reconcile import reconcile    # library use
"""

import sys
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import marathon_plan as mp
import plan_tracker


# A week in one of these statuses is finalized — its recorded history must
# never be rewritten by a later slide (which shifts every week's effective
# window) or by a stale backfill call (a --date in the past recomputing a
# week that has since concluded with more data).
TERMINAL_STATUSES = ("complete", "partial", "missed")


def _actuals_entry(c: dict, stamp: str) -> dict:
    """Build the weeks_actuals record from a weekly_compliance result."""
    return {
        "start_date": c["start_date"],
        "run_mi": c["miles_actual"],
        "runs": c["run_count"],
        "long_run_mi": c["long_run_actual"],
        "miles_pct": c["miles_pct"],
        "long_run_hit": c["long_run_hit"],
        "bike_sessions": c["bike_sessions"],
        "bike_min": c["bike_min"],
        "bike_equiv_mi": c["bike_equiv_mi"],
        "status": c["status"],
        "computed_at": stamp,
    }


def _values_changed(old: Optional[dict], new: dict) -> bool:
    """Compare actuals ignoring the computed_at timestamp (idempotency)."""
    if old is None:
        return True
    keys = [k for k in new if k != "computed_at"]
    return any(old.get(k) != new.get(k) for k in keys)


def reconcile(today: Optional[date] = None, verbose: bool = True) -> dict:
    """Reconcile all elapsed plan weeks; write plan_state; return summary.

    Guards:
    - Frozen history: a week already in a TERMINAL status (complete/partial/
      missed) is never rewritten — not by a later slide_plan() shifting its
      effective window, and not by a stale --date backfill recomputing it
      as in_progress/upcoming. History, once finalized, stays finalized.
    - Coverage guard: a week whose effective window ends before the
      earliest activity this pass actually loaded gets no signal at all —
      that's missing coverage (fresh clone, a short --backfill window),
      not evidence of zero training. Such a week's existing record (if
      any) is preserved and flagged data_stale; a week never before
      recorded is left unwritten rather than fabricating a zero. The flag
      clears automatically once a pass loads data that does cover it.
    - Manual overrides: a week whose weeks_status_source is "manual" (or
      absent — same default plan_tracker.weekly_compliance uses) keeps its
      status; only "auto" entries are ever rewritten here.
    """
    if today is None:
        today = date.today()

    current = mp.current_week(today)
    if current is None:
        # Before plan start or after race — reconcile everything elapsed
        last_week_num = mp.total_weeks() if today > mp.race_date() else 0
    else:
        last_week_num = current["week_num"]

    if last_week_num == 0:
        if verbose:
            print("  [reconcile] Plan has not started; nothing to reconcile.")
        return {"weeks_reconciled": 0, "changes": []}

    from metrics import load_activities
    from plan_tracker import _activity_date
    runs = load_activities(activity_type="Run")
    earliest_loaded = min((d for r in runs if (d := _activity_date(r)) is not None),
                          default=None)

    state = mp.load_state()
    actuals = state.setdefault("weeks_actuals", {})
    statuses = state.setdefault("weeks_status", {})
    sources = state.setdefault("weeks_status_source", {})
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")

    changes = []
    for n in range(1, last_week_num + 1):
        c = plan_tracker.weekly_compliance(n, today=today, runs=runs)
        if "error" in c:
            continue
        key = str(n)
        old = actuals.get(key)
        # Missing source defaults to "manual" ONLY when a status already
        # exists for this week (protects pre-schema-v2 manual marks written
        # before weeks_status_source existed) — a week with no status yet
        # must default to "auto" or reconcile could never write its first
        # status at all. Mirrors plan_tracker.weekly_compliance's own
        # default, which is moot there for the same reason (no entry means
        # nothing to protect).
        default_source = "manual" if key in statuses else "auto"
        is_manual = sources.get(key, default_source) == "manual"

        # Frozen history: don't let a later slide or a stale backfill call
        # rewrite a week that has already been finalized.
        if old and old.get("status") in TERMINAL_STATUSES:
            if old.get("start_date") != c["start_date"] or c["status"] in ("in_progress", "upcoming"):
                continue

        # Coverage guard: does this pass's loaded data actually reach back
        # far enough to say anything about this week?
        eff_start = date.fromisoformat(c["start_date"])
        eff_end = eff_start + timedelta(days=7)
        no_coverage = earliest_loaded is None or eff_end <= earliest_loaded
        if c["run_count"] == 0 and c["bike_sessions"] == 0 and no_coverage:
            if old is None:
                continue  # never fabricate a zero record from missing coverage
            if not old.get("data_stale"):
                old["data_stale"] = True
                changes.append(f"week {n}: no activity data loaded — kept "
                               f"previous actuals, flagged data_stale")
            continue
        if old and old.get("data_stale"):
            old.pop("data_stale", None)  # coverage returned; clear staleness

        new = _actuals_entry(c, stamp)
        new_status = c["status"]
        if _values_changed(old, new):
            if old:
                delta_bits = []
                for k, label in (("run_mi", "run_mi"), ("runs", "runs"),
                                 ("long_run_mi", "long_run"), ("status", "status")):
                    if old.get(k) != new.get(k):
                        delta_bits.append(f"{label} {old.get(k)} -> {new.get(k)}")
                changes.append(f"week {n}: " + "; ".join(delta_bits))
            else:
                changes.append(f"week {n}: recorded {new['run_mi']}mi, "
                               f"{new['runs']} runs, LR {new['long_run_mi']}mi "
                               f"[{new_status}]")
            actuals[key] = new

        # Status: only elapsed weeks, never overwrite a manual entry
        week_ended = new_status not in ("in_progress", "upcoming")
        if week_ended and not is_manual:
            statuses[key] = new_status
            sources[key] = "auto"

    state["last_reconciled"] = stamp
    mp.save_state(state)

    if verbose:
        print(f"  [reconcile] Weeks 1-{last_week_num} reconciled "
              f"({len(changes)} change{'s' if len(changes) != 1 else ''}).")
        for ch in changes:
            print(f"    {ch}")

    return {"weeks_reconciled": last_week_num, "changes": changes}


def add_note(text: str, today: Optional[date] = None) -> None:
    """Append a timestamped in-the-moment note to the current plan week."""
    if today is None:
        today = date.today()
    current = mp.current_week(today)
    key = str(current["week_num"]) if current else "general"
    state = mp.load_state()
    notes = state.setdefault("notes", {})
    stamp = today.isoformat()
    notes.setdefault(key, []).append(f"{stamp}: {text}")
    mp.save_state(state)
    print(f"  [reconcile] Note added to week {key}: {text}")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    today = None
    args = sys.argv[1:]
    if "--date" in args:
        today = date.fromisoformat(args[args.index("--date") + 1])
    reconcile(today=today)


if __name__ == "__main__":
    main()
