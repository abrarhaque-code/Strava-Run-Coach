"""Is the heart-rate trace trustworthy? Checks that run in both directions.

Wrist optical HR fails in known ways: it lags a pace change by 30-60 s, takes
a while to re-acquire after a stop (reading far too LOW on the resume), and
can lock onto step rate and report cadence as heart rate (sitting exactly
where a marathon-effort HR would sit). A high reading and a low one get the
same scrutiny here, and each finding is evidence, not a verdict: the notes say
"unverifiable from one run" because that is the truth of it.

Input is a normalized stream (see intervals.normalize_streams): flat lists
under "time", "heartrate", and optionally "moving" and "cadence". Imports
only `config` (for the sensor wording), so any module can use it.
"""

import statistics
from typing import Optional

import config

STOP_MIN_S = 20          # a sample gap or still run this long is a stop
RAMP_SKIP_S = 75         # after a resume, HR is still climbing back (wrist lag)
REACQ_STOP_S = 60        # only stops at least this long can show re-acquisition
REACQ_SLOW_S = 75        # still >10 bpm under the pre-stop level after this long
LOCK_TOL = 3.0           # bpm within 2x cadence that counts as "close"
LOCK_MIN_S = 600         # a close stretch shorter than this is coincidence


def _stops(t: list, mv: list) -> list:
    """[(stop_start_s, resume_s)] from sample gaps and still runs."""
    out = []
    n = len(t)
    still = None
    for i in range(1, n):
        if t[i] - t[i - 1] >= STOP_MIN_S:
            out.append((t[i - 1], t[i]))
        if mv and i < len(mv):
            if mv[i] is False and still is None:
                still = t[i]
            elif mv[i] is not False and still is not None:
                if t[i] - still >= STOP_MIN_S:
                    out.append((still, t[i]))
                still = None
    out.sort()
    merged = []
    for a, b in out:
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(b, merged[-1][1]))
        else:
            merged.append((a, b))
    return merged


def _window_mean(t, y, lo, hi, mv=None):
    """Time-weighted mean of y over [lo, hi), skipping stops and non-moving
    samples. -> (mean or None, seconds covered)."""
    acc = tot = 0.0
    for i in range(1, len(t)):
        if not (lo <= t[i] < hi) or t[i] - t[i - 1] >= STOP_MIN_S:
            continue
        if mv and i < len(mv) and mv[i] is False:
            continue
        if y[i] is None:
            continue
        dt = t[i] - t[i - 1]
        acc += y[i] * dt
        tot += dt
    return (acc / tot, tot) if tot else (None, 0.0)


def _corr(x, y) -> Optional[float]:
    if len(x) < 10 or len(x) != len(y):
        return None
    try:
        return statistics.correlation(x, y)
    except (statistics.StatisticsError, AttributeError, ValueError):
        return None


def _fmt_clock(s: float) -> str:
    s = int(s)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def _cadence_lock(t, hr, cad, mv) -> dict:
    """Longest stretch with |HR - 2*cadence| <= LOCK_TOL and whether HR
    co-moves with it.

    Closeness alone is coincidence: HR crosses 155-170 while step rate sits
    near 162 on many runners. A locked sensor reproduces cadence with no lag;
    a real heart lags a pace change by 30-60 s. So the test is the lag-0
    correlation of 30-s changes against the +30 s lag, inside the close stretch.
    """
    grid, h, c = [], [], []
    j = 0
    for s in range(int(t[0]), int(t[-1]), 5):
        while j + 1 < len(t) and t[j + 1] <= s:
            j += 1
        if mv and j < len(mv) and mv[j] is False:
            grid.append(None)
            continue
        if hr[j] is None or not cad[j]:
            grid.append(None)
            continue
        grid.append(s)
        h.append(float(hr[j]))
        c.append(2.0 * float(cad[j]))
    best = (0, 0, 0)
    run_start = None
    k = 0
    idx_map = []
    for g in grid:
        if g is None:
            idx_map.append(None)
            continue
        idx_map.append(k)
        k += 1
    close = [None if m is None else abs(h[m] - c[m]) <= LOCK_TOL for m in idx_map]
    for i, ok in enumerate(close + [False]):
        if ok and run_start is None:
            run_start = i
        elif not ok and run_start is not None:
            span = (i - run_start) * 5
            if span > best[0]:
                best = (span, run_start, i)
            run_start = None
    span, a, b = best
    out = {"flagged": False, "longest_s": span}
    if span < LOCK_MIN_S:
        return out
    ks = [idx_map[i] for i in range(a, b) if idx_map[i] is not None]
    hh = [h[m] for m in ks]
    cc = [c[m] for m in ks]
    lag = 6  # 30 s on the 5-s grid
    dh = [hh[i + lag] - hh[i] for i in range(len(hh) - lag)]
    dc = [cc[i + lag] - cc[i] for i in range(len(cc) - lag)]
    r0 = _corr(dh, dc)
    r_lag = _corr(dh[lag:], dc[:-lag]) if len(dh) > lag else None
    out.update({"start_s": grid[a] if grid[a] is not None else a * 5,
                "end_s": grid[b - 1] if grid[b - 1] is not None else b * 5,
                "r0": r0, "r_lag": r_lag})
    out["flagged"] = bool(r0 is not None and r0 > 0.5 and (r_lag is None or r0 > r_lag))
    return out


