# strava-run-coach

A command-line running coach. It reads your Strava data and computes VDOT race
predictions, CTL/ATL/TSB fitness tracking, adaptive half and marathon plans, a
calendar export, and an HTML dashboard.

[![CI](https://github.com/abrarhaque-code/strava-run-coach/actions/workflows/ci.yml/badge.svg)](https://github.com/abrarhaque-code/strava-run-coach/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

![strava-run-coach dashboard](docs/img/01-full.png)

## Why

Most running analytics live behind a subscription or a cloud account, and your
training history lives there too. This runs on your own machine instead. It
reads your Strava export or API feed and turns it into the coaching numbers that
drive training decisions. It uses only the Python standard library, so the code
is small and none of your data leaves your computer.

## Features

- Jack Daniels VDOT race prediction with a goal-time probability estimate.
- CTL/ATL/TSB training-load balance (fitness, fatigue, and form) from
  exponentially weighted training stress.
- Eddington number for running, plus run streaks and best efforts at standard
  distances.
- Training polarization and acute:chronic ratio, so you can see whether your
  easy days are actually easy.
- Adaptive multi-week half and marathon plans with phase structure and
  decision points that evaluate against your current fitness.
- iCalendar (`.ics`) export so planned workouts land in your calendar.
- A single-file HTML dashboard that opens in a browser.

## Quick start

```bash
git clone https://github.com/abrarhaque-code/strava-run-coach.git
cd strava-run-coach
python3 setup.py
python3 coach.py
```

`setup.py` walks through a short config and can generate a sample training
history, so `coach.py` produces output on a fresh clone. There is nothing to
`pip install`; it needs only Python 3.10 or newer.

## Connect your Strava (optional)

The sample data lets you try everything immediately. To run on your own
training, connect the Strava API:

1. Create an API application at https://www.strava.com/settings/api and request
   the `activity:read_all` scope.
2. `cp .env.example .env`
3. Fill in `STRAVA_CLIENT_ID` and `STRAVA_CLIENT_SECRET` in `.env`.
4. `python3 strava_authorize.py` for the one-time OAuth handshake.
5. `python3 strava_sync.py` to pull your activities.

Your data stays on your machine. `.env`, `activities.csv`, and the Strava cache
are listed in `.gitignore`, so they are not committed.

## Configuration

All personalization lives in one file. Copy `config.example.json` to
`config.json` (or let `setup.py` do it) and edit:

- `athlete` - max HR, threshold HR, easy-run HR cap, threshold pace, units.
- `pace_zones` - your easy, tempo, threshold, and race-pace bands.
- `races` - a list of goal races. Each has an id, date, distance, goal time,
  and a plan reference. `active_race` defaults to `"auto"`, which picks the
  earliest upcoming race and rolls to the next one the day after each race. Set
  it to a race id to pin it.
- `theme` - the dashboard color palette and fonts.

`config.json` is gitignored, so your personal numbers never get committed. If it
is absent, the app falls back to `config.example.json` so a fresh clone still
runs.

![strava-run-coach dashboard detail](docs/img/end.png)

## Commands

`coach.py` is the single entry point. Sub-commands:

| Command | What it does |
| --- | --- |
| `python3 coach.py` | Full report: brief, fitness, metrics, forecast, last-run review, weekly status |
| `python3 coach.py brief` | Today's workout and a fatigue read |
| `python3 coach.py review` | Debrief of your latest run (laps, cardiac drift) |
| `python3 coach.py fitness` | CTL/ATL/TSB fitness, fatigue, and form |
| `python3 coach.py forecast` | VDOT race prediction and goal probability |
| `python3 coach.py metrics` | Eddington number, streaks, best efforts |
| `python3 coach.py week` | Weekly check-in and plan compliance |
| `python3 coach.py dashboard` | Render the static HTML dashboard |
| `python3 coach.py sync` | Sync from Strava, then run the full report |
| `python3 coach.py init` | First-run setup wizard (creates your config.json) |

Other useful scripts: `python3 setup.py` (first-run wizard),
`python3 scripts/generate_sample_data.py` (sample history),
`python3 dashboard.py` (dashboard only), `python3 planner.py` (generate a
short-race plan), `python3 ical_generator.py` (calendar export).

## How it works

`activities.csv` is the data interface for the whole system. It is the Strava
bulk-export CSV, parsed by fixed column index, and the API sync writes the same
shape so every analysis module keeps working whether the data came from an
export or the API. Richer per-activity detail (laps, best efforts) lives in a
local JSON cache. `config.py` centralizes all athlete-specific numbers so the
rest of the code stays generic. For the full module map and design notes, see
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Tests

```bash
python3 -m unittest discover -s tests
```

The suite is standard-library `unittest`, runs with no `.env` and no network,
and is exercised in CI across Python 3.10 through 3.13.

## License

MIT. See [LICENSE](LICENSE).

---

Suggested GitHub topics for this repo: `running`, `strava`, `marathon`,
`half-marathon`, `marathon-training`, `training-plan`, `vdot`, `tss`, `python`,
`cli`, `sports-analytics`.
