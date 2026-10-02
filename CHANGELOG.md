# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning: [SemVer](https://semver.org/); the Claude plugin version in
`.claude-plugin/plugin.json` moves in lockstep with release tags.

## [1.1.0] - 2026-10-01

The "one conversation" release: say "coach me" to Claude with the Strava MCP
connected and the coach sets itself up, asks one question, and coaches.

### Added
- **`coach.py init --from-mcp`** (`onboarding.py`): config.json from your
  Strava profile (name, units, the race you are training for), zones (HR caps,
  pace bands), and history (observed max HR, your real easy pace, trailing
  volume). Every derivation noted; anything missing listed under NEEDS. Never
  overwrites without `--force` (and a backup). `--dry-run`.
- **`coach.py status [--json]`** (`status.py`): what is cached, what the MCP
  still needs to fetch (performance for recent runs without HR, streams for
  long runs and workouts), the inbox, the plan, and the next step in order.
- **Graded post-run review** (`post_run_review.py` rewrite): a pure `review()`
  dict and `render()`. Intent from your Strava description first ("13 easy,
  3 at half pace, 2 easy"), then the plan week. Easy discipline by lap HR
  under the cap; declared segments scored by pace band AND HR band; marathon-
  pace blocks by laps at pace inside the HR band; reps reconstructed from the
  stream (`intervals.py`); pace-matched decoupling (HR at the same pace, early
  vs late); stops; surge-then-fade vs a deliberate finish; load as TSS and
  form; a wrist-HR reliability flag (`hr_quality.py`). Grade A-D. 1 km
  auto-laps count as full splits. `coach.py review [<id>] [--json]`.
- **Plans built around the athlete** (`plan_generator.py` rewrite,
  `plan_layout.py`): race-distance presets (marathon, half, 10k, 5k), a `plan`
  config block and flags for days per week, long-run day, key-session day,
  rest and lifting days, quality level (`none|strides|tempo|full`), a long-run
  cap; week-level `days[]` with roles, distances, paces and HR caps; `--entry
  40km`; `--from-data` from the last four weeks. Decision points with
  configurable minimum runs; `data/<race>.generated.json`. A short block keeps
  its taper (a marathon five weeks out still tapers for three), scales the
  peak to the build weeks actually available, and grows the long run from the
  athlete's recent longest rather than inventing one.
- **Weekly check-in rewrite** (`weekly_check.py`): last week vs the plan with
  "adjust and move forward" wording, the 7-day layout with standing notes
  applied, the last seven days of runs with their review grades, the next
  checkpoint read live, fitness with a short-history warning. Writes
  `plan_output/weekly/YYYY-Wnn.md`; useful lines with no plan at all.
- **Daily brief rewrite**: today's session from the layout, fatigue thresholds
  from the week's targets and the config caps, standing notes.
- **Standing notes**: `coach.py note "..." --until YYYY-MM-DD` keeps a note in
  force; one that rules out speedwork turns key days easy while it stands.
- **MCP ingest v3** (`mcp_adapter.py`): `coach.py ingest [data/mcp/]` merges
  list pages, `perf/<id>.json` and `streams/<id>.json` by id; unwraps
  persisted tool results; preserves HR/laps on a summary-only re-import;
  streams cached raw for the review. `fetch_plan()` decides which calls are
  still worth making.
- **Units** (`units.py`): every distance, pace and volume prints in
  `athlete.units`; the Eddington number counts in it; `--entry 50km`.
- **State home** (`config.home()`): next to the code for a clone, under
  `~/.claude/plugins/data/strava-run-coach/` for the plugin, or
  `STRAVA_COACH_HOME`.
- **Fitness guards**: CTL needs 60 days of history (`UNRELIABLE` phase and a
  data warning below that); "overreaching" needs three consecutive days of
  TSB under -20; `tsb_context()` for the review's load line.
- **One run classifier** (`enrichment.classify_run`, enrichment v4): stated
  intent, then the rep-session shape, then distance, then HR/pace vs config
  zones. Rep sessions are excluded from the whole-run VDOT scan.
- **`docs/COACHING.md`**: how the coach talks, athlete-agnostic. A fifth skill,
  `review-run`; the other four rewritten around the one-conversation flow and
  `status --json`.
- `strava_api`: a rejected refresh token now says Strava rotates it and names
  the fix; stream requests include `moving` and `watts`. `strava_sync`: a cold
  backfill under 90 days is raised to 90.

### Changed
- `planner.py` removed; `plan_generator.py` covers every race distance. The
  plan template moved to `docs/examples/plan.example.json`.
- `reconcile` flags `data_stale` only when a pass loaded nothing at all; a
  finalized week outside a windowed pull is skipped silently.
- The forecast's advice reads the athlete's zones and plan week instead of
  one runner's paces. `coach.py` exits with each module's code.
- README rewritten for runners (a conversation up top, the technical material
  collapsed at the bottom); the terminal and Strava API material moved to
  `docs/TERMINAL.md`. The plugin manifest now carries the Strava MCP server, so
  a marketplace install brings the connector along.

### Removed
- First-half-vs-second-half "cardiac drift" in the review (it measured how a
  run was designed, not decoupling) and the half-marathon projection line.

### Fixed
- `coach.py review <id>` dropped its arguments.
- The weekly check-in ran a hardcoded six-week half plan instead of the
  generated one.
- The MCP adapter counted persisted tool-result files as zero activities,
  overwrote laps and HR on re-ingest, and never mapped `is_trainer`.

## [1.0.0] - 2026-07-17

The "agent-native coach" release.

### Added
- **Activity classification at ingest** (`enrichment.classify_activity` /
  `is_real_run`): bike sessions manually logged as Run entries (detected by
  a config-derived speed signature), zero-distance rows, and soft-deleted
  activities never count toward mileage, VDOT, or run TSS. TSS routing is
  class-aware; REST-synced rides now feed the aerobic-load stream, with
  same-day double-logs deduplicated.
- **Plan reconciliation loop** (`reconcile.py`, `coach.py reconcile|note`):
  actual-vs-planned lands in `data/plan_state.json` after every sync, with
  frozen terminal history, coverage guards, and manual-override semantics.
  Lap-verified marathon-pace segment metric for plan decision gates.
- **Strava MCP adapter v2**: paginated multi-file ingest,
  `get_activity_performance` merge (HR flips TSS from pace-based to
  HR-based; laps light up run reviews), CSV-seam rebuild, and
  `coach.py init --from-mcp-zones` zone calibration.
- **`race_history` config key**: real race results anchor VDOT for 180 days.
- **`trends.py`** (`coach.py trends`): cardiac-drift history, pace-at-same-HR
  efficiency, consistency gaps, recovery patterns, elevation cost,
  treadmill-vs-outdoor, effort efficiency.
- **Claude integration**: four skills under `.claude/skills/`, a checked-in
  `.mcp.json` for the official Strava MCP, and an installable Claude Code
  plugin (`/plugin marketplace add abrarhaque-code/Strava-Run-Coach`).
- **`coach.py init --sample`**: one-command non-interactive demo bootstrap.
- **`docs/METHODOLOGY.md`**: every model named and cited, caveats included.
  `llms.txt` map for agents. Issue/PR templates, SECURITY.md.
- Headless Strava credentials via `STRAVA_*` environment variables.

### Changed
- Dashboard reskinned to the Yves Klein Blue design-system tokens
  (IKB `#1D1DE6`, warm paper, flame accent, Archivo display); all ink
  shades now flow through the theme (three new optional keys with
  defaults — existing configs keep working).
- `setup.py` renamed to `wizard.py` (`coach.py init` unchanged) so
  `pip install .` can no longer execute the wizard by accident.
- README rewritten: sample terminal output, accurate command table,
  "Use with Claude" section.
- The motivational report footer is now opt-in (`report.motivational_footer`).

### Fixed
- `fitness_tracker.load_runs` no longer counts soft-deleted activities.
- 3-digit race countdowns no longer overflow the dashboard panel.
- Sample-data runs carry `max_speed`, so a 10:00/mi easy run can't be
  mistaken for a manual bike-equivalence entry.

[1.1.0]: https://github.com/abrarhaque-code/Strava-Run-Coach/releases/tag/v1.1.0
[1.0.0]: https://github.com/abrarhaque-code/Strava-Run-Coach/releases/tag/v1.0.0
