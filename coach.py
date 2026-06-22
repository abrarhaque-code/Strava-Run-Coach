"""Unified coach output. The single command for everything you need.

Usage:
    python3 coach.py            # full coaching report (default)
    python3 coach.py brief      # daily brief only
    python3 coach.py review     # post-run review (latest run)
    python3 coach.py fitness    # fitness tracker (CTL/ATL/TSB)
    python3 coach.py forecast   # race forecast
    python3 coach.py week       # weekly check-in
    python3 coach.py dashboard  # regenerate the HTML dashboard
    python3 coach.py sync       # sync from Strava + full report
    python3 coach.py scenario   # base-build scenarios (20/25/30 -> peak -> marathon)
    python3 coach.py plan       # generate a parametric 16-week marathon plan
    python3 coach.py analyze --from-mcp <file.json>  # ingest Strava MCP JSON, then report
    python3 coach.py init       # first-run setup wizard

Examples:
    python3 coach.py scenario --entry 20,25,30
    python3 coach.py plan --entry 25 --weeks 16
"""

import sys
import subprocess
from pathlib import Path


SCRIPT_DIR = Path(__file__).parent


def _run_module(name: str, args: list = None) -> int:
    """Run a sibling Python module and return exit code."""
    cmd = [sys.executable, "-u", str(SCRIPT_DIR / f"{name}.py")]
    if args:
        cmd.extend(args)
    sys.stdout.flush()
    return subprocess.call(cmd)


def _section(title: str):
    print()
    print("#" * 70)
    print(f"#  {title}")
    print("#" * 70)
    print()
    sys.stdout.flush()


def full_report(save_brief: bool = False):
    """Print the full coach report — everything in one shot.

    When save_brief=True, daily_brief also writes plan_output/brief.md so an
    external consumer (a notification script, a cron job, etc.) can read a
    fresh markdown copy.
    """
    _section("DAILY BRIEF")
    _run_module("daily_brief", ["--save"] if save_brief else None)

    _section("FITNESS STATUS")
    _run_module("fitness_tracker")

    _section("CONSISTENCY + BEST EFFORTS")
    _run_module("metrics")

    _section("RACE FORECAST")
    _run_module("race_predictor")

    _section("LATEST RUN REVIEW")
    _run_module("post_run_review")

    _section("WEEKLY STATUS")
    _run_module("weekly_check")

    print()
    print("=" * 70)
    print("  THE BOTTOM LINE")
    print("=" * 70)
    print()
    print("  You're not training to feel comfortable. You're training to win.")
    print("  Every easy run protects a hard one. Every hard run earns the next.")
    print("  Show up. Run the plan. Trust the data.")
    print()


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    cmd = sys.argv[1] if len(sys.argv) > 1 else "full"
    extra = sys.argv[2:]

    routes = {
        "full": full_report,
        "brief": lambda: _run_module("daily_brief"),
        "review": lambda: _run_module("post_run_review"),
        "fitness": lambda: _run_module("fitness_tracker"),
        "forecast": lambda: _run_module("race_predictor"),
        "metrics": lambda: _run_module("metrics"),
        "week": lambda: _run_module("weekly_check"),
        "dashboard": lambda: _run_module("dashboard"),
        "scenario": lambda: _run_module("scenario", extra),
        "plan": lambda: _run_module("plan_generator", extra),
        "init": lambda: _run_module("setup"),
    }

    if cmd == "sync":
        _run_module("strava_sync")
        full_report(save_brief=True)
        # Dashboard auto-regen happens at end of sync; also kick a fresh one
        # in case sync was a no-op (still want dashboard updated)
        _run_module("dashboard")
        return

    if cmd == "analyze":
        # Ingest a Strava MCP list_activities JSON dump, then report. Lets any
        # Claude session with the Strava MCP drive the coach with no OAuth/sync.
        from mcp_adapter import ingest_mcp_file
        path = None
        for i, a in enumerate(extra):
            if a == "--from-mcp" and i + 1 < len(extra):
                path = extra[i + 1]
            elif a.startswith("--from-mcp="):
                path = a.split("=", 1)[1]
        if not path:
            print("Usage: python3 coach.py analyze --from-mcp <file.json>")
            sys.exit(1)
        summary = ingest_mcp_file(path)
        print(f"Ingested {summary['written']} activities from {path}")
        for t, c in sorted(summary["by_type"].items()):
            print(f"  {t:14} {c}")
        _section("RACE FORECAST")
        _run_module("race_predictor")
        _section("BASE-BUILD SCENARIOS")
        _run_module("scenario")
        return

    if cmd not in routes:
        print(f"Unknown command: {cmd}")
        print(__doc__)
        sys.exit(1)

    routes[cmd]()


if __name__ == "__main__":
    main()
