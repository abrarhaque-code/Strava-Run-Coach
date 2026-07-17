---
name: strava-coach-analyze
description: Pull the athlete's recent Strava history via the Strava MCP and run the coach engine (race forecast, fitness, base-build scenarios). Use when the user asks to analyze their running, "coach me", "how is my training going", "what shape am I in", or wants a report from live Strava data.
---

# Analyze training from live Strava data

You are driving the strava-run-coach engine. The engine computes the numbers
(VDOT, CTL/ATL/TSB, goal probability, scenarios) — your job is to get clean
data into it and relay its output conversationally. Never recompute its math
by hand.

## 0. Locate the engine

Run from the repo root (where `coach.py` lives). If there is no checkout,
clone it first: `git clone https://github.com/abrarhaque-code/Strava-Run-Coach`
(if this skill came from the installed plugin, a copy of the engine also
lives at `${CLAUDE_PLUGIN_ROOT}` — fine for throwaway analysis, but data
written there does not survive plugin updates).

## 1. Verify the Strava MCP is connected

Check for a `list_activities` tool (Strava MCP). If it is missing, tell the
user how to connect it, then stop or fall back:

- claude.ai: Settings -> Connectors -> enable **Strava** (requires a Strava
  subscription).
- Claude Code: `claude mcp add --transport http strava https://mcp.strava.com/mcp`
  (the repo also ships this in `.mcp.json`; approve it and authenticate via `/mcp`).
- No Strava at all: offer the sample demo — `python3 coach.py init --sample`.

## 2. Pull the window (exact pagination loop)

Call `list_activities` with `first: 100` and `range_start` set to ~120 days
ago as an ISO LocalDateTime (e.g. `2026-03-18T00:00:00`) — the coach anchors
fitness on the trailing weeks and endurance efforts. Then loop:

1. Append the page's `activities` to your collection.
2. While the response says more pages exist (`has_next_page` /
   `pageInfo.hasNextPage`), call again passing the cursor (`after` =
   `end_cursor` / `pageInfo.endCursor`).

Save everything **verbatim** as `{"activities": [...]}` to
`data/mcp_activities.json` (gitignored by design). Do not reshape or trim
activity objects — the adapter reads the `summary.*` fields exactly as the
MCP emits them. Multiple page files also work:
`coach.py analyze --from-mcp p1.json p2.json`.

## 3. Optional but recommended: heart rate + laps

`list_activities` summaries carry **no heart rate**. For the runs that
matter (races, long runs, workouts — say the 5-10 most recent significant
runs), call `get_activity_performance(activity_id)` and save the payloads
keyed by id to `data/mcp_performance.json`:

```json
{"19330270757": { ...verbatim performance payload... }}
```

This flips TSS from pace-based to HR-based and lights up lap analysis.

## 4. Ingest + report

```bash
python3 coach.py analyze --from-mcp data/mcp_activities.json --performance data/mcp_performance.json
python3 coach.py fitness       # optional: CTL/ATL/TSB detail
```

The ingest classifies cross-training logged as "Run" (bike sessions) into
the aerobic-load stream automatically — do not delete or edit those entries.

## 5. Relay

Summarize conversationally: predicted race time vs goal, goal probability,
CTL/ATL/TSB and phase, scenario feasibility, and whatever the report itself
flags. Frame ranges as planning bands, not promises.

## Never

- Paste raw activity JSON back to the user.
- Recompute VDOT/CTL/TSS by hand instead of running the engine.
- Edit `config.json` unasked (offer `coach.py init --from-mcp-zones` if the
  user wants zone calibration from their Strava zones).
- Commit anything under `data/` or `activities.csv` — it is personal data
  and gitignored for a reason.
