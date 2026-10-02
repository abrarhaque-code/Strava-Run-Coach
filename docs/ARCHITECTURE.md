# Architecture

Developer context for `strava-run-coach`. This is a single-process, stdlib-only
Python application. There is no web server, no database, and no third-party
package. Everything reads local files and prints to a terminal or writes a
self-contained HTML file.

## Core rule: stdlib only

No third-party dependencies. The whole codebase uses only the Python standard
library (`json`, `csv`, `urllib`, `datetime`, `dataclasses`, `math`, `pathlib`,
`unittest`, and friends). This keeps a fresh clone runnable with nothing but a
Python 3.10+ interpreter, makes CI a no-install step, and removes supply-chain
surface area. Pull requests that add a runtime dependency will be declined. If
you reach for `requests`, use `urllib`. If you reach for `pandas`, use `csv`
plus plain dicts.

Python 3.10+ is required: the code uses PEP 604 unions (`int | None`), builtin
generics (`list[dict]`), and the walrus operator.

## The data interface contract

`activities.csv` is the seam the whole system is built around. It is the Strava
bulk-export CSV, parsed by fixed column index (the export has duplicate column
names, so name-based parsing is not safe). The analysis layer was written
against that file first, and the Strava API client was added later behind the
same data shape. That means:

- The CSV column layout is a contract. `analysis.py` and `fitness_tracker.py`
  read specific 0-based indices (date, type, distance in km, moving time, HR,
  cadence, elevation). Do not reorder or rename columns in code that produces
  this file.
- `activities.csv.example` is the canonical header and a small set of example
  rows. The sample-data generator reads its header verbatim so the generated
  file always matches what the parsers expect.
- The richer source is the per-activity JSON cache under
  `data/strava_cache/activities/<id>.json`, shaped like the Strava API activity
  object (distances in meters, `start_date_local` as naive-local ISO, optional
  `best_efforts`). Modules that need lap or best-effort detail read the cache;
  modules that only need weekly aggregates can read either.
- New activities are run through `enrichment.enrich()` at ingest, which
  pre-computes analytical fields under a reserved `_*` namespace (`_run_tss`,
  `_pace_zone`, `_workout_type`). Downstream code reads those instead of
  recomputing.
- `enrichment.classify_activity()` is the single source of truth for "is this
  a real run?". It buckets every activity as `run` / `treadmill_run` (count
  everywhere), `bike_equiv` (a bike session manually logged as a Run at the
  configured `crosstrain.bike_min_per_mi` equivalence speed — cross-training
  TSS only, never run mileage), `invalid` (zero-distance Run entries), `ride`
  (real rides and mcp-typed CrossTrain), or `other`. **Every run-metric
  consumer filters through `enrichment.is_real_run()`; if you add a consumer,
  use that helper.** Bumping `ENRICHMENT_VERSION` re-enriches the whole cache
  on the next sync.

Because both the CSV and the cache are gitignored, none of an athlete's data is
ever committed. The HTML dashboard renders locally and the data never leaves the
machine.

## Configuration

`config.py` centralizes all personalization. Every other module reads athlete
physiology, pace zones, races, theme, and calendar defaults from there instead
of hardcoding constants, so the whole system is tuned by editing one JSON file.

- `config.example.json` ships in the repo. `config.json` is gitignored.
- If `config.json` is absent, `config.py` falls back to the example so a fresh
  clone and CI still run.
- `config.py` imports nothing from the project, so it sits at the bottom of the
  import graph and can never create an import cycle.
- Race selection is data-driven: `config.active_race(today)` picks the earliest
  race whose date is today or later, and the last race once all are past. The
  "switch to the next race the day after this one" behavior falls out of that
  rule for any number of races. A manual override lives in
  `data/plan_state.json`.
- `coach.py init --from-mcp` (`onboarding.py`) writes `config.json` from the
  Strava MCP inbox and the cache; every derivation lands in `_meta.notes`.

### Where state lives

`config.home()` decides the root for everything the app writes: the code
directory for a clone, `~/.claude/plugins/data/strava-run-coach/` when the
code sits under a `.claude/plugins/` tree (so an installed plugin's data
survives updates), or `STRAVA_COACH_HOME` when set. Every path is a constant
on `config` (`DATA_DIR`, `ACTIVITIES_DIR`, `STREAMS_DIR`, `MCP_DIR`, `CSV_PATH`,
`CONFIG_PATH`, `STATE_PATH`, `PLAN_OUTPUT_DIR`, `ENV_PATH`); modules copy them
into module-level names so tests can swap them. Shipped files
(`config.example.json`, `docs/examples/`) stay code-relative.

### Units

