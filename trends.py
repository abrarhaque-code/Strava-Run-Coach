"""Long-horizon training trends: drift, efficiency, consistency, recovery.

These are the lenses that answer "is the training working?" over months, not
days — complementary to analysis.py (weekly aggregates, polarization, ACTR)
and post_run_review.py (one run's laps and streams):

- drift_history:       max-vs-avg HR gap on long runs (fatigue accumulation)
- efficiency_trend:    pace at a controlled HR band per quarter (aerobic gains)
- consistency:         gaps between runs + weeks at 3+/2/1 runs
- recovery_pattern:    HR elevation in runs the day(s) after hard efforts
- elevation_cost:      flat-vs-hilly pace and HR deltas
- treadmill_vs_outdoor: same-athlete indoor/outdoor comparison
- effort_efficiency:   Strava relative effort per mile, best and worst

Every function is pure (list-of-runs in, dict out) so the whole module tests
without file I/O; load_trend_runs() is the only thing that touches disk.
Thresholds come from config.json -> "trends" (falls back to sane defaults).

Usage:
    python3 trends.py            # or: python3 coach.py trends
"""

import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta

import config


# ---------------------------------------------------------------------------
# Loading + normalization
# ---------------------------------------------------------------------------

def _norm(a: dict):
    """Normalize a cache/CSV activity dict to the flat row trends read."""
    iso = a.get("start_date_local") or a.get("start_date") or ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None
    dist_mi = (a.get("distance", 0) or 0) / 1609.34
    moving_min = (a.get("moving_time", 0) or 0) / 60
    if dist_mi <= 0 or moving_min <= 0:
        return None
    return {
        "date": dt,
        "dist_mi": dist_mi,
        "moving_min": moving_min,
        "pace": moving_min / dist_mi,
        "avg_hr": a.get("average_heartrate"),
        "max_hr": a.get("max_heartrate"),
        "elev": a.get("total_elevation_gain") or 0,
        "rel_effort": a.get("relative_effort"),
    }


def load_trend_runs() -> list:
    """Real runs only (metrics.load_activities filters through is_real_run)."""
    from metrics import load_activities
    rows = []
    for a in load_activities(activity_type="Run"):
        r = _norm(a)
        if r:
            rows.append(r)
    rows.sort(key=lambda r: r["date"])
    return rows


def _cfg() -> dict:
    return config.load_config().get("trends", {})


# ---------------------------------------------------------------------------
# Lenses (pure functions)
# ---------------------------------------------------------------------------

def drift_history(runs: list, min_mi: float = None) -> list:
    """Per-long-run HR drift proxy: max HR - avg HR, plus effort per mile.

    A widening gap on comparable long runs means fatigue is accumulating
    late in runs — the classic sign of starting too fast or a thin aerobic
    base. (post_run_review computes true first-half/second-half drift from
    streams for a single run; this is the longitudinal view.)
    """
    if min_mi is None:
        min_mi = _cfg().get("long_run_min_mi", 6)
    out = []
    for r in runs:
        if not (r["avg_hr"] and r["max_hr"] and r["dist_mi"] >= min_mi):
            continue
        out.append({
            "date": r["date"].date().isoformat(),
            "dist_mi": round(r["dist_mi"], 1),
            "pace": round(r["pace"], 2),
            "avg_hr": round(r["avg_hr"]),
            "max_hr": round(r["max_hr"]),
            "drift_bpm": round(r["max_hr"] - r["avg_hr"]),
            "effort_per_mi": round(r["rel_effort"] / r["dist_mi"], 1)
                             if r["rel_effort"] else None,
        })
    out.sort(key=lambda x: x["dist_mi"], reverse=True)
    return out


def efficiency_trend(runs: list, hr_band: tuple = None) -> list:
    """Pace at a controlled HR band, bucketed by quarter.

    Pace dropping at the same HR = aerobic fitness improving. Flat = volume
    is the bottleneck, not speed work.
    """
    if hr_band is None:
        band = _cfg().get("controlled_hr_band", [145, 155])
        hr_band = (band[0], band[1])
    lo, hi = hr_band
    quarters = defaultdict(list)
    for r in runs:
        if r["avg_hr"] and lo <= r["avg_hr"] <= hi and r["dist_mi"] >= 3:
            q = f"{r['date'].year} Q{(r['date'].month - 1) // 3 + 1}"
            quarters[q].append(r["pace"])
    out = []
    for q in sorted(quarters.keys()):
        paces = quarters[q]
        if len(paces) >= 2:
            out.append({"quarter": q, "avg_pace": round(statistics.mean(paces), 2),
                        "runs": len(paces)})
    return out


