#!/usr/bin/env python3
"""Base-build scenario projector.

Answers: "If I base-build to an entry mileage of E mpw before a W-week marathon
block, what peak mileage does that imply, and what marathon time range does that
project?" Runs the configured entry scenarios (default 20 / 25 / 30) side by
side.

Honest about uncertainty by design:
- Ramp rates are guardrails, not laws. The 10%/week rule failed its main RCT
  (Buist/GRONORUN); ACWR is contested. So weekly growth is a conservative,
  config-tunable default and the better-evidenced constraint (long-run growth
  relative to the recent longest) is what is enforced.
- The volume -> marathon-time link is correlational and individual, so every
  projection is a RANGE with stated assumptions, never a single number.

Marathon time is anchored on the best recent ENDURANCE effort (a half or a long
run), not the max VDOT across all efforts. Anchoring on a 5K would overstate
marathon readiness for a runner whose speed is ahead of their endurance.

Reuses race_predictor's Jack Daniels VDOT math and config for the goal race.
Stdlib only.
"""

import json
import math
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import config
from race_predictor import (
    load_activities,
    compute_vdot,
    predict_race_time,
    fmt_time,
    fmt_pace_min_mi,
)

MARATHON_M = 42195.0
CACHE_DIR = Path(__file__).parent / "data" / "strava_cache" / "activities"

DEFAULTS = {
    "block_weeks": 16,
    "taper_weeks": 3,
    "peak_multiplier": 1.9,     # peak mpw ~ this x entry mpw over the block
    "cutback_every": 4,         # every Nth build week is a cutback
    "cutback_pct": 0.82,        # cutback week as a fraction of the ramped target
    "max_weekly_jump": 0.12,    # safety cap on week-over-week growth
    "long_run_frac": 0.42,      # peak long run as a fraction of peak weekly mpw
    "long_run_cap_mi": 22.0,    # absolute cap on the longest run
    "safe_weekly_ramp": 0.10,   # "comfortable" build rate ceiling
    "aggressive_weekly_ramp": 0.15,  # boundary into "unsafe"
    "aerobic_gain_max": 3.0,    # max projected VDOT gain over the block
    "aerobic_gain_tau": 25.0,   # diminishing-returns scale (mpw above current)
    # Late-race fade penalty (multiplier on the VDOT-ideal marathon time).
    # Keyed to long-run durability: low long-run volume -> bigger fade.
    "durability_floor_mi": 10.0,
    "durability_ceil_mi": 20.0,
    "penalty_worst_low_dur": 1.18,   # worst case at <=10mi longest run
    "penalty_worst_high_dur": 1.05,  # worst case at >=20mi longest run
    "penalty_best_low_dur": 1.06,    # best case at <=10mi longest run
    "penalty_best_high_dur": 1.02,   # best case at >=20mi longest run
    "entries": [20, 25, 30],
}


def cfg() -> dict:
    """Scenario constants, config-overridable via the optional 'scenario' block."""
    merged = dict(DEFAULTS)
    merged.update(config.load_config().get("scenario", {}) or {})
    return merged


# ---------------------------------------------------------------------------
# Mileage ramp
# ---------------------------------------------------------------------------

def _taper_fracs(n: int) -> list:
    """Descending fractions of peak for the taper weeks (race in the last week)."""
    presets = {1: [0.50], 2: [0.65, 0.45], 3: [0.75, 0.55, 0.40]}
    if n in presets:
        return presets[n]
    return [0.75 - 0.4 * i / max(1, n - 1) for i in range(n)]


def _cap_jumps(targets: list, max_jump: float) -> list:
    """Clamp each upward week-over-week step to <= max_jump (safety guardrail)."""
    out = list(targets)
    for i in range(1, len(out)):
        ceiling = out[i - 1] * (1 + max_jump)
        if out[i] > ceiling:
            out[i] = ceiling
    return out


