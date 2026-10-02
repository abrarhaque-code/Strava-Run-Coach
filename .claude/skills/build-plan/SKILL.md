---
name: build-plan
description: Build or rebuild the training plan around the athlete's goal and situation - race, days per week, long-run day, lifting days, an injury or a return to running - with an optional subscribable .ics calendar. Use for "build me a plan", "I can only run 4 days", "my long run has to be Sunday", "no speedwork, my calf", "plan for my half", "put my workouts on my calendar".
---

# Build a training plan

Engine location and voice: see `strava-coach-analyze` step 0 and
`docs/COACHING.md`. The plan is generated for the active race in
`config.json` from the athlete's recent volume and the preferences below;
regenerating is cheap and the reconciled actuals re-derive on the next
weekly check-in.

## 1. The race

`python3 coach.py status --json` -> `race`. Wrong or missing: fix config
first, with the athlete's confirmation (it rewrites `config.json`, keeping a
backup):

```bash
python3 coach.py init --from-mcp --force --race-name "Spring Half" --race-date 2027-05-16 --distance 21.1km --goal-time 1:50:00
```

## 2. Their situation, in conversation

Ask only what they have not already said; defaults are fine.

| what | flag | default |
|---|---|---|
| days running per week (3-7) | `--days 4` | 5 (config `plan.days_per_week`) |
| long-run day | `--long-day sun` | sat |
| key-session day | `--quality-day tue` | wed (never the day before the long run) |
| lifting days | `--lift-days mon,thu` | none |
| days that must be off | `--rest-days fri` | none |
| injury / return to running | `--quality none` or `--quality strides` | full |
| cap on the long run | `--max-long 16` (their unit) | from the race preset |
| current volume | `--entry 40` / `--entry 50km` | `--from-data` (last 4 weeks, rounded up) |
| weeks | `--weeks 12` | the race-distance preset, capped at the weeks left |

## 3. Generate

```bash
python3 coach.py plan --from-data --days 4 --long-day sun --lift-days mon,thu
python3 coach.py plan --from-data --quality strides        # calf still settling
python3 coach.py plan --entry 50km --weeks 12 --ics        # + calendar feed
```

Writes `data/<race>.generated.json` and, with `--ics`, `plan_output/training.ics`
(one subscribable feed: runs, long runs, lift days; regenerating updates it).

## 4. Changes mid-block

- Temporary (an injury, a travel week): `python3 coach.py note "no speedwork,
  calf" --until 2026-10-20`. Key days turn easy while the note stands; the
  plan file is untouched.
- Lasting (fewer days, a new long-run day, a new race): regenerate with the
  new flags.

## 5. Relay

The number of weeks and the phases, peak volume and the long-run
progression in their unit, the shape of a week (which day is which), and the
checkpoints with their dates and what happens if one is missed. Not the JSON.

## Never

- Hand-write or hand-edit plan JSON; the generator writes it and the schema is
  validated on load.
- Promise outcomes; the checkpoints exist because fitness is earned, not
  scheduled.
