---
name: race-forecast
description: Predict race time and goal probability from current fitness (Daniels VDOT), and explore what-if mileage scenarios. Use when the user asks "can I run 3:45", "what's my predicted marathon/half time", "what's my VDOT", "goal probability", or "what if I trained at 30 miles a week".
---

# Race forecast + scenarios

Run from the strava-run-coach repo root (see the `strava-coach-analyze`
skill, step 0, if there is no checkout). If the data is stale, refresh it
first via that skill's ingestion steps.

## 1. Forecast

```bash
python3 coach.py forecast
```

This prints the current VDOT anchor (best recent effort or a race result
from config `race_history`), the predicted time for the active race, the
gap to goal, and a probability verdict.

## 2. What-if mileage questions

```bash
python3 coach.py scenario --entry 20,25,30    # or the user's numbers
```

Each entry mileage implies a peak and a marathon-time RANGE anchored on the
best recent ENDURANCE effort (a half or long run — deliberately not a 5K,
which over-predicts marathon fitness).

## 3. Relay

- Lead with predicted time vs goal and the probability verdict.
- Explain the anchor: which effort/race the VDOT came from and when.
- Present scenario outputs as planning bands, not promises; the ramp rates
  are guardrails (see docs/METHODOLOGY.md for the honest caveats).
- If the gap is large, say what would actually move it (volume and
  consistency, per the model) rather than softening the number.

## Never

- Hand-compute VDOT or invent probabilities — run the engine.
- Present a single scenario number without its range.