def hr_quality(norm: dict, parsed_laps: Optional[list] = None) -> dict:
    """Sensor checks on a normalized stream.

    Returns {"available", "stops", "ramps", "slow_reacq", "lock",
    "lap_hr": {idx: moving HR without post-resume ramps}, "notes": [...],
    "unreliable": bool, "flag": one-line summary or None}.
    """
    t = norm.get("time") or []
    hr = norm.get("heartrate") or []
    if len(t) < 30 or len(hr) != len(t):
        return {"available": False, "notes": [], "unreliable": False, "flag": None}
    mv = norm.get("moving") or []
    cad = norm.get("cadence") if norm.get("cadence") and len(norm["cadence"]) == len(t) else None
    stops = _stops(t, mv)
    notes, reasons = [], []

    # Post-resume ramps: HR is still coming back for a while after every stop.
    ramps = [(b, b + RAMP_SKIP_S) for a, b in stops]
    slow = []
    for a, b in stops:
        if b - a < REACQ_STOP_S:
            continue
        pre, _ = _window_mean(t, hr, a - 120, a, mv)
        if pre is None:
            continue
        back = None
        for i in range(len(t)):
            if t[i] >= b and hr[i] is not None and hr[i] >= pre - 10:
                back = t[i] - b
                break
            if t[i] > b + 240:
                break
        if back is None or back > REACQ_SLOW_S:
            slow.append({"at_s": b, "pre_hr": pre, "back_s": back})
    if slow:
        notes.append(f"{len(slow)} stop(s) where HR took over {REACQ_SLOW_S}s to come back; "
                     "those ramps are left out of lap HR (sensor re-acquisition or a real "
                     "slow return, unverifiable from one run).")
        if len(slow) >= 2:
            reasons.append(f"slow re-acquisition after {len(slow)} stops")

    lock = _cadence_lock(t, hr, cad, mv) if cad else {"flagged": False, "longest_s": 0}
    if lock.get("flagged"):
        notes.append(f"HR tracked step rate within {LOCK_TOL:.0f} bpm and moved with it, "
                     f"no lag, for {lock['longest_s'] / 60:.0f} min "
                     f"({_fmt_clock(lock['start_s'])}-{_fmt_clock(lock['end_s'])}): "
                     "likely cadence lock, HR there is unverifiable.")
        reasons.append(f"cadence lock for {lock['longest_s'] / 60:.0f} min")
    elif lock.get("longest_s", 0) >= LOCK_MIN_S:
        notes.append(f"HR sat within {LOCK_TOL:.0f} bpm of step rate for "
                     f"{lock['longest_s'] / 60:.0f} min but did not move with it: "
                     "coincidence, not lock; kept.")

    lap_hr = {}
    if parsed_laps:
        start = 0.0
        for lp in parsed_laps:
            el = lp.get("elapsed_s") or lp.get("moving_s") or 0
            end = start + el
            acc = tot = 0.0
            for i in range(1, len(t)):
                if not (start <= t[i] < end) or t[i] - t[i - 1] >= STOP_MIN_S:
                    continue
                if mv and i < len(mv) and mv[i] is False:
                    continue
                if any(r0 <= t[i] < r1 for r0, r1 in ramps):
                    continue
                if lock.get("flagged") and lock["start_s"] <= t[i] <= lock["end_s"]:
                    continue
                dt = t[i] - t[i - 1]
                acc += hr[i] * dt
                tot += dt
            if tot >= 60:
                lap_hr[lp.get("idx")] = acc / tot
            start = end

    unreliable = bool(reasons)
    flag = None
    if unreliable:
        source = config.hr_source()
        who = "Wrist HR" if source.lower().startswith("w") else "The HR trace"
        flag = f"{who} looks unreliable here: " + "; ".join(reasons) + "."
    return {"available": True, "stops": stops, "ramps": ramps, "slow_reacq": slow,
            "lock": lock, "lap_hr": lap_hr, "notes": notes,
            "unreliable": unreliable, "flag": flag}
