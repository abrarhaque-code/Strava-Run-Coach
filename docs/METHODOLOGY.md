# Methodology

Every number this tool prints comes from a named, citable model, implemented
in plain Python you can read in one sitting. That is the point of the
stdlib-only rule: no black boxes, no `import magic`. This document names each
model, shows where it lives in the code, and — just as importantly — says
where the science is thin. A coaching tool that hides its uncertainty is a
worse coach.

## Race prediction: Daniels/Gilbert VDOT

**What:** VDOT is Jack Daniels' and Jimmy Gilbert's "effective VO2max" — a
single fitness number derived from a race (or race-like) performance, which
can then be projected onto any other distance.

**How it's implemented** (`race_predictor.py`, `metrics.py`): the classic
Daniels/Gilbert equations —

```
VO2 demanded  = -4.60 + 0.182258·v + 0.000104·v²          (v in m/min)
% of VO2max   = 0.8 + 0.1894393·e^(−0.012778·t) + 0.2989558·e^(−0.1932605·t)
VDOT          = VO2 / %VO2max                              (t in minutes)
```

Predicted race time inverts the same equations by binary search
(`race_predictor.predict_race_time`).

**The anchoring choice that matters:** `current_fitness_vdot` takes the MAX
VDOT across recent whole runs, Strava best-effort splits, and actual race
results from config `race_history` (a real race is a maximal, measured
effort, so it stays valid for ~180 days — far longer than the 30-day
training-run scan). The base-build projector (`scenario.py`) deliberately
does the opposite: it anchors marathon projections on the best recent
**endurance** effort (a half or long run), because a 5K VDOT systematically
over-predicts marathon fitness in under-built runners — the marathon is an
endurance exam, not a speed exam.

**Reference:** Daniels, J., *Daniels' Running Formula* (Human Kinetics);
Daniels & Gilbert, *Oxygen Power: Performance Tables for Distance Runners*
(1979).

## Training stress: TSS

**What:** a per-session load score in the spirit of Coggan's Training Stress
Score: one hour at threshold ≈ 100 points, scaled by intensity squared.

**How** (`enrichment.compute_run_tss`, `fitness_tracker.compute_tss`):

```
TSS = duration_hr · (avg_hr / threshold_hr)² · 100        (HR-based)
TSS = duration_hr · (threshold_pace / pace)² · 100        (pace fallback)
```

capped at 200 per session. HR is preferred whenever present — pace lies on
hills, treadmills, and hot days. Classification is load-bearing here: bike
sessions logged as "Run" entries never get the pace formula (a fake 10:00/mi
"pace" is exactly the pollution `enrichment.classify_activity` exists to
remove); they get HR-based or conservative duration-based cross-training
TSS instead.

**Caveat:** HR-based TSS is an approximation of the power/pace-based
original. It under-counts short intense work (HR lags effort) and
over-counts hot-day easy runs. It is consistent with itself, which is what a
trend model needs.

**Reference:** Allen, H. & Coggan, A., *Training and Racing with a Power
Meter* (VeloPress).

## Fitness / fatigue / form: Banister-style CTL / ATL / TSB

**What:** the impulse-response model behind every "performance management
chart": fitness is a slow exponentially-weighted average of daily load,
fatigue a fast one, and form is their difference.

**How** (`fitness_tracker.compute_loads_all`):

```
CTL_today = CTL + (TSS_today − CTL) / 42      # fitness, 42-day EWMA
ATL_today = ATL + (TSS_today − ATL) / 7       # fatigue, 7-day EWMA
TSB       = CTL − ATL                          # form ("freshness")
```

with a 42-day warmup before the reporting window so day one is converged.
Race-day target: TSB in the +10..+20 band — rested but not detrained.

**Reference:** Banister, E.W. et al., "A systems model of training for
athletic performance" (1975); popularized as CTL/ATL/TSB by Coggan.

## The two-stream load model (this project's own rule)

Most tools track one load number. Running has two distinct costs:

1. **Running mileage** — impact and structural durability. Only real runs
   count (`fitness_tracker.load_runs`, filtered through
   `enrichment.is_real_run`). This stream gates ramp rates, long-run
   progression, and plan compliance.
2. **Aerobic load** — the engine. Runs *plus* cross-training (bike at a
   configurable intensity factor) *plus* strength sessions
   (`fitness_tracker.load_sessions`) feed CTL/ATL/TSB.

