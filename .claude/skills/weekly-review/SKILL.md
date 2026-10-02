---
name: weekly-review
description: The weekly check-in - last week against the plan, this week's seven days with any standing notes applied, grades for the recent runs, the next checkpoint read live, and fitness - ending in one focus. Use for "how did my week go", "weekly review", "am I on plan", "what does this week look like", "move my long run", "no speedwork until my calf settles", or a recurring Monday check-in.
---

# Weekly review

Engine location and voice: see `strava-coach-analyze` step 0 and
`docs/COACHING.md`.

## 1. Fresh data

`python3 coach.py status --json`. If `newest_activity` is more than two days
old and the Strava MCP is available, refresh: `list_activities` from
`newest_activity` minus three days (`fetch.range_start` on an empty cache),
save the pages to `data/mcp/list/`, `python3 coach.py ingest`, then fetch
`get_activity_performance` for `fetch.perf_needed` and `get_activity_streams`
for `fetch.streams_needed` (see `strava-coach-analyze` step 3) and ingest
again. No fresh data available: proceed, and say the review is on stale data.

## 2. Run it

```bash
python3 coach.py week                     # reconciles actuals, prints, writes plan_output/weekly/YYYY-Wnn.md
python3 coach.py week --date 2026-09-28   # a frozen date (looking back)
```

Without a plan it still shows the last seven days of runs and fitness, and
says how to build one.

## 3. Relay, in the report's order

1. **Last week vs plan**: volume, runs, long run, status, the verdict line. A
   missed week repeats; two in a row slide the plan. "We adjust and move
   forward", never a lecture.
2. **This week**: the seven-day layout in their unit, today marked; standing
   notes that shaped it (a key day turned easy "per your note").
3. **Runs, last 7 days**: one line each with the grade and the dimension that
   set it. `python3 coach.py review <id>` for the full debrief of any of them.
4. **Checkpoint**: the next decision point, criteria met so far, what happens
   if it is missed.
5. **Fitness**: CTL/ATL/TSB and phase; repeat the history warning if printed.

End with ONE focus for the coming week.

## 4. Notes the athlete gives you

Something that should shape the coming weeks ("no speedwork until the calf
settles", "travelling, easy runs only until the 20th"):

```bash
python3 coach.py note "no speedwork until the calf settles" --until 2026-10-20
```

Key days turn easy in the layout and the brief while the note stands. A
one-off ("moved the long run to Sunday"): `python3 coach.py note "..."`
without `--until`. A lasting change to the week's shape (fewer days, a
different long-run day) is a plan change: see `build-plan`.

## Never

- Mark weeks complete or missed yourself (manual marks override the
  automatic reconciliation for good).
- Regenerate the plan to paper over a missed week; the plan's rule is repeat,
  then slide.
- Paste the report file or JSON; relay it.
