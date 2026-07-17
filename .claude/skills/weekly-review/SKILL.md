---
name: weekly-review
description: Run the weekly training check-in - plan compliance, fitness trend, and one focus for next week. Use when the user asks "how did my training week go", "weekly review", "am I on plan", "week summary", or on a recurring Monday check-in.
---

# Weekly training review

Run from the strava-run-coach repo root (see the `strava-coach-analyze`
skill, step 0, if there is no checkout).

## 1. Ensure data freshness

Check the newest file in `data/strava_cache/activities/` (or the newest row
in `activities.csv`). If it is older than ~2 days and the Strava MCP is
available, refresh first using the ingestion procedure in
`../strava-coach-analyze/SKILL.md` (steps 2-4). If no fresh data is
available, proceed but say clearly that the review is based on stale data.

## 2. Run the review

```bash
python3 coach.py reconcile     # record actual-vs-planned into plan_state.json
python3 coach.py week          # weekly check-in
python3 coach.py fitness       # CTL/ATL/TSB trend
```

(`reconcile` is meaningful only when a structured plan is active; it
degrades to a one-line notice otherwise — that is fine.)

## 3. Relay

Report, in this order:
1. Last week vs plan: miles, run count, long run, status
   (complete/partial/missed) — use reconcile's deltas if it printed any.
2. Fitness direction: CTL trend, TSB now, phase.
3. This week's targets and the key workout.
4. ONE actionable focus for next week — the single highest-leverage thing,
   not a list.

If the user made an in-the-moment adjustment worth remembering
("moving my long run to Sunday"), record it:
`python3 coach.py note "moved long run to Sunday - travel"`.

## Never

- Paste raw JSON or full file contents back to the user.
- Mark weeks complete/missed yourself unless the user explicitly asks
  (manual marks permanently override the automatic reconciliation).
