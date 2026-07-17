# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning: [SemVer](https://semver.org/); the Claude plugin version in
`.claude-plugin/plugin.json` moves in lockstep with release tags.

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

[1.0.0]: https://github.com/abrarhaque-code/Strava-Run-Coach/releases/tag/v1.0.0