def consistency(runs: list, window_weeks: int = 52,
                today: datetime = None) -> dict:
    """Longest gaps between runs + week-frequency histogram over the window.

    Frequency is usually the binding constraint on race outcomes — a plan's
    mileage means little if half the weeks have one run in them.
    """
    if today is None:
        today = datetime.now()
    cutoff = today - timedelta(weeks=window_weeks)
    recent = [r for r in runs if r["date"] >= cutoff]

    gaps = []
    for prev, cur in zip(recent, recent[1:]):
        gaps.append({
            "days": (cur["date"] - prev["date"]).days,
            "after": prev["date"].date().isoformat(),
            "before": cur["date"].date().isoformat(),
        })
    gaps.sort(key=lambda g: g["days"], reverse=True)

    weeks = defaultdict(int)
    for r in recent:
        iso = r["date"].isocalendar()
        weeks[f"{iso[0]}-W{iso[1]:02d}"] += 1
    total = len(weeks)
    hist = {
        "3plus": sum(1 for v in weeks.values() if v >= 3),
        "2": sum(1 for v in weeks.values() if v == 2),
        "1": sum(1 for v in weeks.values() if v == 1),
    }
    return {
        "window_weeks": window_weeks,
        "weeks_with_runs": total,
        "longest_gaps": gaps[:8],
        "weeks_3plus": hist["3plus"],
        "weeks_2": hist["2"],
        "weeks_1": hist["1"],
        "pct_3plus": round(hist["3plus"] / total * 100) if total else 0,
    }


def recovery_pattern(runs: list, hard_hr_floor: int = None,
                     hard_dist_mi: float = None) -> dict:
    """Compare HR/pace of runs 1-2 days after a hard effort vs normal days.

    A big HR elevation after hard days means the easy days aren't easy
    enough (or recovery capacity is the limiter).
    """
    tcfg = _cfg()
    if hard_hr_floor is None:
        hard_hr_floor = tcfg.get("hard_hr_floor", 155)
    if hard_dist_mi is None:
        hard_dist_mi = tcfg.get("hard_dist_mi", 6)

    hard_dates = {r["date"].date() for r in runs
                  if (r["avg_hr"] and r["avg_hr"] > hard_hr_floor)
                  or r["dist_mi"] > hard_dist_mi}

    post_hard, normal = [], []
    for r in runs:
        if not r["avg_hr"] or r["dist_mi"] < 1:
            continue
        prev1 = (r["date"] - timedelta(days=1)).date()
        prev2 = (r["date"] - timedelta(days=2)).date()
        if prev1 in hard_dates or prev2 in hard_dates:
            post_hard.append(r)
        elif r["date"].date() not in hard_dates:
            normal.append(r)

    if not post_hard or not normal:
        return {"note": "not enough data", "post_hard_runs": len(post_hard),
                "normal_runs": len(normal)}
    return {
        "post_hard_runs": len(post_hard),
        "normal_runs": len(normal),
        "post_hard_avg_hr": round(statistics.mean(r["avg_hr"] for r in post_hard), 1),
        "normal_avg_hr": round(statistics.mean(r["avg_hr"] for r in normal), 1),
        "post_hard_avg_pace": round(statistics.mean(r["pace"] for r in post_hard), 2),
        "normal_avg_pace": round(statistics.mean(r["pace"] for r in normal), 2),
        "hr_elevation_bpm": round(
            statistics.mean(r["avg_hr"] for r in post_hard)
            - statistics.mean(r["avg_hr"] for r in normal), 1),
    }


