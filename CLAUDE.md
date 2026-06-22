# Strava Run Coach: working notes for Claude

A stdlib-only Python running coach. It reads Strava activity history and produces
fitness tracking (CTL/ATL/TSB), VDOT race prediction, adaptive plans, and a
base-build scenario projector. No external dependencies; run with `python3`.

## Daily coaching workflow (Strava MCP)

The engine reads activities from `data/strava_cache/activities/*.json`. You can
populate that cache straight from the Strava MCP, with no OAuth or `strava_sync`:

1. Pull the window you need via the Strava MCP `list_activities` (use
   `range_start` to cover the trailing weeks; the half/long runs anchor fitness).
2. Save the raw MCP JSON to `data/mcp_activities.json` (the `{"activities": [...]}`
   shape, verbatim).
3. Ingest + report:
   - `python3 coach.py analyze --from-mcp data/mcp_activities.json` (forecast + scenarios), or
   - `python3 coach.py brief` / `forecast` / `scenario` / `fitness` after ingest.
4. Relay the output. Do not paste raw activity JSON back to the user.

`mcp_adapter.py` maps the MCP shape to the cache shape and CLASSIFIES rather than
deletes cross-training: bike sessions logged as "Run" (and real rides) become
`type: CrossTrain`, so they feed aerobic load but never running mileage.

## Two-stream load model

- Running mileage (impact/durability, drives ramp + peak): runs only (`load_runs`).
- Aerobic load (CTL/ATL/TSB fitness/fatigue): runs + cross-training (bike via
  `config.crosstrain.intensity_factor`) + strength (`config.strength.tss_per_session`).
  See `fitness_tracker.load_sessions` / `compute_loads_all`.

## Base-build scenarios + plan generation

- `python3 coach.py scenario --entry 20,25,30` compares entry points: each implies
  a peak mileage and a marathon time RANGE. Marathon time anchors on the best
  recent ENDURANCE effort (half / long run), not the max VDOT (which would lean on
  a 5K and over-predict). Ramp rates are guardrails, not laws; ranges are planning
  bands. Tunable constants live in `config.json -> scenario`.
- `python3 coach.py plan --entry 25 --weeks 16 [--ics]` writes
  `data/marathon_plan.generated.json` (same schema as `data/marathon_plan.json`)
  and, with `--ics`, a single subscribable feed `plan_output/training.ics`
  (runs + long run + lift days) for Outlook.

## Adaptive + Outlook

The Microsoft 365 MCP here is read-only for calendar (no create-event). Push
workouts to Outlook by regenerating `plan_output/training.ics` and subscribing to
it once; re-generate weekly to adapt. Use the M365 calendar READ tools
(`outlook_calendar_search`, `find_meeting_availability`) to detect conflicts and
move a key session off blocked days.

## Conventions

- Stdlib only. No new dependencies.
- All athlete numbers live in `config.json` (falls back to `config.example.json`).
- Run tests: `python3 -m unittest discover -s tests`.
