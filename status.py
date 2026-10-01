"""What the coach has and what it still needs.

    python3 coach.py status          # human-readable
    python3 coach.py status --json   # for the Claude skills to branch on

Reads the cache, the MCP inbox (data/mcp/), config.json and the plan file,
and lists the next steps in order: data in, then config, then a plan. It
never writes anything.
"""

import json
import sys
from datetime import date
from pathlib import Path
from typing import Optional

import config
import marathon_plan as mp
import mcp_adapter
from enrichment import is_real_run
from fitness_tracker import MIN_HISTORY_DAYS


def _inbox_counts(inbox: Path) -> dict:
    def _n(sub: str) -> int:
        d = inbox / sub
        return len(list(d.glob("*.json"))) if d.exists() else 0

    loose = 0
    if inbox.exists():
        loose = len([p for p in inbox.glob("*.json") if p.stem not in mcp_adapter._IGNORED_STEMS])
    return {
        "list_pages": _n("list") + loose,
        "perf": _n("perf"),
        "streams": _n("streams"),
        "profile": (inbox / "profile.json").exists(),
        "zones": (inbox / "zones.json").exists(),
    }


def _newest(acts: list) -> Optional[dict]:
    return max(acts, key=lambda a: str(a.get("start_date_local") or a.get("start_date") or ""),
               default=None)


def next_steps(s: dict) -> list:
    out = []
    fetch = s.get("fetch") or {}
    if s["cache_count"] == 0:
        out.append("No activities yet: pull the last 90 days from the Strava MCP (list_activities "
                   "pages saved to data/mcp/list/) and run `python3 coach.py ingest`.")
    else:
        if s["history_days"] < MIN_HISTORY_DAYS:
            out.append(f"Only {s['history_days']} days of history: fitness (CTL) is still warming up. "
                       "Pull back to 90 days if Strava has it.")
        if fetch.get("perf_needed"):
            out.append(f"{len(fetch['perf_needed'])} recent run(s) lack heart rate and laps: fetch "
                       "get_activity_performance for each, save to data/mcp/perf/<id>.json, ingest again.")
        if fetch.get("streams_needed"):
            out.append(f"{len(fetch['streams_needed'])} long run(s) or workout(s) lack streams: fetch "
                       "get_activity_streams (time, heart_rate, velocity_smooth, cadence, distance, "
                       "altitude, moving; resolution 3000), save to data/mcp/streams/<id>.json, ingest again.")
    if not s["configured"]:
        out.append("No config.json: run `python3 coach.py init --from-mcp data/mcp/ --goal-time H:MM:SS` "
                   "(with profile.json and zones.json in the inbox it needs no other answers).")
    elif not s["plan_present"]:
        out.append("No training plan: `python3 coach.py plan --from-data` (add --days N, --long-day sun, "
                   "--quality strides to fit your week and any injury).")
    if not out:
        out.append("Up to date: `python3 coach.py` for the full report.")
    return out


def snapshot(today: Optional[date] = None, cache_dir=None, inbox=None, streams_dir=None) -> dict:
    today = today or date.today()
    cache_dir = Path(cache_dir or mcp_adapter.CACHE_DIR)
    inbox = Path(inbox or mcp_adapter.MCP_DIR)
    streams_dir = Path(streams_dir or mcp_adapter.STREAMS_DIR)

    acts = mcp_adapter._all_cached(cache_dir)
    runs = [a for a in acts if a.get("type") == "Run" and is_real_run(a)]
    newest = _newest(acts)
    newest_run = _newest(runs)
    fetch = mcp_adapter.fetch_plan(acts, today=today, inbox=inbox, streams_dir=streams_dir)
    try:
        race = config.active_race(today)
    except Exception:
        race = None
    try:
        unit = config.units()
    except Exception:
        unit = "mi"

    s = {
        "home": str(config.HOME),
        "configured": config.CONFIG_PATH.exists(),
        "config_path": str(config.CONFIG_PATH),
        "units": unit,
        "race": ({"id": race.get("id"), "name": race.get("name"), "date": race.get("date"),
                  "distance_mi": race.get("distance_mi"), "goal_time": race.get("goal_time"),
                  "days_to_race": (date.fromisoformat(str(race["date"])) - today).days}
                 if race and race.get("date") else None),
        "cache_count": len(acts),
        "run_count": len(runs),
        "history_days": fetch.get("history_days", 0),
        "history_sufficient": fetch.get("history_days", 0) >= MIN_HISTORY_DAYS,
        "newest_activity": (str(newest.get("start_date_local") or newest.get("start_date"))[:10]
                            if newest else None),
        "newest_run_id": newest_run.get("id") if newest_run else None,
        "streams_cached": len(list(streams_dir.glob("*.json"))) if streams_dir.exists() else 0,
        "plan_present": mp.has_plan(),
        "plan_path": str(config.plan_path(race)) if race else None,
        "fetch": {"range_start": fetch.get("range_start"),
                  "perf_needed": sorted(fetch.get("perf_needed") or []),
                  "streams_needed": sorted(fetch.get("streams_needed") or [])},
        "inbox": _inbox_counts(inbox),
    }
    s["next"] = next_steps(s)
    return s


def render(s: dict) -> str:
    lines = ["STATUS", f"  Home:        {s['home']}",
             f"  Config:      {'yes' if s['configured'] else 'no (using the example defaults)'}  |  units {s['units']}"]
    r = s.get("race")
    if r:
        lines.append(f"  Race:        {r['name']} on {r['date']} ({r['days_to_race']} days), "
                     f"goal {r.get('goal_time') or 'not set'}")
    lines.append(f"  Activities:  {s['cache_count']} cached ({s['run_count']} runs), "
                 f"{s['history_days']} days of history"
                 + ("" if s["history_sufficient"] else f" (< {MIN_HISTORY_DAYS}: fitness read unreliable)"))
    if s["newest_activity"]:
        lines.append(f"  Newest:      {s['newest_activity']}  |  streams cached for {s['streams_cached']} run(s)")
    f = s["fetch"]
    lines.append(f"  Still to fetch: {len(f['perf_needed'])} performance, {len(f['streams_needed'])} streams"
                 + (f" (window from {f['range_start'][:10]})" if f.get("range_start") else ""))
    ib = s["inbox"]
    lines.append(f"  Inbox:       {ib['list_pages']} list page(s), {ib['perf']} perf, {ib['streams']} streams, "
                 f"profile {'yes' if ib['profile'] else 'no'}, zones {'yes' if ib['zones'] else 'no'}")
    lines.append(f"  Plan:        {'yes' if s['plan_present'] else 'no'}")
    lines.append("")
    lines.append("  NEXT")
    for n in s["next"]:
        lines.append(f"  - {n}")
    return "\n".join(lines)


def main(argv: Optional[list] = None) -> int:
    args = sys.argv[1:] if argv is None else list(argv)
    s = snapshot()
    if "--json" in args:
        print(json.dumps(s, indent=2, default=str))
    else:
        print(render(s))
    return 0


if __name__ == "__main__":
    sys.exit(main())
