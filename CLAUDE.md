# strava-run-coach: working notes for Claude

A stdlib-only Python running coach. Strava history in (Strava MCP JSON, REST
sync, or the bulk-export CSV) -> Daniels VDOT race prediction, CTL/ATL/TSB
fitness, a graded debrief of every run, plans built around the athlete's race
and week, weekly check-ins, long-horizon trends, an .ics feed, and an HTML
dashboard. `coach.py` is the single entry point. `docs/COACHING.md` is how the
coach talks: read it before relaying anything to an athlete.

## Commands

```bash
python3 coach.py init --sample                       # demo: config defaults + sample history + a plan
python3 coach.py init --from-mcp [data/mcp/] --goal-time 3:45:00 [--dry-run | --force]
python3 coach.py                                     # full report
python3 coach.py brief | fitness | forecast | metrics | trends
python3 coach.py review [<id>] [--json]
python3 coach.py week [--date YYYY-MM-DD] [--no-write] [--no-reconcile]
python3 coach.py plan --from-data [--days 4 --long-day sun --lift-days mon,thu --quality strides] [--ics]
python3 coach.py plan --entry 50km --weeks 12
python3 coach.py note "..." [--until YYYY-MM-DD]
python3 coach.py status [--json]
python3 coach.py ingest [--dry-run] [data/mcp/]      # Strava MCP payloads -> cache
python3 coach.py analyze [data/mcp/]                 # ingest, then the full report + scenarios
python3 coach.py scenario --entry 30,40,50
python3 coach.py reconcile | dashboard | sync
python3 -m unittest discover -s tests                # the whole suite; no network, no config.json needed
```

## Hard rules

- **Stdlib only. No new dependencies, ever.** PRs adding imports outside the
  standard library get declined.
- All athlete numbers live in `config.json` (gitignored), falling back to
  `config.example.json`. Paces in config are min/mi; `athlete.units` is
  display only and `units.py` converts at the print and parse edges. Never
  hardcode physiology, paces or theme values, and never print a bare `mi` /
  `/mi` / `mpw` label: use `units.fmt_dist`, `units.fmt_pace`,
  `units.volume_label`.
- State lives at `config.home()`: next to the code for a clone, under the
  plugin's data directory for the plugin, or `STRAVA_COACH_HOME`. Every data
  path comes from the `config.*_DIR` / `*_PATH` constants, never from
  `Path(__file__)`.
- `activities.csv` is the Strava bulk-export CSV parsed by **fixed column
  index**; never reorder columns in code that writes it.
- `enrichment.classify_activity()` / `is_real_run()` is the single source of
  truth for "is this a real run?" and `enrichment.classify_run()` the one
  run-kind classifier. Every run-metric consumer filters through them; a
  bump of `ENRICHMENT_VERSION` re-enriches the whole cache.
- Tests must run with no network, no `.env` and no `config.json`. Isolate
  with `tests/helpers.py` (`temp_config`, `temp_plan`, `temp_activity_data`);
  end-to-end tests run `coach.py` in a subprocess with `STRAVA_COACH_HOME`
  set and every `STRAVA_*` variable stripped.
- The coaching guardrails are in the code (CTL needs 60 days, `UNRELIABLE`
  below that; overreaching needs three consecutive days of TSB under -20;
  decoupling is pace-matched; HR is cost, pace is intensity). Do not talk
  around them; `docs/COACHING.md` says how to relay them.

## Module map

- Data: `strava_api` / `strava_sync` (REST + cache/CSV), `mcp_adapter`
  (Strava MCP JSON -> cache, merge by id, `fetch_plan`), `enrichment`
  (classification + TSS + run kind at ingest), `status`, `onboarding`
- Engine: `metrics` (loader chokepoint, streams, Eddington, PRs),
  `fitness_tracker` (two-stream CTL/ATL/TSB with history guards),
  `race_predictor` (VDOT, probability, advice), `analysis` (CSV weekly
  aggregates), `trends`, `intervals` (reps from streams), `session_intent`
  (description -> segments), `hr_quality` (wrist-HR checks), `units`
- Plan: `marathon_plan` (schema, state, notes), `plan_generator` (presets,
  `plan` block, CLI), `plan_layout` (7-day layout, `role_for_date`,
  `apply_notes`), `plan_tracker` (compliance, checkpoints, MP laps),
  `reconcile`, `scenario`, `ical_generator`
- Surface: `coach` (CLI), `dashboard`, `daily_brief`, `weekly_check`,
  `post_run_review` (pure `review()` + `render()`), `wizard`

Two-stream load model: running mileage (impact) is runs only; aerobic load
(CTL/ATL/TSB) also counts cross-training and lifting. The sports science and
its caveats live in `docs/METHODOLOGY.md`.

## Strava MCP ingest (the zero-OAuth path)

The procedures live in `.claude/skills/`: `strava-coach-analyze` (the entry:
`status --json` -> pull 90 days -> `ingest` -> one question -> `init
--from-mcp` -> report), `review-run`, `weekly-review`, `race-forecast`,
`build-plan`. Facts worth repeating: the inbox is `data/mcp/` (gitignored):
`list/*.json` pages saved verbatim, `perf/<id>.json`, `streams/<id>.json`,
`profile.json`, `zones.json`; a persisted `[{"type":"text",...}]` wrapper is
unwrapped; list summaries carry **no heart rate**; `coach.py status --json`
says what is still missing (`fetch.perf_needed`, `fetch.streams_needed`);
never paste raw activity JSON back to the user; never commit anything under
`data/`.