The engine is in miles, minutes per mile and metres throughout. `units.py`
converts at the edges: `fmt_dist`, `fmt_pace`, `fmt_pace_range`,
`volume_label` for output, `parse_dist` / `to_mi` for input (`--entry 50km`),
`per_mi_to_user` for rates. `athlete.units` is the only switch; paces in the
config are always min/mi. The Eddington number counts in the athlete's unit.

## Module map

### Strava integration
- `strava_api.py` - API client with automatic token refresh and rate-limit
  awareness.
- `strava_authorize.py` - one-time OAuth flow to obtain tokens.
- `strava_sync.py` - pulls activities into the local CSV and JSON cache
  (incremental by default, with backfill and full modes).
- `enrichment.py` - the ingest pipeline that pre-computes analytical fields.

### Analysis
- `analysis.py` - CSV parser plus weekly aggregation, polarization, and ACTR
  (acute:chronic training ratio).
- `race_predictor.py` - Jack Daniels VDOT, race-time prediction, and goal
  probability.
- `fitness_tracker.py` - CTL/ATL/TSB training-load balance (fitness, fatigue,
  form) via exponentially weighted TSS.
- `metrics.py` - the cache loader chokepoint (`load_activities`,
  `load_streams`, `latest_run`), Eddington number, streaks, best efforts.
- `trends.py` - long-horizon lenses (efficiency, consistency, recovery,
  elevation cost, treadmill vs outdoor).
- `intervals.py` - reps reconstructed from a stream (laps on a track are
  auto splits); `session_intent.py` - a Strava description such as
  "13 easy, 3 at half pace, 2 easy" into placed segments; `hr_quality.py` -
  wrist-HR plausibility (post-stop re-acquisition, cadence lock).

### Planning
- `marathon_plan.py` - loads and validates the plan JSON for the active race
  (`config.plan_path`), slide-aware week lookup, plan state and notes
  (`standing_notes` carry an `until` date).
- `plan_generator.py` - writes the plan: race-distance presets, the `plan`
  config block and flags (days, long-run day, lifting, quality level, caps),
  week-level `days[]` from `plan_layout`.
- `plan_layout.py` - the 7-day layout of a plan week, `role_for_date` (the
  review's plan layer), `apply_notes` (a standing "no speedwork" note turns
  key days easy), `format_session`.
- `plan_tracker.py` - compliance, checkpoints (decision points), marathon-pace
  laps by pace and HR band.
- `reconcile.py` - actual-vs-planned into `data/plan_state.json`; notes.
- `ical_generator.py` - exports workouts as an `.ics` calendar.

### Coaching brain / output
- `daily_brief.py` - today's session from the layout (standing notes applied)
  plus a fatigue read against the week's targets.
- `post_run_review.py` - a pure `review(activity, streams, plan_week, laps)
  -> dict` and `render()`: intent, easy discipline, declared segments by pace
  and HR band, pace-matched decoupling, stops, the finish, load, a sensor
  flag, a grade. `weekly_check.py` consumes the dict for its run grades.
- `weekly_check.py` - the weekly check-in, plan-driven; writes
  `plan_output/weekly/`.
- `status.py` - what is cached, what the MCP still needs, next steps.
- `dashboard.py` - a self-contained static HTML dashboard.
- `coach.py` - the unified entry point that routes sub-commands and runs the
  full report.

### Data
- `data/<race_id>.generated.json` - the plan for a race, written by
  `plan_generator` (validated on load: contiguous week numbers, start dates
  exactly 7 days apart, phases referencing valid week ranges, 7 `days` per
  week when present). The annotated template is
  `docs/examples/plan.example.json`.
- `data/plan_state.json` - auto-managed slide offset, per-week actuals and
  status, notes, and the manual active-race override.
- `data/strava_cache/activities/` - the per-activity JSON cache;
  `data/strava_cache/streams/<id>.json` - raw streams for the review.
- `data/mcp/` - the Strava MCP inbox: `list/`, `perf/`, `streams/`,
  `profile.json`, `zones.json`. All gitignored.

## A naming note

`ical_generator.py` is named that way on purpose. A module named `calendar.py`
would shadow Python's standard-library `calendar` module on the import path and
break any code that imports it. Do not rename it to `calendar.py`.

## Adding a new analysis module

1. Write a module with a clear print/report function and a `main()` guarded by
   `if __name__ == "__main__":`.
2. Read inputs through `config.py` (never hardcode athlete numbers) and through
   the existing data loaders in `analysis.py` / `metrics.py` /
   `fitness_tracker.py` rather than re-parsing files.
3. Register it in `coach.py` so it shows up as a sub-command and in the full
   report.
4. Add a `unittest` test under `tests/` that runs with no `.env` and no network.