The consequence is honest accounting in both directions: a Zone-2 bike hour
builds your aerobic base and shows up in fitness, but it never justifies a
steeper *running* ramp, because it did not load your tendons. Same-day
double-logs (device ride + manual equivalent entry) are deduplicated within
a 3-hour window so cross TSS isn't counted twice.

## Ramp guardrails and ACWR — used, with their caveats printed

The plan generator and scenario projector cap week-over-week mileage growth
and flag acute:chronic workload ratios (`analysis.compute_actr`: trailing
week vs 4-week average) outside ~0.8–1.3.

The honest part, straight from the code's own comments: **the 10%/week rule
failed its randomized controlled trial** (Buist et al. 2008 — a graded 10%
program produced the same injury rate as a faster ramp), and **ACWR is
genuinely contested** (Gabbett 2016 proposed the "sweet spot"; Impellizzeri
et al. 2020 dismantled much of its statistical foundation). This tool treats
both as *guardrails, not laws* — cheap insurance that costs a little
training upside, worth it for most self-coached runners. The constants are
in `config.json → scenario` so you can recalibrate them to your own
injury history.

**References:** Buist, I. et al., "No effect of a graded training program on
the number of running-related injuries in novice runners" (Am J Sports Med,
2008); Gabbett, T., "The training-injury prevention paradox" (BJSM, 2016);
Impellizzeri, F. et al., "Acute:chronic workload ratio: conceptual issues
and fundamental pitfalls" (Int J Sports Physiol Perform, 2020).

## Polarization

`analysis.polarization_stats` buckets runs by HR/pace zone and reports the
easy/hard split, against the ~80/20 pattern observed in elite endurance
training (Seiler). The common failure mode it exists to catch is the
"one-speed runner": every run moderately hard, no run truly easy, volume
stalls. **Reference:** Seiler, S., "What is best practice for training
intensity and duration distribution in endurance athletes?" (IJSPP, 2010).

## Goal probability: an in-house heuristic, labeled as such

`race_predictor.race_probability` is **not** from the literature:

```
p = 1 / (1 + e^(−1.2 · (current_VDOT − goal_VDOT)))   # logistic on the gap
p × 1.1 if fitness is trending up
p × 0.6 if running < 12 mi/wk, × 0.85 if < 18 mi/wk
```

A logistic on the VDOT gap with volume discounts, clamped to [1%, 99%].
It exists to convert "you are 3.5 VDOT short" into something a human can
feel the weight of. Treat it as a calibrated gut-check, not a forecast with
error bars.

## Decoupling at matched pace, and the run grade

Aerobic decoupling is heart rate rising at the SAME pace as a run goes on, a
proxy for durability and fuelling (the Friel/TrainingPeaks formulation). The
naive version, first-half HR vs second-half HR, measures how the run was
designed instead: a progression run, a fast finish or a hilly back half all
"drift". `post_run_review.pace_matched_decoupling` therefore pairs every
early split with every late split run within 12 s per split of it, scores
each pair's HR delta, and reports the median (the verdict) and the worst pair
(named). Under 3 bpm is clean, under 8 mild, above that real drift. The
longitudinal lens in `trends.py` (max minus average HR on long runs) is a
coarser screen and is labelled as such.

The review grades a run on five dimensions, each `ok` / `watch` (-10) /
`miss` (-20) from 100: intent (the athlete's own Strava description, else the
plan week), easy discipline (the share of easy splits under the HR cap), the
declared work (pace band AND HR band for tempo/threshold/race-pace segments;
marathon-pace blocks as laps at pace inside the HR band; reps reconstructed
from the stream), the finish (a surge that then fades 30+ s per split at
effort reads as over-spent; a slow finish under the easy cap is a cool-down),
and the clock cap on a long run. Load (TSS and the form it leaves) and the
wrist-HR plausibility check are reported, never deducted. HR is treated as
what the day cost and pace as what the athlete did, because heat, travel and
sleep move HR at the same pace; the easy cap is the one place HR alone
decides. **Caveat:** the bands are the athlete's configured zones, and the
grade is a rubric, not a measurement; the "confirm with the athlete" lines
exist because intent is not observable.

Efficiency (`pace at a controlled HR band, by quarter`, in `trends.py`) is the
cleanest aerobic-fitness signal available without lab testing: pace dropping
at the same HR means the engine is growing.

## What this tool does not model

Weather, altitude, course profile beyond an elevation-cost lens, sleep,
nutrition, life stress, injury risk beyond load ratios. When outputs and
body disagree, the body outranks the model.