def _phase_for(week_idx: int, build_weeks: int, taper_weeks: int) -> str:
    """Label a 1-based week index by phase."""
    if week_idx > build_weeks:
        return "taper"
    if week_idx <= max(2, build_weeks // 4):
        return "base"
    if week_idx >= build_weeks - 1:
        return "peak"
    return "build"


def build_ramp(entry_mi: float, c: dict = None) -> dict:
    """Project a week-by-week mileage ramp from an entry point.

    Returns weekly targets, the implied peak and average weekly mileage, and the
    peak long run (capped). Cutbacks dip every Nth build week; the last three
    weeks taper into the race.
    """
    c = c or cfg()
    weeks = int(c["block_weeks"])
    taper = int(c["taper_weeks"])
    build_weeks = weeks - taper
    peak_mi = entry_mi * c["peak_multiplier"]
    ramp_peak_week = max(2, build_weeks - 1)  # hit peak ~1 week before taper

    targets = []
    for w in range(1, build_weeks + 1):
        frac = min(1.0, (w - 1) / (ramp_peak_week - 1)) if ramp_peak_week > 1 else 1.0
        base = entry_mi + (peak_mi - entry_mi) * frac
        is_cutback = (w % int(c["cutback_every"]) == 0) and (w < ramp_peak_week)
        if is_cutback:
            base *= c["cutback_pct"]
        targets.append(base)

    for tf in _taper_fracs(taper):
        targets.append(peak_mi * tf)

    targets = _cap_jumps(targets, c["max_weekly_jump"])
    actual_peak = max(targets)
    long_run_peak = min(c["long_run_cap_mi"], actual_peak * c["long_run_frac"])

    weeks_out = []
    prev_long = max(4.0, entry_mi * 0.30)
    for i, t in enumerate(targets, start=1):
        phase = _phase_for(i, build_weeks, taper)
        if phase == "taper":
            # long run comes down through the taper
            lr = long_run_peak * _taper_fracs(taper)[i - build_weeks - 1]
        else:
            target_lr = min(long_run_peak, t * c["long_run_frac"])
            # evidence-based guardrail: cap long-run growth vs the recent longest
            lr = min(target_lr, prev_long * 1.10 + 0.5)
            lr = max(lr, prev_long * 0.7)  # cutback weeks ease the long run too
            prev_long = max(prev_long, lr)
        weeks_out.append({
            "week_num": i,
            "phase": phase,
            "target_miles": round(t, 1),
            "long_run_target": round(lr, 1),
        })

    return {
        "entry_mi": entry_mi,
        "weeks": weeks_out,
        "peak_mi": round(actual_peak, 1),
        "avg_mi": round(sum(targets) / len(targets), 1),
        "long_run_peak": round(long_run_peak, 1),
    }


# ---------------------------------------------------------------------------
# Fitness anchor + current volume (from real activities)
# ---------------------------------------------------------------------------

def endurance_anchor_vdot(activities: list, today=None) -> tuple:
    """Best VDOT from a recent ENDURANCE effort (half or long run), not a 5K.

    Looks back 120 days. Prefers efforts >= 10mi; falls back to >= 6mi, then any.
    """
    today = today or date.today()
    cutoff = _as_date(today) - timedelta(days=120)

    # Race results from config.race_history are the strongest endurance
    # anchors when recent and long enough (half-ish or beyond).
    hist_best = None
    try:
        import config as _config
        for h in _config.race_history():
            d = date.fromisoformat(h["date"])
            if not (cutoff <= d <= _as_date(today)):
                continue
            if (h.get("distance_mi") or 0) < 10.0:
                continue
            t_sec = _config.goal_time_to_sec(h.get("result_time", ""))
            if t_sec <= 0:
                continue
            v = compute_vdot(h["distance_mi"] * 1609.34, t_sec)
            if v > 0 and (hist_best is None or v > hist_best["vdot"]):
                hist_best = {
                    "vdot": v,
                    "name": h.get("name", "race result"),
                    "date": h["date"],
                    "distance_mi": h["distance_mi"],
                }
    except Exception:
        pass

    pools = [
        [a for a in activities if _as_date(a["date"]) >= cutoff and a["distance_mi"] >= 10.0],
        [a for a in activities if _as_date(a["date"]) >= cutoff and a["distance_mi"] >= 6.0],
        [a for a in activities if _as_date(a["date"]) >= cutoff and a["distance_mi"] >= 3.0],
    ]
    for i, pool in enumerate(pools):
        best = hist_best if i == 0 else None
        for a in pool:
            v = compute_vdot(a["distance_m"], a["time_sec"])
            if v > 0 and (best is None or v > best["vdot"]):
                best = {
                    "vdot": v,
                    "name": a.get("name", ""),
                    "date": _as_date(a["date"]).isoformat(),
                    "distance_mi": a["distance_mi"],
                }
        if best:
            return best["vdot"], best
    return 38.0, {"note": "no recent endurance effort; using a neutral default"}


def trailing_run_mpw(activities: list, today=None, weeks: int = 4) -> float:
    """Average running miles per week over the trailing window (runs only)."""
    today = _as_date(today or date.today())
    cutoff = today - timedelta(days=weeks * 7)
    miles = sum(a["distance_mi"] for a in activities if _as_date(a["date"]) >= cutoff)
    return miles / weeks


def trailing_crosstrain_equiv_mpw(today=None, weeks: int = 4) -> float:
    """Run-equivalent miles per week from cross-training (e.g. Zone-2 bike).

    Uses the athlete's own conversion (config.crosstrain.bike_min_per_mi,
    default 10 min = 1 mi). This counts toward the AEROBIC base, not running
    mileage: biking maintains the engine but not the legs' impact durability.
    """
    today = _as_date(today or date.today())
    cutoff = today - timedelta(days=weeks * 7)
    per_mi = float(config.crosstrain_cfg().get("bike_min_per_mi", 10.0))
    if per_mi <= 0 or not CACHE_DIR.exists():
        return 0.0
    total = 0.0
    for p in CACHE_DIR.glob("*.json"):
        try:
            a = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if a.get("type") != "CrossTrain":
            continue
        try:
            d = datetime.fromisoformat(a["start_date_local"].replace("Z", "")).date()
        except (KeyError, ValueError, AttributeError):
            continue
        if d < cutoff:
            continue
        total += ((a.get("moving_time", 0) or 0) / 60.0) / per_mi
    return total / weeks


# ---------------------------------------------------------------------------
# Marathon projection (range, not a point)
# ---------------------------------------------------------------------------

def aerobic_gain_bounds(avg_mi: float, current_mpw: float, c: dict) -> tuple:
    """Projected VDOT gain over the block: (low, high), diminishing with mileage."""
    delta = max(0.0, avg_mi - max(1.0, current_mpw))
    mid = c["aerobic_gain_max"] * (1 - math.exp(-delta / c["aerobic_gain_tau"]))
    return (0.5 * mid, mid)


def exec_penalty_bounds(long_run_peak: float, c: dict) -> tuple:
    """Late-race fade multiplier (>1): shrinks as long-run durability rises.

    Driven by long-run volume specifically, so cross-training (which builds the
    aerobic engine but not impact durability) does not erase the penalty.
    """
    floor, ceil = c["durability_floor_mi"], c["durability_ceil_mi"]
    lr = max(floor, min(ceil, long_run_peak))
    t = (lr - floor) / (ceil - floor) if ceil > floor else 1.0  # 0 low dur, 1 high dur
    worst = c["penalty_worst_low_dur"] - t * (c["penalty_worst_low_dur"] - c["penalty_worst_high_dur"])
    best = c["penalty_best_low_dur"] - t * (c["penalty_best_low_dur"] - c["penalty_best_high_dur"])
    return min(best, worst), worst


def project_marathon(anchor_vdot: float, ramp: dict, current_mpw: float, c: dict = None) -> dict:
    """Project a marathon finish-time range for a given ramp. Returns a dict."""
    c = c or cfg()
    glow, ghigh = aerobic_gain_bounds(ramp["avg_mi"], current_mpw, c)
    pbest, pworst = exec_penalty_bounds(ramp["long_run_peak"], c)
    fast = predict_race_time(anchor_vdot + ghigh, MARATHON_M) * pbest
    slow = predict_race_time(anchor_vdot + glow, MARATHON_M) * pworst
    mid_vdot = anchor_vdot + (glow + ghigh) / 2.0
    return {
        "fast_sec": int(fast),
        "slow_sec": int(slow),
        "gain_bounds": (round(glow, 1), round(ghigh, 1)),
        "penalty_bounds": (round(pbest, 3), round(pworst, 3)),
        "projected_vdot_mid": round(mid_vdot, 1),
    }


# ---------------------------------------------------------------------------
# Feasibility given the runway to the block start
# ---------------------------------------------------------------------------

def feasibility(current_mpw: float, entry_mi: float, runway_weeks: float, c: dict) -> dict:
    """Classify how reachable an entry point is from current mileage.

    Uses the required compounding weekly rate vs the comfortable (10%) and
    aggressive (15%) guardrails. Explicitly a guardrail, not a guarantee.
    """
    if runway_weeks <= 0 or current_mpw <= 0:
        rate = float("inf") if entry_mi > current_mpw else 0.0
    else:
        rate = (entry_mi / current_mpw) ** (1.0 / runway_weeks) - 1.0
    if rate <= c["safe_weekly_ramp"]:
        label = "comfortable"
    elif rate <= c["aggressive_weekly_ramp"]:
        label = "aggressive"
    else:
        label = "unsafe"
    return {"label": label, "required_weekly_rate": rate}


# ---------------------------------------------------------------------------
# Orchestration + report
# ---------------------------------------------------------------------------

def _as_date(d):
    return d.date() if hasattr(d, "date") else d


def derive_inputs(today=None):
    """Anchor VDOT, current running volume, and aerobic-equivalent volume."""
    today = today or date.today()
    acts = load_activities()  # type == Run only (cross-training is excluded)
    anchor, src = endurance_anchor_vdot(acts, today)
    run_mpw = trailing_run_mpw(acts, today, weeks=4)
    xt_mpw = trailing_crosstrain_equiv_mpw(today, weeks=4)
    return {
        "anchor": anchor,
        "anchor_src": src,
        "run_mpw": run_mpw,
        "crosstrain_equiv_mpw": xt_mpw,
        "aerobic_mpw": run_mpw + xt_mpw,
        "activities_loaded": len(acts),
    }


def runway_to_block(c: dict, today=None) -> tuple:
    """(block_start_date, runway_weeks) for the active marathon race."""
    today = today or date.today()
    race = config.active_race(today)
    race_date = date.fromisoformat(race["date"])
    block_start = race_date - timedelta(weeks=int(c["block_weeks"]))
    runway_weeks = max(0.0, (block_start - today).days / 7.0)
    return race_date, block_start, runway_weeks


def compare_scenarios(entries=None, today=None) -> dict:
    c = cfg()
    today = today or date.today()
    inp = derive_inputs(today)
    anchor = inp["anchor"]
    run_mpw = inp["run_mpw"]
    entries = entries or c["entries"]
    race_date, block_start, runway = runway_to_block(c, today)

    rows = []
    for e in entries:
        ramp = build_ramp(e, c)
        # Aerobic gain is anchored on running volume; cross-training's benefit is
        # already carried in the endurance anchor VDOT. Feasibility uses running
        # volume because the ramp governs impact durability, which biking does
        # not build.
        proj = project_marathon(anchor, ramp, run_mpw, c)
        feas = feasibility(run_mpw, e, runway, c)
        rows.append({"entry": e, "ramp": ramp, "proj": proj, "feas": feas})

    return {
        "anchor_vdot": anchor,
        "anchor_src": inp["anchor_src"],
        "run_mpw": run_mpw,
        "crosstrain_equiv_mpw": inp["crosstrain_equiv_mpw"],
        "aerobic_mpw": inp["aerobic_mpw"],
        "activities_loaded": inp["activities_loaded"],
        "race_date": race_date,
        "block_start": block_start,
        "runway_weeks": runway,
        "block_weeks": c["block_weeks"],
        "rows": rows,
    }


def print_scenarios(entries=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    r = compare_scenarios(entries)
    race = config.active_race()
    print("=" * 72)
    print(f"  BASE-BUILD SCENARIOS  |  {race['name']} ({r['race_date']})")
    print("=" * 72)
    print()
    print(f"  Anchor fitness (endurance): VDOT {r['anchor_vdot']:.1f}")
    src = r["anchor_src"]
    if src.get("date"):
        dist = f"{src.get('distance_mi', 0):.1f}mi " if src.get("distance_mi") else ""
        print(f"    from {dist}on {src['date']}  [{src.get('name','')}]")
    xt = r["crosstrain_equiv_mpw"]
    print(f"  Current running volume: {r['run_mpw']:.0f} mpw (trailing 4 wk, runs only)")
    if xt >= 0.1:
        print(f"  + Zone-2 cross-training:  {xt:.0f} mpw-equiv  "
              f"->  aerobic-equivalent base {r['aerobic_mpw']:.0f} mpw")
    print(f"  16-wk block starts ~{r['block_start']}  ->  runway to build base: "
          f"{r['runway_weeks']:.1f} weeks")
    print()
    print(f"  {'Entry':>6} {'Peak':>6} {'Avg':>5} {'Long':>5}  {'Marathon range':>17}  "
          f"{'Reach base?':>12}")
    print(f"  {'mpw':>6} {'mpw':>6} {'mpw':>5} {'run':>5}  {'(projected)':>17}  "
          f"{'by Jul':>12}")
    print("  " + "-" * 64)
    for row in r["rows"]:
        ramp, proj, feas = row["ramp"], row["proj"], row["feas"]
        rng = f"{fmt_time(proj['fast_sec'])}-{fmt_time(proj['slow_sec'])}"
        print(f"  {row['entry']:>6.0f} {ramp['peak_mi']:>6.0f} {ramp['avg_mi']:>5.0f} "
              f"{ramp['long_run_peak']:>5.0f}  {rng:>17}  {feas['label']:>12}")
    print()
    print("  How to read this:")
    print(f"  - Peak mpw ~= entry x {cfg()['peak_multiplier']:.2f} (the build factor, "
          "tunable in config).")
    print("    Peak is tied to entry, so a low entry caps how high you can safely build.")
    print("  - Marathon range blends a bounded aerobic gain over the block with a")
    print("    late-race fade penalty that shrinks as your long-run volume rises.")
    print("  - 'Reach base?' uses RUNNING volume only: biking builds the aerobic")
    print("    engine but not impact durability, so it can't justify a steeper run ramp.")
    print("    Z2 bike still counts as aerobic-equivalent base above and as an easy-run")
    print("    substitute in the plan (10 min ~= 1 mi).")
    print()
    print("  Caveats (read these): ramp percentages are heuristics, not validated")
    print("  laws; the volume->time link is correlational and individual. Treat the")
    print("  ranges as planning bands, not predictions. Constants live in")
    print("  config.json -> 'scenario' so you can recalibrate them to your response.")
    print()


if __name__ == "__main__":
    args = sys.argv[1:]
    entries = None
    for a in args:
        if a.startswith("--entry"):
            val = a.split("=", 1)[1] if "=" in a else None
            if val is None:
                idx = args.index(a)
                val = args[idx + 1] if idx + 1 < len(args) else ""
            entries = [float(x) for x in val.split(",") if x.strip()]
    print_scenarios(entries)
