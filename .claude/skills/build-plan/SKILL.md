---
name: build-plan
description: Generate a periodized marathon/half training plan from entry mileage, optionally exported as a subscribable .ics calendar. Use when the user asks to "build me a training plan", "16-week marathon plan", "plan for my race", or "put my workouts on my calendar".
---

# Build a training plan

Run from the strava-run-coach repo root (see the `strava-coach-analyze`
skill, step 0, if there is no checkout).

## 1. Confirm the target race

The plan generator plans for the active race in `config.json` -> `races[]`.
Check it matches what the user wants (`python3 config.py` prints the active
race). If their race isn't configured, help them edit `config.json` first —
with their confirmation, never silently.

## 2. Pick entry mileage

If the user didn't state a current weekly mileage, either ask, or run
`python3 coach.py scenario` on their data and use its feasibility read to
recommend one.

## 3. Generate

```bash
python3 coach.py plan --entry 25 --weeks 16          # plan only
python3 coach.py plan --entry 25 --weeks 16 --ics    # + calendar feed
```

This writes `data/marathon_plan.generated.json` and, with `--ics`,
`plan_output/training.ics` — a single subscribable calendar feed (runs,
long runs, lift days). Tell the user to subscribe to the .ics once in
their calendar app; regenerating the file updates the feed.

## 4. Relay

Summarize the phase structure (base/build/peak/taper and weeks), the weekly
skeleton, peak mileage, the long-run progression, and the decision points
(the plan bakes in evaluation gates with downgrade actions — name their
dates). Do not dump the raw JSON.

## Never

- Hand-author plan JSON or edit `data/marathon_plan.json` directly — the
  generator writes `marathon_plan.generated.json`, and the schema is
  validated on load.
- Promise outcomes; the plan's own decision points exist because fitness is
  earned, not scheduled.
