---
name: review-run
description: Debrief one run - the latest, or a run the athlete names - on the five things that matter - easy discipline, workout execution (reps, tempo, marathon-pace blocks), how the long run held (pace-matched decoupling, stops, the finish), training load, and a grade - plus a flag when the wrist heart-rate trace looks unreliable. Use for "how was my run", "review my long run", "did I execute the workout", "grade today's run", "was that too hard", "how did Saturday go".
---

# Review a run

Engine location and voice: see `strava-coach-analyze` step 0 and
`docs/COACHING.md`. The review judges the run against the athlete's own
Strava description first ("13 easy, 3 at half pace, 2 easy"), then the plan
week; HR is reported as what the day cost, pace as what they did.

## 1. Which run

`python3 coach.py status --json` gives `newest_run_id`. For "Saturday's long
run" or a named run, find the id in the cache or with `list_activities`
(`range_start` the day before, `first: 10`).

## 2. Make sure the run has its detail

- `get_activity_performance(id)` -> `data/mcp/perf/<id>.json` (heart rate and
  laps; without laps the review reads the run average only).
- Long run or workout: `get_activity_streams(id, streams=["time","heart_rate",
  "velocity_smooth","cadence","distance","altitude","moving"], resolution=3000)`
  -> `data/mcp/streams/<id>.json` (stops, time in HR bands, reps, the sensor
  check all need it).
- `python3 coach.py ingest`

## 3. Review

```bash
python3 coach.py review <id>          # or: review   (latest run)
python3 coach.py review <id> --json   # the dict, for anything you need to look up
```

The "Data: laps yes/no, stream yes/no" line says how deep the read is; if a
stream is missing on a long run or workout, fetch it and run again rather
than relaying a thin review.

## 4. Relay

- Lead with the grade and the one or two verdicts that set it, then what went
  well. Verdicts: `+` ok, `~` watch (-10), `-` miss (-20), `i` information.
- "You said:" means the run was scored against their description. No
  description and undeclared fast miles: the review reports them without
  scoring. ASK what the plan was before calling them a surge or a fade; then
  the athlete can put it in the description and the run scores properly.
- Finish: `surge_then_fade` reads as over-spent, `deliberate` as a tired-legs
  finish. Both say "confirm with the athlete". Do.
- Decoupling is the pace-matched line (HR at the same pace, early vs late).
  Do not call a rising HR across a progression run "drift".
- HR is cost: "pace right, HR above the band: a hot day or too fast for today",
  not "you ran too hard". Heat, travel and sleep move HR at the same pace.
- Sensor line: when it flags the trace, say so; a low reading gets the same
  scrutiny as a high one.
- Load line: TSS and form going in and out. Flagged "history understated"
  means do not read fatigue off it.

## Never

- Grade by average heart rate alone.
- Diagnose fatigue or fitness from one run, or from under 60 days of history.
- Paste the JSON or the lap table verbatim; describe it.
