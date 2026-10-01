"""Unified coach output. The single command for everything you need.

Usage:
    python3 coach.py            # full coaching report (default)
    python3 coach.py brief      # daily brief only
    python3 coach.py review [<id>] [--json]  # post-run review (latest run by default)
    python3 coach.py fitness    # fitness tracker (CTL/ATL/TSB)
    python3 coach.py forecast   # race forecast
    python3 coach.py week       # weekly check-in
    python3 coach.py dashboard  # regenerate the HTML dashboard
    python3 coach.py sync       # sync from Strava + full report
    python3 coach.py scenario   # base-build scenarios (20/25/30 -> peak -> marathon)
    python3 coach.py plan [--from-data|--entry N] [--days 4 --long-day sun --quality strides] [--ics]
    python3 coach.py ingest data/mcp/   # merge Strava MCP payloads (list/perf/streams) into the cache
    python3 coach.py analyze [data/mcp/]  # ingest, then the full report + base-build scenarios
    python3 coach.py reconcile  # record actual-vs-planned into plan_state.json
    python3 coach.py note "..." [--until YYYY-MM-DD]  # log an adjustment; --until keeps it in force
    python3 coach.py trends     # long-horizon lenses: drift, efficiency, recovery
    python3 coach.py init       # first-run setup wizard

Examples:
    python3 coach.py scenario --entry 20,25,30
    python3 coach.py plan --entry 25 --weeks 16
"""

import sys
import subprocess
from pathlib import Path

import config


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


def _auto_reconcile():
    """Record actual-vs-planned after fresh data lands.

    Only meaningful when the active race carries a structured plan JSON;
    degrades to a one-line notice otherwise so sync/analyze never break.
    """
    try:
        if not config.has_structured_plan(config.active_race()):
            return
        from reconcile import reconcile
        reconcile()
    except Exception as e:
        print(f"  [coach] reconcile skipped: {e}")


def _has_any_data() -> bool:
    csv_path = config.CSV_PATH
    cache = config.ACTIVITIES_DIR
    return csv_path.exists() or (cache.exists() and any(cache.glob("*.json")))


def full_report(save_brief: bool = False):
    """Print the full coach report — everything in one shot.

    When save_brief=True, daily_brief also writes plan_output/brief.md so an
    external consumer (a notification script, a cron job, etc.) can read a
    fresh markdown copy.
    """
    if not _has_any_data():
        # A hint beats six empty report sections. Never auto-generate — a
        # user who intends to sync real data shouldn't get fake miles.
        print("No training data yet.")
        print("  Demo with sample data:  python3 coach.py init --sample")
        print("  Or connect Strava:      see README 'Connect your Strava'")
        return

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

    # Off by default: a pep talk reads differently on someone else's terminal.
    if config.report_cfg().get("motivational_footer", False):
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
        "review": lambda: _run_module("post_run_review", extra),
        "fitness": lambda: _run_module("fitness_tracker"),
        "forecast": lambda: _run_module("race_predictor"),
        "metrics": lambda: _run_module("metrics"),
        "week": lambda: _run_module("weekly_check"),
        "dashboard": lambda: _run_module("dashboard"),
        "scenario": lambda: _run_module("scenario", extra),
        "plan": lambda: _run_module("plan_generator", extra),
        "reconcile": lambda: _run_module("reconcile", extra),
        "trends": lambda: _run_module("trends"),
        "init": lambda: _run_module("wizard", extra),
    }

    if cmd == "note":
        until, words = None, []
        i = 0
        while i < len(extra):
            a = extra[i]
            if a == "--until" and i + 1 < len(extra):
                until = extra[i + 1]
                i += 2
                continue
            if a.startswith("--until="):
                until = a.split("=", 1)[1]
            else:
                words.append(a)
            i += 1
        text = " ".join(words).strip()
        if not text:
            print('Usage: python3 coach.py note "what changed and why" [--until YYYY-MM-DD]')
            sys.exit(1)
        from reconcile import add_note
        try:
            add_note(text, until=until)
        except ValueError:
            print(f"  [coach] --until needs a date like 2026-11-15, got {until!r}")
            sys.exit(1)
        return

    if cmd == "sync":
        _run_module("strava_sync")
        _auto_reconcile()
        full_report(save_brief=True)
        # Dashboard auto-regen happens at end of sync; also kick a fresh one
        # in case sync was a no-op (still want dashboard updated)
        _run_module("dashboard")
        return

    if cmd == "ingest":
        # Merge Strava MCP payloads (list pages + perf/<id>.json + streams/<id>.json)
        # into the cache. `ingest` with no path reads data/mcp/.
        sys.exit(_run_module("mcp_adapter", extra))

    if cmd == "analyze":
        # Ingest, then the whole report. Lets any Claude session with the
        # Strava MCP drive the coach with no OAuth/sync. Legacy
        # `--from-mcp <file> [--performance <f>]` still works.
        import mcp_adapter
        try:
            opts = mcp_adapter.parse_cli(extra)
        except ValueError as e:
            print(f"  [mcp] {e}")
            print("Usage: python3 coach.py analyze [data/mcp/ | <files...>]")
            sys.exit(1)
        res = mcp_adapter.run_merge(opts["paths"], dry_run=opts["dry_run"])
        if opts["dry_run"]:
            return
        _auto_reconcile()
        full_report(save_brief=True)
        _section("BASE-BUILD SCENARIOS")
        _run_module("scenario")
        if res["import"] and res["import"]["written"] == 0:
            sys.exit(1)
        return

    if cmd not in routes:
        print(f"Unknown command: {cmd}")
        print(__doc__)
        sys.exit(1)

    routes[cmd]()


if __name__ == "__main__":
    main()
