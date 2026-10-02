---
name: race-forecast
description: Predict the race time and goal probability from current fitness (Daniels VDOT, real runs only), say what would move the number, and explore what-if volume scenarios. Use for "can I run 3:45", "what's my predicted marathon time", "what's my VDOT", "goal probability", "what if I trained at 50 km a week".
---

# Race forecast and scenarios

Engine location and voice: see `strava-coach-analyze` step 0 and
`docs/COACHING.md`. Stale data (`status --json` -> `newest_activity`): refresh
with that skill's step 3 first.

## 1. Forecast

```bash
python3 coach.py forecast
```

It prints the fitness anchor (the best recent whole run or best effort, or a
race result from `race_history`; rep sessions are excluded because reps read
as one continuous run inflate VDOT), the predicted time and pace for the
active race, the gap to the goal, a probability with its verdict, three
things to do about the gap drawn from the athlete's own zones and plan week,
and the 14-day trend against the prior 30.

## 2. What-if volume

```bash
python3 coach.py scenario --entry 30,40,50      # in the athlete's unit; "50km" also works
```

Each entry volume implies a peak, a long-run peak and a marathon-time RANGE
anchored on the best recent ENDURANCE effort (a half or a long run; a 5K
over-predicts marathon fitness), plus whether the base is reachable before
the block starts.

## 3. Relay

- Predicted time vs goal and the probability as a band ("a coin flip that
  leans against you"), then the anchor: which effort, when.
- The "what you need to do" lines are the engine's; pick the one that
  matters most and say why.
- Scenario outputs are planning bands, not promises; the ramp rates are
  guardrails (`docs/METHODOLOGY.md`).
- A big gap gets named plainly, with what moves it (volume and consistency,
  per the model), not softened.

## Never

- Hand-compute VDOT or invent a probability.
- Quote a scenario number without its range.
- Read fitness trend off under 60 days of history.