def elevation_cost(runs: list, flat_max_m: float = 20,
                   hilly_min_m: float = 50) -> dict:
    """Pace and HR cost of hilly runs vs flat ones (same athlete, all data).

    Useful for budgeting a non-flat race course honestly.
    """
    pool = [r for r in runs if r["avg_hr"] and 7 < r["pace"] < 12
            and r["dist_mi"] > 2]
    flat = [r for r in pool if r["elev"] < flat_max_m]
    hilly = [r for r in pool if r["elev"] >= hilly_min_m]
    if not flat or not hilly:
        return {"note": "not enough data", "flat_runs": len(flat),
                "hilly_runs": len(hilly)}
    flat_pace = statistics.mean(r["pace"] for r in flat)
    hilly_pace = statistics.mean(r["pace"] for r in hilly)
    flat_hr = statistics.mean(r["avg_hr"] for r in flat)
    hilly_hr = statistics.mean(r["avg_hr"] for r in hilly)
    return {
        "flat_runs": len(flat), "hilly_runs": len(hilly),
        "flat_pace": round(flat_pace, 2), "hilly_pace": round(hilly_pace, 2),
        "flat_hr": round(flat_hr, 1), "hilly_hr": round(hilly_hr, 1),
        "cost_sec_per_mi": round((hilly_pace - flat_pace) * 60),
        "cost_bpm": round(hilly_hr - flat_hr, 1),
    }


def treadmill_vs_outdoor(runs: list) -> dict:
    """Indoor (zero elevation gain) vs outdoor pace/HR.

    Treadmill pace is usually NOT comparable to outdoor pace at the same HR
    — this quantifies the gap so HR, not pace, drives prescriptions.
    """
    indoor = [r for r in runs if r["avg_hr"] and r["elev"] == 0
              and r["dist_mi"] >= 1.5]
    outdoor = [r for r in runs if r["avg_hr"] and r["elev"] > 5
               and r["dist_mi"] >= 1.5]
    if not indoor or not outdoor:
        return {"note": "not enough data", "indoor_runs": len(indoor),
                "outdoor_runs": len(outdoor)}
    return {
        "indoor_runs": len(indoor), "outdoor_runs": len(outdoor),
        "indoor_pace": round(statistics.mean(r["pace"] for r in indoor), 2),
        "indoor_hr": round(statistics.mean(r["avg_hr"] for r in indoor), 1),
        "outdoor_pace": round(statistics.mean(r["pace"] for r in outdoor), 2),
        "outdoor_hr": round(statistics.mean(r["avg_hr"] for r in outdoor), 1),
    }


def effort_efficiency(runs: list, max_pace: float = 9.5, top_n: int = 5) -> dict:
    """Strava relative effort per mile for faster runs: best and worst.

    Low effort/mi at pace = efficient. The worst list is where overstriding,
    heat, or fatigue show up.
    """
    pool = [dict(r, eff_per_mi=r["rel_effort"] / r["dist_mi"])
            for r in runs
            if r["rel_effort"] and r["dist_mi"] >= 2 and r["pace"] < max_pace]
    if not pool:
        return {"note": "not enough data"}
    pool.sort(key=lambda r: r["eff_per_mi"])

    def _fmt(r):
        return {"date": r["date"].date().isoformat(),
                "dist_mi": round(r["dist_mi"], 1),
                "pace": round(r["pace"], 2),
                "eff_per_mi": round(r["eff_per_mi"], 1),
                "avg_hr": round(r["avg_hr"]) if r["avg_hr"] else None}

    return {"most_efficient": [_fmt(r) for r in pool[:top_n]],
            "least_efficient": [_fmt(r) for r in pool[-top_n:]]}


# ---------------------------------------------------------------------------
# Reporter
# ---------------------------------------------------------------------------

def _fmt_pace(p: float) -> str:
    if not p or p <= 0:
        return "N/A"
    m = int(p)
    s = int(round((p - m) * 60))
    if s == 60:
        m, s = m + 1, 0
    return f"{m}:{s:02d}"


