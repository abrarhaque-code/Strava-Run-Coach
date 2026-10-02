# Run the coach yourself: the terminal and the Strava API

Everything the Claude skills do, you can do from a terminal. This page is for
that: trying the coach on a demo athlete, connecting your own Strava without
Claude, and what each command does. The runner-facing tour is the
[README](../README.md); every flag is listed in [CLAUDE.md](../CLAUDE.md).

## What you need

Python 3.10 or newer, and nothing else. Check with `python3 --version`. Macs
offer to install it the first time something asks for it; Windows gets it from
[python.org](https://www.python.org/downloads/). There is no `pip install`:
the whole coach is the Python standard library.

## Try it on a demo athlete

```bash
git clone https://github.com/abrarhaque-code/Strava-Run-Coach.git
cd Strava-Run-Coach
python3 coach.py init --sample   # a config and three months of made-up running, dated to today
python3 coach.py                 # the full report
```

The demo athlete has a marathon on the calendar, a generated plan, and enough
history for every section to show something.

### What the output looks like

`python3 coach.py review`, on a run described on Strava as "3 easy, 4 tempo, 2 easy":

```
  Classification:    TEMPO  (from intent)
  Execution score:   A (100/100)
  Data:              laps yes, stream no
  You said:          3 easy, 4 tempo, 2 easy  (from the description)

  Lap 3:  1.00 mi @  10:00/mi | HR 140
  Lap 4:  1.00 mi @   9:00/mi | HR 158 [tempo]
  Lap 5:  1.00 mi @   9:00/mi | HR 158 [tempo]
  ...
  [+] Intent: Ran 9.0 mi as you described it: 3 easy, 4 tempo, 2 easy.
  [+] Easy: 4 of 4 easy splits under the easy cap 148 (HR 140-145,
      10:00-10:10/mi); time in zones n/a without a stream.
  [+] Quality: tempo splits #4-7: 9:00/mi, HR 158, inside the tempo
      band (8:50-9:10/mi).
  [+] Finish: After the declared work you ran 10:10/mi vs 10:00/mi in
      the opening splits (+10 s): held.
```

`python3 coach.py week`, the part that lays the week out:

```
  Target: 30.0 mi  |  Long run: 12.6 mi

  7-DAY LAYOUT:
    Mon Sep 28:  Easy 4.5 mi @ 10:00-10:30/mi, HR < 148
    Tue Sep 29:  Easy 3.9 mi + 4 x 15 s strides @ 10:00-10:30/mi, HR < 148
    Wed Sep 30:  Rest
    Thu Oct 01:  Easy 4.5 mi @ 10:00-10:30/mi, HR < 148
  > Fri Oct 02:  Rest
    Sat Oct 03:  Long run 12.6 mi @ 10:00-10:30/mi, HR < 145
    Sun Oct 04:  Easy 4.5 mi @ 10:00-10:30/mi, HR < 148

  Standing notes (they shape the layout above):
    - no speedwork until the calf settles (until 2026-10-20)
```

`python3 coach.py forecast`:

```
Current fitness VDOT: 37.5
  Source: best 10K effort on 2026-09-29 @ 8:30/mi [Threshold Run]
Goal VDOT (3:45:00): 41.0
Predicted finish time: 4:01:59 (9:14/mi)
Goal (3:45:00) probability: 2%  [LOW - fitness gap]

What you need to do:
- Hit at least one quality run with HR >= 165 in the next 10 days
- Long run progression: aim for 10.4 mi+ this weekend
- Maintain consistency: 5+ runs/week through race week
```

## Connect your own Strava

Three ways in. Pick one.

**The Strava connector through Claude** is the no-setup path: the
[README](../README.md#get-started) covers it. Claude pulls the data through
Strava's connector and hands it to the coach; no keys, no `.env`.

**The Strava API, from the terminal.** One-time setup, then a one-line sync:

1. Create an API application at https://www.strava.com/settings/api and ask
   for the `activity:read_all` scope.
2. `cp .env.example .env` and fill in `STRAVA_CLIENT_ID` and `STRAVA_CLIENT_SECRET`.
3. `python3 strava_authorize.py` once (it opens Strava to sign you in and stores the tokens in `.env`).
4. `python3 strava_sync.py --backfill 90` to pull your activities. Ninety days,
   not fewer: fitness is a 42-day average and a shorter first pull reads as a
   false "overreaching", so a smaller number is raised to 90 on an empty cache.
   After that, `python3 coach.py sync` keeps it topped up.

Headless machines can export `STRAVA_CLIENT_ID`, `STRAVA_CLIENT_SECRET`,
`STRAVA_ACCESS_TOKEN` and `STRAVA_REFRESH_TOKEN` as environment variables
instead of a `.env` file. If a sync says Strava rejected the refresh token: Strava
rotates it on every refresh, so a token another copy used is dead; run
`strava_authorize.py` again.

**Strava's bulk export.** Request your archive from Strava (Settings → My
Account → Download or Delete Your Account → Get Started), drop its
`activities.csv` next to `coach.py`, and run `python3 coach.py`. No heart rate per
lap this way, so run reviews are shallower.

## Set yourself up

- `python3 coach.py init` walks through the questions (name, units, max heart
  rate, your easy-run heart-rate cap, your race and goal) and writes `config.json`.
- With a Strava connector inbox present (`data/mcp/profile.json` and
  `zones.json`, which the Claude skills save), `python3 coach.py init --from-mcp
  --goal-time 3:45:00` writes the same file from your Strava profile, zones and
  history, and lists every derivation; `--dry-run` shows it without writing.
- `config.json` is yours to edit. The `plan` block is how `coach.py plan` lays
  out your week (days per week, long-run day, lifting days, quality level, a
  long-run cap); `race_history` holds real race results, the strongest anchor the
  predictor has. Paces in the file are always minutes per mile; `athlete.units`
  only changes what you see.

## The commands, one by one

| Command | What it does |
| --- | --- |
| `python3 coach.py` | The full report: today, fitness, forecast, the latest run's review, the week |
| `python3 coach.py brief` | Today's session from the plan, with any standing note applied, and how fresh you are |
| `python3 coach.py review [<id>]` | Debrief one run (the latest by default): easy discipline, the work you declared, the finish, load, a grade, and a flag when the wrist heart rate looks unreliable. `--json` for the data |
| `python3 coach.py week` | The weekly check-in: last week vs plan, this week laid out, run grades, the next checkpoint, fitness. Writes `plan_output/weekly/` |
| `python3 coach.py forecast` | Predicted finish time, goal probability, what would move it |
| `python3 coach.py plan --from-data` | Build the plan for your race from your recent volume. `--days 4 --long-day sun --lift-days mon,thu --quality strides`, or `--entry 50km` for a volume of your choosing; `--ics` adds a calendar feed |
| `python3 coach.py note "..." --until 2026-10-20` | A note that stands until a date; one that rules out speedwork turns key days easy |
| `python3 coach.py scenario --entry 30,40,50` | What different weekly volumes would buy you |
| `python3 coach.py fitness` | Fitness, fatigue and form over the last 90 days |
| `python3 coach.py metrics` / `trends` | Streaks, best efforts, the Eddington number; long-horizon lenses |
| `python3 coach.py status` | What is cached, what is still missing, the next step |
| `python3 coach.py dashboard` | Render the HTML dashboard |

Every flag: [CLAUDE.md](../CLAUDE.md).

## Where your files live

Everything the coach writes goes under one folder: the project folder for a
clone, `~/.claude/plugins/data/strava-run-coach/` when it runs as the Claude Code
plugin, or wherever `STRAVA_COACH_HOME` points. Inside it: `config.json`,
`activities.csv`, `data/strava_cache/` (your activities and streams), `data/mcp/`
(what the Claude skills saved), `data/plan_state.json`, `data/<race>.generated.json`
(your plan) and `plan_output/` (the weekly reports, the brief, the dashboard, the
calendar feed). All of it is gitignored; nothing is uploaded anywhere.

## When something looks off

- **Fitness says `UNRELIABLE`.** Fewer than 60 days of history are loaded. Pull
  more before reading anything into the numbers.
- **The review says "laps no" or "stream no".** The run came in without lap or
  per-second detail. Through Claude, the `review-run` skill fetches it; through
  the API, run `coach.py sync` again.
- **A run you consider a long run is not graded as one.** `trends.long_run_min_mi`
  in `config.json` is the threshold; the Strava setup sets it at a quarter of
  your weekly volume.
- **A leaked token.** See [SECURITY.md](../SECURITY.md).

The sports science and its caveats: [docs/METHODOLOGY.md](METHODOLOGY.md). How
the pieces fit: [docs/ARCHITECTURE.md](ARCHITECTURE.md).
