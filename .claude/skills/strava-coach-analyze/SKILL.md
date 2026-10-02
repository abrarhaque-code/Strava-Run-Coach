---
name: strava-coach-analyze
description: The entry point. Pull the athlete's Strava history through the Strava MCP, set the coach up on first run (one question), and give the report - today's session, fitness, race forecast, the latest run's review, the week. Use for "coach me", "set me up", "how is my training going", "what should I do today", "what shape am I in", or any request for a report from live Strava data.
---

# Coach me, from live Strava data

You drive the strava-run-coach engine. It does the maths (VDOT, CTL/ATL/TSB,
grades, probabilities, pace bands); you get clean data into it and relay the
output the way `docs/COACHING.md` says. Read that file once per session.
Never recompute its numbers by hand.

## 0. Where the engine lives

Run every command from the repo root (where `coach.py` is). Installed as a
plugin, the code is at `${CLAUDE_PLUGIN_ROOT}` and state must live in
`${CLAUDE_PLUGIN_DATA}` (it survives plugin updates), so the command shape is

```bash
STRAVA_COACH_HOME="${CLAUDE_PLUGIN_DATA}" python3 "${CLAUDE_PLUGIN_ROOT}/coach.py" status --json
```

From a clone, plain `python3 coach.py status --json` is the same thing (state
lives next to the code). The rest of this file writes the short form. No
checkout and no plugin: `git clone https://github.com/abrarhaque-code/Strava-Run-Coach`.

## 1. Is the Strava MCP connected?

Check for a `list_activities` tool. If it is missing, say how to connect it
and either stop or fall back to the demo:

- The Claude desktop app (Cowork): Settings -> Connectors -> enable **Strava**,
  with the project folder open in the session (the engine has to run somewhere;
  in a web browser alone Claude can read Strava but cannot run `coach.py`).
- Claude Code: the plugin and the repo both carry the Strava server; if it is
  missing, `claude mcp add --transport http strava https://mcp.strava.com/mcp`,
  approve it, then `/mcp` to authenticate.
- No Strava at all: `python3 coach.py init --sample` builds a demo athlete.

## 2. Where things stand

```bash
python3 coach.py status --json
```

Read `configured`, `cache_count`, `history_days`, `fetch.range_start`,
`fetch.perf_needed`, `fetch.streams_needed`, `plan_present`, and `next`. The
`next` list is in order; follow it. On a first run (`configured: false`) also
save the athlete's profile and zones, verbatim, before anything else:

- `get_athlete_profile()` -> `data/mcp/profile.json`
- `get_athlete_zones()` -> `data/mcp/zones.json`

(`data/mcp/` is the inbox; it is gitignored. Write each tool result to its
file exactly as returned. A persisted `[{"type":"text","text":"..."}]` wrapper
is fine: the ingest unwraps it. Never reshape or trim payloads.)

## 3. Data in: 90 days, three kinds of call

1. `list_activities` with `first: 100`, `range_start: <fetch.range_start>`
   (90 days back on an empty cache; fitness is a 42-day average and a short
   pull reads as a false "overreaching"), `include_tags: true`. While the
   response says more pages exist (`has_next_page` / `pageInfo.hasNextPage`),
   call again with `after` = the end cursor. Save each page to
   `data/mcp/list/page-1.json`, `page-2.json`, ...
2. `python3 coach.py ingest` (reads `data/mcp/`), then `status --json` again.
3. For every id in `fetch.perf_needed`: `get_activity_performance(id)` ->
   `data/mcp/perf/<id>.json` (heart rate and laps; without them TSS falls back
   to pace and the review is shallow). For every id in `fetch.streams_needed`:
   `get_activity_streams(id, streams=["time","heart_rate","velocity_smooth",
   "cadence","distance","altitude","moving"], resolution=3000)` ->
   `data/mcp/streams/<id>.json` (stops, time in zones, reps, the wrist-HR check).
4. `python3 coach.py ingest` again.

This is one to three list pages plus a handful of per-run calls. Do it without
asking; it is the setup the athlete wanted when they said "coach me".

## 4. First run only: config, and the one question

If `configured` was false:

```bash
python3 coach.py init --from-mcp --dry-run
```

It prints the athlete Strava knows (name, units, zones, observed max HR, their
real easy pace) and the race Strava says they are training for, plus a NEEDS
list, which is normally just the goal time. Ask ONE question, reading the
race back:

> Strava says you're training for the **New York City Marathon** on
> **2026-11-01** (26.2 mi). What's your goal time? If anything there is wrong,
> correct it in the same reply.

Then write it, with whatever they corrected:

```bash
python3 coach.py init --from-mcp --goal-time 3:45:00 [--race-name "..." --race-date YYYY-MM-DD --distance 42.2km]
```

It writes `config.json`, re-enriches the cache so load follows the new zones,
and generates a plan to the race (5 days/week, Saturday long run by default;
the `build-plan` skill tunes days, long-run day, lifting and injuries). No race
at all: `--no-race`, then offer `build-plan` when they have one.

## 5. The report

```bash
python3 coach.py            # brief + fitness + forecast + latest review + week
```

One piece at a time: `brief` (today), `fitness`, `forecast`, `review` (latest
run; see `review-run`), `week` (see `weekly-review`), `status`.

## 6. Relay (the COACHING.md digest)

- Where they are first, wins included; then what needs to happen; then how.
- Today's session and the week's shape; predicted time vs goal with the
  probability as a band; the latest run's grade and the one dimension that
  set it.
- Under 60 days of history the fitness read is `UNRELIABLE`: say the numbers
  are warming up and leave it there. No "overreaching", no "lost fitness".
- A rising HR late in a run is not decoupling until the pace-matched line says
  so; a fast finish may be deliberate: ask.
- Missed sessions: adjust and move forward. One focus, not a list.
- Their units, never yours.

## Never

- Paste raw JSON or tool results at the athlete.
- Recompute VDOT, TSS, CTL or a grade by hand.
- Edit `config.json` by hand; use `init --from-mcp --force` with confirmation.
- Run `init --sample` on a real athlete's home: it adds fake runs.
- Ask more than the one setup question before showing them something.
- Commit anything under `data/`, `plan_output/` or `config.json`.