def print_trends(runs: list = None) -> None:
    if runs is None:
        runs = load_trend_runs()
    print(f"Loaded {len(runs)} real runs")

    print()
    print("CARDIAC DRIFT ON LONG RUNS (max HR - avg HR)")
    drifts = drift_history(runs)
    for d in drifts[:10]:
        eff = f" | effort/mi {d['effort_per_mi']}" if d["effort_per_mi"] else ""
        print(f"  {d['date']} | {d['dist_mi']:5.1f}mi | {_fmt_pace(d['pace'])}/mi | "
              f"avg {d['avg_hr']} max {d['max_hr']} | drift {d['drift_bpm']:2d} bpm{eff}")
    if drifts:
        print("  Rising drift on comparable runs = fatigue building late; "
              "start long runs more conservatively.")
    else:
        print("  (no qualifying long runs with HR yet)")

    print()
    print("AEROBIC EFFICIENCY (pace at controlled HR, by quarter)")
    for e in efficiency_trend(runs):
        print(f"  {e['quarter']}: {_fmt_pace(e['avg_pace'])}/mi ({e['runs']} runs)")
    print("  Pace dropping at the same HR = aerobic fitness improving. "
          "Flat = volume, not speed work, is the bottleneck.")

    print()
    print("CONSISTENCY (rolling year)")
    c = consistency(runs)
    print(f"  Weeks at 3+ runs: {c['weeks_3plus']}/{c['weeks_with_runs']} "
          f"({c['pct_3plus']}%) | 2 runs: {c['weeks_2']} | 1 run: {c['weeks_1']}")
    for g in c["longest_gaps"][:5]:
        print(f"  gap {g['days']:2d} days: {g['after']} -> {g['before']}")

    print()
    print("RECOVERY PATTERN (runs 1-2 days after hard efforts)")
    rp = recovery_pattern(runs)
    if "note" in rp:
        print(f"  {rp['note']}")
    else:
        print(f"  After hard days: HR {rp['post_hard_avg_hr']}, "
              f"{_fmt_pace(rp['post_hard_avg_pace'])}/mi ({rp['post_hard_runs']} runs)")
        print(f"  Normal days:     HR {rp['normal_avg_hr']}, "
              f"{_fmt_pace(rp['normal_avg_pace'])}/mi ({rp['normal_runs']} runs)")
        if rp["hr_elevation_bpm"] > 3:
            print(f"  HR runs {rp['hr_elevation_bpm']} bpm higher after hard days — "
                  "recovery runs need to be genuinely easy.")
        else:
            print(f"  HR elevation is small ({rp['hr_elevation_bpm']} bpm) — "
                  "recovery looks adequate.")

    print()
    print("ELEVATION COST (flat vs hilly)")
    ec = elevation_cost(runs)
    if "note" in ec:
        print(f"  {ec['note']}")
    else:
        print(f"  Flat:  {_fmt_pace(ec['flat_pace'])}/mi @ HR {ec['flat_hr']} "
              f"({ec['flat_runs']} runs)")
        print(f"  Hilly: {_fmt_pace(ec['hilly_pace'])}/mi @ HR {ec['hilly_hr']} "
              f"({ec['hilly_runs']} runs)")
        print(f"  Cost: ~{ec['cost_sec_per_mi']} sec/mi and {ec['cost_bpm']:+} bpm — "
              "budget this on a non-flat course.")

    print()
    print("TREADMILL vs OUTDOOR")
    t = treadmill_vs_outdoor(runs)
    if "note" in t:
        print(f"  {t['note']}")
    else:
        print(f"  Indoor:  {_fmt_pace(t['indoor_pace'])}/mi @ HR {t['indoor_hr']} "
              f"({t['indoor_runs']} runs)")
        print(f"  Outdoor: {_fmt_pace(t['outdoor_pace'])}/mi @ HR {t['outdoor_hr']} "
              f"({t['outdoor_runs']} runs)")
        print("  If the paces differ at similar HR, prescribe by HR, not pace.")

    print()
    print("EFFORT EFFICIENCY (relative effort per mile, faster runs)")
    ee = effort_efficiency(runs)
    if "note" in ee:
        print(f"  {ee['note']}")
    else:
        print("  Most efficient:")
        for r in ee["most_efficient"]:
            print(f"    {r['date']} | {r['dist_mi']}mi | {_fmt_pace(r['pace'])}/mi | "
                  f"effort/mi {r['eff_per_mi']}")
        print("  Least efficient:")
        for r in ee["least_efficient"]:
            print(f"    {r['date']} | {r['dist_mi']}mi | {_fmt_pace(r['pace'])}/mi | "
                  f"effort/mi {r['eff_per_mi']}")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    print_trends()
