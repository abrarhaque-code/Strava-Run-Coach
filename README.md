# strava-run-coach

A command-line running coach. It reads your Strava data and computes VDOT race
predictions, CTL/ATL/TSB fitness tracking, a graded debrief of every run, a
training plan built around your race and your week, a weekly check-in,
long-horizon trends, a calendar export, and an HTML dashboard. Built to be
driven by a human at a terminal **or by Claude over the Strava MCP**: say
"coach me" and it sets itself up from your Strava profile, asks one question
(your goal time), and coaches from there.

[![CI](https://github.com/abrarhaque-code/Strava-Run-Coach/actions/workflows/ci.yml/badge.svg)](https://github.com/abrarhaque-code/Strava-Run-Coach/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

![strava-run-coach dashboard](docs/img/01-full.png)

## Why

Most running analytics live behind a subscription or a cloud account, and your
training history lives there too. This runs on your own machine instead. It
reads your Strava export or API feed and turns it into the coaching numbers
that drive training decisions. It uses only the Python standard library, so
the code is small, auditable, and none of your data leaves your computer.
Every model is named and cited — see [docs/METHODOLOGY.md](docs/METHODOLOGY.md),
including the honest caveats (the 10%-rule RCT, the ACWR controversy).

## Features

- Jack Daniels VDOT race prediction with a goal-time probability estimate,
  anchored on your best recent efforts and real race results.
- CTL/ATL/TSB training-load balance (fitness, fatigue, and form) with a
  two-stream model: running mileage and aerobic load are tracked separately,
  so bike cross-training builds your engine without inflating run volume.
- Activity classification at ingest: bike sessions logged as "Run" entries,
  zero-distance rows, and deleted activities never pollute mileage or VDOT.
- Plan reconciliation: actual-vs-planned lands in `data/plan_state.json`
  after every sync, with frozen history and manual-override semantics.
- Long-horizon trends: cardiac drift, pace-at-same-HR efficiency,
  consistency gaps, recovery patterns, elevation cost.
- Eddington number, run streaks, and best efforts at standard distances.
- A debrief of every run on the five things that matter: easy discipline,
  workout execution (reps, tempo, marathon-pace blocks), how the long run
  held (pace-matched decoupling, stops, the finish), training load, and a
  grade. Judged against your own Strava description first. A flag when the
  wrist heart-rate trace looks unreliable.
- Plans built for your race from your recent volume and your week: days per
  week, long-run day, lifting days, an injury or a return to running. A
  standing note ("no speedwork until the calf settles") turns key days easy
  while it stands. Base-build scenarios from any entry volume.
- Everything prints in your units, miles or kilometres.
- iCalendar (`.ics`) export so planned workouts land in your calendar.
- A single-file HTML dashboard skinned with a Klein-blue design system.
- Works with Claude out of the box: Strava MCP ingestion, zero-question
  setup from your Strava profile and zones, five shipped skills, and an
  installable Claude Code plugin.

## Quick start

```bash
git clone https://github.com/abrarhaque-code/Strava-Run-Coach.git
cd Strava-Run-Coach
python3 coach.py init --sample   # config defaults + generated sample history
python3 coach.py                 # full report
```

There is nothing to `pip install`; it needs only Python 3.10 or newer. The
sample data is deterministic and anchored to today, so the report, dashboard,
countdowns, and heatmap all populate on a fresh clone. Run
`python3 coach.py init` (no flag) for the interactive wizard instead, or skip
all of this and say "coach me" to Claude with the Strava MCP connected (see
[Use with Claude](#use-with-claude)).

## What it looks like

`python3 coach.py forecast`:

```
============================================================
  RACE FORECAST  |  City Marathon, Nov 01 2026
============================================================

Days to race: 108
Goal: 3:45:00 (8:35/mi)

Current fitness VDOT: 37.5
  Source: best 10K effort on 2026-07-14 @ 8:30/mi [Threshold Run]

Goal VDOT (3:45:00): 41.0
Gap: -3.5 VDOT

Predicted finish time: 4:01:59 (9:14/mi)

Goal (3:45:00) probability: 2%  [LOW - fitness gap]

What you need to do:
- Hit at least one quality run with HR >= 165 in the next 10 days
- Long run progression: aim for 10.4 mi+ this weekend
- Maintain consistency: 5+ runs/week through race week
```

`python3 coach.py review` (a run described on Strava as "3 easy, 4 tempo, 2 easy"):

```
======================================================================
  POST-RUN REVIEW  |  2026-09-29 (Tue)
======================================================================

  Tuesday tempo
  9.00 mi  |  1:26:20 moving / 1:26:47 elapsed  |  9:36/mi  |  HR avg 149, max 166

  Classification:    TEMPO  (from intent)
  Execution score:   A (100/100)
  Data:              laps yes, stream no
  You said:          3 easy, 4 tempo, 2 easy  (from the description)

----------------------------------------------------------------------
  LAP BREAKDOWN
----------------------------------------------------------------------
  Lap 1:  1.00 mi @  10:00/mi | HR 140
  Lap 2:  1.00 mi @  10:00/mi | HR 140
  Lap 3:  1.00 mi @  10:00/mi | HR 140
  Lap 4:  1.00 mi @   9:00/mi | HR 158 [tempo]
  Lap 5:  1.00 mi @   9:00/mi | HR 158 [tempo]
  Lap 6:  1.00 mi @   9:00/mi | HR 158 [tempo]
  Lap 7:  1.00 mi @   9:00/mi | HR 158 [tempo]
  Lap 8:  1.00 mi @  10:10/mi | HR 145
  Lap 9:  1.00 mi @  10:10/mi | HR 145

  Pattern: declared tempo at splits #4-7 (see VERDICTS); split pattern not read
  Pace-matched decoupling: +5 bpm median over 9 pair(s), worst +5 bpm
    (split #1 -> #8). MILD: normal for a long run in heat.

----------------------------------------------------------------------
  VERDICTS
----------------------------------------------------------------------
  [+] Intent: Ran 9.0 mi as you described it: 3 easy, 4 tempo, 2 easy.
  [+] Easy: 4 of 4 easy splits under the easy cap 148 (HR 140-145,
      10:00-10:10/mi); time in zones n/a without a stream.
  [+] Quality: tempo splits #4-7: 9:00/mi, HR 158, inside the tempo
      band (8:50-9:10/mi).
  [+] Finish: After the declared work you ran 10:10/mi vs 10:00/mi in
      the opening splits (+10 s): held. Pace-matched HR: +5 bpm late
      vs early (mild).
  [ ] Load: No training load computed for this run.
  [ ] Sensor: No stream: wrist-HR checks not run.
  [i] Stops: Continuous: 99% moving.
```

`python3 coach.py fitness`:

```
  CTL (fitness, 42d):   43.6
  ATL (fatigue, 7d):    55.3
  TSB (form):          -10.1

  Phase: BUILDING
  -> Productive overload. Watch for cumulative fatigue.

  CTL trend (last 90 days):

   44                                                                     *
                                                                    *** ** @
                                                             * * ***   *
                                                  *********** * *
                                    *  * *** *****
                                  ** ** *   *
                         *********
                    *****
              ** ***
   12 ********  *
```

`python3 coach.py scenario --entry 20,25,30` (or `--entry 30,40,50km`):

```
   Entry   Peak   Avg  Long     Marathon range   Reach base?
     mpw    mpw   mpw   run        (projected)        by Jul
  ----------------------------------------------------------------
      20     38    27    16    4:43:13-5:01:16   comfortable
      25     48    33    20    4:35:53-4:45:49   comfortable
      30     57    40    22    4:31:46-4:43:22        unsafe
```

![strava-run-coach dashboard detail](docs/img/end.png)

## Use with Claude

The repo is agent-native: five skills, the official Strava MCP, and one
conversation from "coach me" to a plan.

1. **Connect Strava.** claude.ai / Cowork: Settings → Connectors → **Strava**.
   Claude Code: the checked-in `.mcp.json` offers `https://mcp.strava.com/mcp`
   on first use (OAuth, no keys to manage).
2. **Get the engine.** Clone this repo, or install the plugin (no clone needed):

   ```
   /plugin marketplace add abrarhaque-code/Strava-Run-Coach
   /plugin install strava-run-coach@strava-run-coach
   ```

3. **Say "coach me".** The `strava-coach-analyze` skill pulls your last 90 days
   (the activity list, then heart rate and laps for recent runs, streams for
   long runs and workouts), sets the coach up from your Strava profile and
   zones, asks you ONE question (your goal time, reading back the race Strava
   says you are training for), builds a plan for it, and gives you the report.

From then on: "how was my run" (`review-run`), "how did my week go"
(`weekly-review`), "can I run 3:45" (`race-forecast`), "I can only run four
days" or "no speedwork until my calf settles" (`build-plan`).
[docs/COACHING.md](docs/COACHING.md) is how it talks: honest, specific, on
your side, and careful never to escalate beyond what the data supports (no
"overreaching" off a two-week pull, no "cardiac drift" on a progression run).

As a plugin the engine runs from the plugin directory and keeps your data under
`~/.claude/plugins/data/strava-run-coach/`; `STRAVA_COACH_HOME` points any
install at a directory of your choice. Your data never leaves your machine:
`data/`, `config.json` and `plan_output/` are gitignored. (If you both clone
and install the plugin, the skills appear twice; harmless.)

No Strava subscription? Everything still works from the sample data, the
Strava bulk-export CSV, or the free API sync below.

## Without the MCP: the Strava API or a bulk export

The sample data lets you try everything immediately. Without Claude and the
MCP, connect the Strava API:

1. Create an API application at https://www.strava.com/settings/api and request
   the `activity:read_all` scope.
2. `cp .env.example .env`
3. Fill in `STRAVA_CLIENT_ID` and `STRAVA_CLIENT_SECRET` in `.env`.
4. `python3 strava_authorize.py` for the one-time OAuth handshake.
5. `python3 strava_sync.py --backfill 90` to pull your activities (a shorter
   first pull is raised to 90 days: fitness is a 42-day average).

Headless environments (CI, cloud sessions) can export `STRAVA_*` environment
variables instead of using a `.env` file. Your data stays on your machine.
`.env`, `activities.csv`, and the Strava cache are gitignored.

## Configuration

All personalization lives in one file. Copy `config.example.json` to
`config.json` (or let `coach.py init` do it) and edit:

- `athlete` - max HR, threshold HR, easy-run HR cap, threshold pace, units.
- `pace_zones` - your easy, tempo, threshold, and race-pace bands.
- `races` - a list of goal races. `active_race` defaults to `"auto"`, which
  picks the earliest upcoming race and rolls over the day after each race.
- `race_history` - completed race results; a real race is the strongest
  fitness anchor the predictor can use.
- `plan` - how `coach.py plan` lays out your week: days per week, long-run
  day, key-session day, rest and lifting days, quality level (`none` /
  `strides` / `tempo` / `full`), a long-run cap. Flags override it per run.
- `crosstrain` / `strength` - how bike work and lifting credit aerobic load.
- `trends`, `scenario`, `report` - analysis thresholds and toggles.
- `theme` - the dashboard palette and fonts (ships with the Yves Klein Blue
  design-system tokens).

Have the Strava MCP? `python3 coach.py init --from-mcp data/mcp/ --goal-time 3:45:00`
writes the whole file from your Strava profile (name, units, the race you are
training for), your zones (HR caps and pace bands) and your history (observed
max HR, your real easy pace, trailing volume), listing every derivation and
anything it still needs. `--dry-run` shows it first; `--force` replaces an
existing file after a backup. Paces in the file are always min/mi internally;
`athlete.units` decides what you see. `config.json` is gitignored; if it is
absent, the app falls back to `config.example.json` so a fresh clone still runs.

## Commands

`coach.py` is the single entry point and exits with each module's code.

| Command | What it does |
| --- | --- |
| `python3 coach.py` | Full report: brief, fitness, forecast, latest run review, weekly check-in |
| `python3 coach.py brief` | Today's session from the plan (standing notes applied) and a fatigue read |
| `python3 coach.py review [<id>] [--json]` | Debrief one run: easy discipline, workout execution, the finish, load, a grade, a wrist-HR flag |
| `python3 coach.py fitness` | CTL/ATL/TSB fitness, fatigue and form; warns under 60 days of history |
| `python3 coach.py forecast` | VDOT race prediction, goal probability, what to do about the gap |
| `python3 coach.py metrics` | Eddington number (in your unit), streaks, best efforts |
| `python3 coach.py trends` | Long-horizon lenses: efficiency, consistency, recovery, elevation cost |
| `python3 coach.py week [--date D]` | Weekly check-in: last week vs plan, the 7-day layout, run grades, the next checkpoint, fitness; writes `plan_output/weekly/` |
| `python3 coach.py scenario --entry 30,40,50` | Base-build scenarios: entry volume → peak → marathon range (`50km` works) |
| `python3 coach.py plan [--from-data\|--entry N] [--days 4 --long-day sun --lift-days mon,thu --quality strides] [--ics]` | Generate the plan for the active race around your week (+ calendar feed) |
| `python3 coach.py note "..." [--until D]` | Adjustment note; with `--until` it stands and turns key days easy |
| `python3 coach.py status [--json]` | What the coach has, what the MCP still needs to fetch, the next step |
| `python3 coach.py ingest [data/mcp/]` | Merge Strava MCP payloads (list pages, performance, streams) into the cache |
| `python3 coach.py analyze [data/mcp/]` | Ingest, then the full report and scenarios |
| `python3 coach.py reconcile` | Record actual-vs-planned into `plan_state.json` |
| `python3 coach.py dashboard` | Render the static HTML dashboard |
| `python3 coach.py sync` | Strava API sync (a cold backfill is floored at 90 days), reconcile, full report |
| `python3 coach.py init [--sample \| --from-mcp [data/mcp/] [--goal-time H:MM:SS] \| --from-mcp-zones f.json]` | Setup: the wizard, the demo bootstrap, or a config written from your Strava profile, zones and history |

## How it works

`activities.csv` is the data interface for the whole system: the Strava
bulk-export CSV, parsed by fixed column index; the API sync and the MCP
adapter write the same shape. Richer per-activity detail (laps, best efforts,
heart rate) lives in a local JSON cache. `enrichment.classify_activity()` is
the single source of truth for "is this a real run?" — every consumer filters
through it. `config.py` centralizes all athlete-specific numbers so the rest
of the code stays generic, and decides where state lives (`config.home()`:
next to the code for a clone, under `~/.claude/plugins/data/strava-run-coach/`
for the plugin, or `STRAVA_COACH_HOME`). `units.py` converts at the edges, so
the engine stays in miles and metres while you read km. For the module map see
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md); for the sports science and its
limits see [docs/METHODOLOGY.md](docs/METHODOLOGY.md).

## Tests

```bash
python3 -m unittest discover -s tests
```

400+ standard-library `unittest` tests; the suite runs with no `.env`, no
`config.json`, and no network, and CI exercises it across Python 3.10-3.13
with zero install steps.

## License

MIT. See [LICENSE](LICENSE).
