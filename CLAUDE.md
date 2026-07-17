# strava-run-coach: working notes for Claude

A stdlib-only Python running coach: Strava history in -> VDOT race
prediction, CTL/ATL/TSB fitness tracking, adaptive plans, base-build
scenarios, long-horizon trends, an .ics calendar feed, and an HTML
dashboard. `coach.py` is the single entry point.

## Commands

```bash
python3 coach.py init --sample   # non-interactive demo bootstrap (config + sample data)
python3 coach.py                 # full report
python3 coach.py forecast | fitness | metrics | trends | week | review | brief
python3 coach.py scenario --entry 20,25,30
python3 coach.py plan --entry 25 --weeks 16 [--ics]
python3 coach.py analyze --from-mcp <file.json>... [--performance <file-or-dir>]
python3 coach.py reconcile       # record actual-vs-planned into plan_state.json
python3 coach.py note "..."      # timestamped adjustment note for this week
python3 coach.py dashboard | sync
python3 -m unittest discover -s tests   # the whole suite; no network, no config.json needed
```

## Hard rules

- **Stdlib only. No new dependencies, ever.** PRs adding imports outside the
  standard library get declined.
- All athlete numbers live in `config.json` (gitignored), falling back to
  `config.example.json` — never hardcode physiology, paces, or theme values.
- `activities.csv` is the Strava bulk-export CSV parsed by **fixed column
  index**; never reorder columns in code that writes it.
- `enrichment.classify_activity()` / `is_real_run()` is the single source of
  truth for "is this a real run?" (bike-as-run entries, zero-distance rows,
  and soft-deleted activities never count). Every run-metric consumer filters
  through it; new consumers must too.
- Tests must run with no network and no `.env`.

## Module map

- Data: `strava_api/strava_sync` (REST + cache/CSV), `mcp_adapter` (Strava
  MCP JSON -> cache), `enrichment` (classification + TSS at ingest)
- Engine: `metrics` (loader chokepoint, Eddington, PRs), `fitness_tracker`
  (two-stream CTL/ATL/TSB), `race_predictor` (VDOT + goal probability),
  `analysis` (weekly aggregates), `trends` (long-horizon lenses)
- Plan: `marathon_plan` (schema + state), `plan_generator`, `planner` (half),
  `plan_tracker` (compliance + decision gates), `reconcile` (actuals ->
  plan_state), `scenario` (entry-mileage projections), `ical_generator`
- Surface: `coach` (CLI), `dashboard` (self-contained HTML), `daily_brief`,
  `weekly_check`, `post_run_review`, `wizard` (setup)

Two-stream load model: running mileage (impact) is runs only; aerobic load
(CTL/ATL/TSB) also counts cross-training and lifting. The sports science and
its honest caveats live in `docs/METHODOLOGY.md`.

## Strava MCP ingest (the zero-OAuth path)

The canonical, step-by-step procedures live in `.claude/skills/` —
`strava-coach-analyze` (pull + paginate + ingest), `weekly-review`,
`race-forecast`, `build-plan`. Facts worth repeating: save MCP pages
verbatim as `{"activities": [...]}` to `data/mcp_activities.json`; list
summaries carry **no heart rate** (merge `get_activity_performance` payloads
via `--performance` for HR-based TSS and laps); never paste raw activity
JSON back to the user; never commit anything under `data/`.
