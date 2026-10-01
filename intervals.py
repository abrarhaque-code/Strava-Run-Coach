"""Interval-session parsing — recover the reps that were actually run.

Track sessions arrive with USELESS laps. The watch records auto mile (or km)
splits, so an 8 x 800m session shows up as five 1-mile laps and the rep
structure is invisible: a lap read sees "even splits, no fade" on a session
that fell apart two reps from the end. The reference fixture in the tests is a
real 8 x 800 that was first diagnosed only by hand-segmenting the stream.

The reps live in the stream, not the laps. This module finds them:

    streams = normalize_streams(raw)        # REST or MCP dialect, either way
    rs = detect_reps(streams, prescribed_reps=8, target_m=800)
    print(format_report(rs))

Segmentation strategy, in the order it degrades:

1. **Time gaps.** A watch that auto-pauses omits stationary samples, so
   `time[i] - time[i-1] > GAP_S` is a hard rep boundary. This is the cleanest
   signal and it is what the reference session had.
2. **The `moving` flag.** Present on Strava streams; False runs mark standing
   recovery even when sampling continues.
3. **A velocity floor.** For jogged recoveries neither of the above fires — the
   athlete never stops. Reps are still far faster than the jog, so the floor is
   set midway between the session's fast mode (p90) and its slow mode (p30).

They are tried in that order and the first one that finds structure wins.
Combining them is actively wrong: during a hard rep the velocity trace dips
below any useful floor several times a lap, so folding the floor into a mask
that already has clean gap boundaries shatters each rep into fragments. The
floor only earns its keep when there are no gaps to find.

Picking the work set out of the segments is the part that needs judgement.
Warm-up strides and drills are the same length as an abandoned final rep (in
the reference session the drills were 494m and 567m while the last rep was
473m), so distance alone cannot separate them. What does separate them is
**recovery uniformity**: the work set has metronomic recoveries (76-102s that
night) while the gap between the drills and rep 1 was 149s. So the anchor is distance-to-target and
the extension rule is recovery-matches-the-median. See `_select_work_set`.

Stdlib only, no I/O — callers hand in streams and get data back.
"""

import re
import statistics
from dataclasses import dataclass, field
from typing import Optional

# A stationary stretch at least this long ends a rep. Below it, a dropped GPS
# sample or a dodged pedestrian would fragment one rep into two.
GAP_S = 15

# Sub-samples this short are merged back into the neighbouring rep rather than
# being reported as their own segment.
MERGE_GAP_S = 5

MIN_SEG_M = 100
MIN_SEG_S = 20

# A segment counts as a core rep if its distance is within this fraction of the
# prescribed rep distance. Loose enough for GPS error on park loops (the
# reference reps measured 755-826m against an 800m target) and for a rep run long.
CORE_TOLERANCE = 0.25

# Having anchored on the core reps, a neighbouring segment joins the work set
# when its recovery is within this fraction of the median inter-rep recovery.
# This is what recovers an abandoned final rep without swallowing the drills.
RECOVERY_TOLERANCE = 0.5

# Two reps are "pace-matched" when their normalised times agree this closely.
# Comparing HR across such a pair is the single most informative number in an
# interval file: identical mechanical work, so any HR delta is fatigue.
PACE_MATCH_S = 6

# A rep this far off the target distance is reported as cut short.
SHORT_REP_FRACTION = 0.9


# ---------------------------------------------------------------------------
# Stream normalisation
# ---------------------------------------------------------------------------

# The REST API nests each stream under {"data": [...]} and spells it
# "heartrate"; the Strava MCP returns flat lists and spells it "heart_rate".
# Normalise once here so nothing downstream learns both dialects.
_STREAM_ALIASES = {
    "time": ("time",),
    "distance": ("distance",),
    "velocity": ("velocity_smooth", "velocity"),
    "heartrate": ("heartrate", "heart_rate"),
    "cadence": ("cadence",),
    "moving": ("moving",),
    "watts": ("watts",),
    "altitude": ("altitude",),
}


def normalize_streams(raw: dict) -> dict:
    """Translate either Strava stream dialect into flat lists.

    Returns {canonical_name: list}. Missing streams are omitted rather than
    filled with zeros, so callers can tell "no HR recorded" from "HR was 0".
    """
    if not isinstance(raw, dict):
        return {}
    if isinstance(raw.get("streams"), dict) and "time" not in raw:
        raw = raw["streams"]          # a merged activity carries them wrapped
    out = {}
    for canon, names in _STREAM_ALIASES.items():
        for name in names:
            if name not in raw:
                continue
            v = raw[name]
            if isinstance(v, dict):          # REST shape
                v = v.get("data")
            if isinstance(v, list) and v:
                out[canon] = v
                break
    return out


def _percentile(sorted_vals: list, q: float) -> float:
    if not sorted_vals:
        return 0.0
    i = max(0, min(len(sorted_vals) - 1, int(round(q * (len(sorted_vals) - 1)))))
    return sorted_vals[i]


def _velocity_floor(vel: list) -> float:
    """Midpoint between the session's fast mode and its slow mode.

    On a session with no two-mode structure (a steady easy run) p90 and p30
    sit close together and the floor lands mid-pack, producing one long
    segment — which is the correct answer for a run with no reps in it.
    """
    vs = sorted(v for v in vel if v is not None and v > 0)
    if not vs:
        return 0.0
    return (_percentile(vs, 0.90) + _percentile(vs, 0.30)) / 2.0


# Velocity is smoothed over this window before thresholding. Raw per-second
# velocity dips below any floor repeatedly inside a single hard rep, so
# thresholding it unsmoothed shatters one rep into a dozen fragments.
SMOOTH_WINDOW_S = 11


def _smooth(vals: list, window: int = SMOOTH_WINDOW_S) -> list:
    if not vals or window <= 1:
        return list(vals)
    half = window // 2
    clean = [(v if v is not None else 0.0) for v in vals]
    out = []
    for i in range(len(clean)):
        lo = max(0, i - half)
        hi = min(len(clean), i + half + 1)
        out.append(sum(clean[lo:hi]) / (hi - lo))
    return out


# ---------------------------------------------------------------------------
# Segmentation
# ---------------------------------------------------------------------------

@dataclass
class Segment:
    """One continuous stretch of running between two rests."""
    start_s: int
    end_s: int
    distance_m: float
    duration_s: int
    hr_start: Optional[int] = None
    hr_end: Optional[int] = None
    hr_max: Optional[int] = None
    hr_mean: Optional[float] = None
    cadence_spm: Optional[float] = None
    recovery_s: Optional[int] = None      # rest taken BEFORE this segment

    @property
    def pace_s_per_mi(self) -> float:
        if self.distance_m <= 0:
            return 0.0
        return self.duration_s / (self.distance_m / 1609.34)


def _runs_from_mask(t: list, n: int, active, gap_s: int) -> list:
    """Maximal [start, end] index runs where `active(i)` holds.

    A time gap always ends a run: the watch stopped recording, which is a rest
    whatever the mask says.
    """
    runs = []
    cur = None
    for i in range(n):
        if cur is not None and i > 0 and (t[i] - t[i - 1]) > gap_s:
            runs.append(cur)
            cur = None
        if active(i):
            if cur is None:
                cur = [i, i]
            else:
                cur[1] = i
        elif cur is not None:
            runs.append(cur)
            cur = None
    if cur is not None:
        runs.append(cur)
    # Stitch runs separated by only a blink of inactivity.
    merged = []
    for r in runs:
        if merged and (t[r[0]] - t[merged[-1][1]]) <= MERGE_GAP_S:
            merged[-1][1] = r[1]
        else:
            merged.append(list(r))
    return merged


def _viable(t, dist, runs) -> int:
    """How many of these runs are big enough to be reps."""
    return sum(1 for a, b in runs
               if (t[b] - t[a]) >= MIN_SEG_S and (dist[b] - dist[a]) >= MIN_SEG_M)


def segment_stream(streams: dict, gap_s: int = GAP_S) -> list:
    """Split a stream into the stretches where the athlete was actually running."""
    t = streams.get("time") or []
    dist = streams.get("distance") or []
    if len(t) < 2 or len(dist) < 2:
        return []

    vel = streams.get("velocity") or []
    hr = streams.get("heartrate") or []
    cad = streams.get("cadence") or []
    moving = streams.get("moving") or []
    n = min(len(t), len(dist))

    # Pass 1: time gaps and the moving flag only. Standing recoveries — the
    # common case on a track — resolve perfectly here.
    def active_stops(i: int) -> bool:
        return not (i < len(moving) and moving[i] is False)

    merged = _runs_from_mask(t, n, active_stops, gap_s)

    # Pass 2: only if pass 1 found no rep structure do we reach for the
    # velocity floor, which is the jogged-recovery case.
    if _viable(t, dist, merged) < 3 and vel:
        floor = _velocity_floor(vel)
        sm = _smooth(vel[:n])

        def active_vel(i: int) -> bool:
            if i < len(moving) and moving[i] is False:
                return False
            return i < len(sm) and sm[i] >= floor

        alt = _runs_from_mask(t, n, active_vel, gap_s)
        if _viable(t, dist, alt) > _viable(t, dist, merged):
            merged = alt

    segs = []
    prev_end_s = None
    for a, b in merged:
        duration = t[b] - t[a]
        travelled = dist[b] - dist[a]
        if duration < MIN_SEG_S or travelled < MIN_SEG_M:
            continue
        hrs = [x for x in hr[a:b + 1] if x] if hr else []
        cads = [x for x in cad[a:b + 1] if x and x > 30] if cad else []
        seg = Segment(
            start_s=t[a],
            end_s=t[b],
            distance_m=travelled,
            duration_s=duration,
            hr_start=hr[a] if hr and a < len(hr) else None,
            hr_end=hr[b] if hr and b < len(hr) else None,
            hr_max=max(hrs) if hrs else None,
            hr_mean=statistics.mean(hrs) if hrs else None,
            # Strava reports cadence one-legged; double it to steps per minute,
            # the unit runners and plans use.
            cadence_spm=statistics.mean(cads) * 2 if cads else None,
            recovery_s=(t[a] - prev_end_s) if prev_end_s is not None else None,
        )
        segs.append(seg)
        prev_end_s = t[b]
    return segs


# ---------------------------------------------------------------------------
# Prescription parsing
# ---------------------------------------------------------------------------

_DIST_UNITS = [
    (re.compile(r"^(\d+(?:\.\d+)?)\s*(?:m|meters?|metres?)$", re.I), 1.0),
    (re.compile(r"^(\d+(?:\.\d+)?)\s*k(?:m)?$", re.I), 1000.0),
    (re.compile(r"^(\d+(?:\.\d+)?)\s*(?:mi|mile|miles)$", re.I), 1609.34),
]

# "8 reps of 800m", "8 x 800m", "8x800", "6 x 1k", "4 reps of 1 mile".
_REP_RE = re.compile(
    r"(\d+)\s*(?:-\s*\d+\s*)?(?:reps?\s+of|x|×)\s*"
    r"(\d+(?:\.\d+)?\s*(?:m|meters?|metres?|k|km|mi|mile|miles)?)\b",
    re.I,
)


def _parse_distance(token: str) -> Optional[float]:
    token = token.strip()
    for rx, mult in _DIST_UNITS:
        m = rx.match(token)
        if m:
            return float(m.group(1)) * mult
    # A bare number in a rep spec means metres ("8x800").
    if re.match(r"^\d+(\.\d+)?$", token):
        v = float(token)
        return v if v >= 100 else None
    return None


def parse_prescription(text: str) -> list:
    """Pull ``[(count, rep_distance_m), ...]`` out of a workout description.

    Handles program phrasing ("8 reps of 800m", "16 reps of 200m", "4 reps of
    1 mile") and the shorthand runners type into Strava ("8 x 800m").
    Blocks are returned in the order they appear; a session built of several
    ("1600m at Tempo, then 4 reps of 800m") yields several entries and callers
    should take the one with the most total distance. Returns [] when there is
    no rep structure to find, which is the common case for easy runs.
    """
    if not text:
        return []
    out = []
    for m in _REP_RE.finditer(text):
        count = int(m.group(1))
        d = _parse_distance(m.group(2))
        # "4 sets of (800m, 400m, 200m)" and similar compound ladders are not
        # representable as one (count, distance) pair; skip rather than guess.
        if d and 1 <= count <= 40:
            out.append((count, d))
    return out


def primary_block(blocks: list) -> Optional[tuple]:
    """The block carrying the most distance — the session's main set."""
    if not blocks:
        return None
    return max(blocks, key=lambda b: b[0] * b[1])


# ---------------------------------------------------------------------------
# Work-set selection
# ---------------------------------------------------------------------------

@dataclass
class RepSet:
    reps: list = field(default_factory=list)          # list[Segment]
    target_m: Optional[float] = None
    prescribed_reps: Optional[int] = None
    warmup: Optional[Segment] = None
    other: list = field(default_factory=list)         # drills, cooldown, strides

    @property
    def completed(self) -> int:
        return len(self.reps)

    @property
    def work_m(self) -> float:
        return sum(r.distance_m for r in self.reps)

    def norm_s(self, rep: Segment) -> float:
        """Rep time scaled to the target distance.

        GPS on a park loop measured the reference reps at 755-826m for an
        800m target; comparing raw times across them is comparing different
        distances. Normalising is what makes rep 2 and rep 7 comparable.
        """
        if not self.target_m or rep.distance_m <= 0:
            return float(rep.duration_s)
        return rep.duration_s * self.target_m / rep.distance_m


def _median(vals: list) -> float:
    return statistics.median(vals) if vals else 0.0


def _select_work_set(segs: list, target_m: Optional[float],
                     prescribed: Optional[int]) -> tuple:
    """Split segments into (reps, warmup, other, target_m).

    Anchors on segments close to the target distance, then extends outward
    through neighbours whose recovery matches the work set's rhythm. See the
    module docstring for why recovery uniformity is the discriminator.

    `target_m` comes back out because it may have been inferred here, and the
    caller needs it to normalise rep times.
    """
    if not segs:
        return [], None, [], target_m

    if target_m is None:
        # No prescription: infer the rep distance from the faster-than-median
        # segments, which is where a rep set lives.
        med_pace = _median([s.pace_s_per_mi for s in segs if s.distance_m > 0])
        fast = [s for s in segs if s.pace_s_per_mi and s.pace_s_per_mi <= med_pace]
        if len(fast) < 2:
            return [], segs[0] if segs else None, segs[1:], target_m
        target_m = _median([s.distance_m for s in fast])

    lo = target_m * (1 - CORE_TOLERANCE)
    hi = target_m * (1 + CORE_TOLERANCE)
    core = [i for i, s in enumerate(segs) if lo <= s.distance_m <= hi]
    if not core:
        return [], segs[0] if segs else None, segs[1:], target_m

    # Keep the longest contiguous stretch of core indices — a rep set is
    # consecutive, and this drops a lone warm-up stride that happens to be
    # rep-length.
    best = [core[0]]
    run = [core[0]]
    for prev, cur in zip(core, core[1:]):
        if cur == prev + 1:
            run.append(cur)
        else:
            run = [cur]
        if len(run) > len(best):
            best = list(run)

    med_rec = _median([segs[i].recovery_s for i in best[1:]
                       if segs[i].recovery_s is not None])

    # Extend forward through segments whose recovery matches the rhythm. This
    # is what picks up a rep the athlete bailed on.
    end = best[-1]
    while end + 1 < len(segs) and (prescribed is None or len(best) < prescribed):
        rec = segs[end + 1].recovery_s
        if med_rec and rec is not None and abs(rec - med_rec) <= med_rec * RECOVERY_TOLERANCE:
            end += 1
            best.append(end)
        else:
            break

    # And backward, for a first rep run short or long.
    start = best[0]
    while start - 1 >= 1 and (prescribed is None or len(best) < prescribed):
        rec = segs[start].recovery_s
        if med_rec and rec is not None and abs(rec - med_rec) <= med_rec * RECOVERY_TOLERANCE:
            start -= 1
            best.insert(0, start)
        else:
            break

    idx = set(best)
    reps = [segs[i] for i in sorted(idx)]
    rest = [s for i, s in enumerate(segs) if i not in idx]
    # The warm-up is whatever ran before the first rep and went furthest.
    before = [s for s in rest if s.start_s < reps[0].start_s]
    warmup = max(before, key=lambda s: s.distance_m) if before else None
    other = [s for s in rest if s is not warmup]
    return reps, warmup, other, target_m


def detect_reps(streams: dict, prescribed_reps: Optional[int] = None,
                target_m: Optional[float] = None,
                prescription: Optional[str] = None) -> RepSet:
    """Find the reps in an interval session.

    Pass `prescription` (free text from the plan or the activity title) to let
    the prescribed rep count and distance anchor detection, or pass
    `prescribed_reps`/`target_m` directly. With neither, the rep distance is
    inferred from the session's own structure.
    """
    if prescription and (prescribed_reps is None or target_m is None):
        blk = primary_block(parse_prescription(prescription))
        if blk:
            prescribed_reps = prescribed_reps or blk[0]
            target_m = target_m or blk[1]

    segs = segment_stream(normalize_streams(streams) if "time" not in streams
                          else streams)
    reps, warmup, other, target_m = _select_work_set(segs, target_m,
                                                     prescribed_reps)
    return RepSet(reps=reps, target_m=target_m, prescribed_reps=prescribed_reps,
                  warmup=warmup, other=other)


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def pace_matched_pair(rs: RepSet) -> Optional[dict]:
    """The most separated pair of reps run at the same pace.

    Identical mechanical work at two points in the session isolates fatigue
    from everything else: no assumptions about conditions, fuel or pacing.
    On the reference session this returned rep 2 vs rep 7 (3:52 vs 3:54):
    +17 bpm at the start, +10 at the end.
    """
    reps = rs.reps
    best = None
    for i in range(len(reps)):
        for j in range(i + 1, len(reps)):
            a, b = reps[i], reps[j]
            if abs(rs.norm_s(a) - rs.norm_s(b)) > PACE_MATCH_S:
                continue
            if a.hr_end is None or b.hr_end is None:
                continue
            # Rep-start HR is only comparable when the two reps were preceded
            # by comparable rests. Rep 1 typically follows a long stand after
            # the warm-up or drills, so pairing it against a mid-set rep would
            # book that extra standing time as fatigue.
            # Measured against the SHORTER rest: rep 1's 150s stand after the
            # drills vs a mid-set 92s is a 63% overshoot on the rest that
            # matters, and booking it as fatigue would overstate the delta.
            start_ok = (a.hr_start is not None and b.hr_start is not None
                        and a.recovery_s and b.recovery_s
                        and abs(a.recovery_s - b.recovery_s)
                        <= min(a.recovery_s, b.recovery_s) * RECOVERY_TOLERANCE)
            if best is None or (j - i) > (best["gap"]):
                best = {
                    "gap": j - i,
                    "early_idx": i + 1, "late_idx": j + 1,
                    "early_norm_s": rs.norm_s(a), "late_norm_s": rs.norm_s(b),
                    "hr_start_delta": (b.hr_start - a.hr_start) if start_ok else None,
                    "hr_end_delta": b.hr_end - a.hr_end,
                }
    return best


def fade(rs: RepSet, tail: int = 2) -> Optional[dict]:
    """How much slower the closing reps ran than the session's best."""
    reps = rs.reps
    if len(reps) < tail + 2:
        return None
    norms = [rs.norm_s(r) for r in reps]
    best = sorted(norms)[:tail]
    last = norms[-tail:]
    return {
        "best_mean_s": statistics.mean(best),
        "last_mean_s": statistics.mean(last),
        "delta_s": statistics.mean(last) - statistics.mean(best),
        "tail": tail,
    }


def recovery_drift(rs: RepSet) -> Optional[dict]:
    """Rep-start HR from first to last — how well recovery kept up."""
    hrs = [r.hr_start for r in rs.reps if r.hr_start is not None]
    if len(hrs) < 3:
        return None
    return {"first": hrs[0], "last": hrs[-1], "delta": hrs[-1] - hrs[0],
            "series": hrs}


def short_reps(rs: RepSet) -> list:
    """1-based indices of reps that came up short of the target distance."""
    if not rs.target_m:
        return []
    return [i for i, r in enumerate(rs.reps, 1)
            if r.distance_m < rs.target_m * SHORT_REP_FRACTION]


def compliance(rs: RepSet) -> dict:
    """Rep-level compliance, for the weekly check-in.

    `work_pct` is distance actually run in the work set over distance
    prescribed — the number that says "96% of the session" rather than the
    weekly-mileage number, which cannot see a session at all.
    """
    prescribed_m = ((rs.prescribed_reps or rs.completed) * rs.target_m
                    if rs.target_m else 0.0)
    short = short_reps(rs)
    return {
        "completed": rs.completed,
        # A rep bailed on at 472m of 800m was started, not completed. Scoring
        # off `completed` alone would call the reference session a clean 8 for 8.
        "completed_full": rs.completed - len(short),
        "prescribed": rs.prescribed_reps,
        "target_m": rs.target_m,
        "work_m": rs.work_m,
        "prescribed_m": prescribed_m,
        "work_pct": (rs.work_m / prescribed_m * 100) if prescribed_m else None,
        "short_reps": short,
        "fade": fade(rs),
        "pace_matched": pace_matched_pair(rs),
        "recovery": recovery_drift(rs),
    }


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def fmt_clock(seconds: float) -> str:
    if seconds is None or seconds <= 0:
        return "--"
    seconds = int(round(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


def fmt_pace(sec_per_mi: float) -> str:
    """Pace in the athlete's unit (see units.fmt_pace); '--' when unknown."""
    if not sec_per_mi:
        return "--"
    import units
    return units.fmt_pace(sec_per_mi / 60.0)


def target_label(target_m: Optional[float]) -> str:
    if not target_m:
        return "rep"
    if abs(target_m - 1609.34) < 20:
        return "mile"
    if target_m >= 1000 and target_m % 1000 == 0:
        return f"{int(target_m / 1000)}k"
    return f"{int(round(target_m))}m"


def format_report(rs: RepSet) -> list:
    """Human-readable rep table plus the three numbers that explain a session."""
    if not rs.reps:
        return []
    lbl = target_label(rs.target_m)
    lines = []
    head = f"  REPS  ({rs.completed}"
    if rs.prescribed_reps:
        head += f" of {rs.prescribed_reps}"
    head += f" x {lbl})"
    lines.append(head)
    lines.append(f"    {'#':>2}  {'dist':>7}  {'time':>6}  {lbl + '-eq':>8}  "
                 f"{'pace':>9}  {'HR':>10}  {'cad':>5}  {'rec':>5}")
    for i, r in enumerate(rs.reps, 1):
        hr = "--"
        if r.hr_start is not None and r.hr_end is not None:
            hr = f"{r.hr_start}->{r.hr_end}"
        lines.append(
            f"    {i:>2}  {r.distance_m:>6.0f}m  {fmt_clock(r.duration_s):>6}  "
            f"{fmt_clock(rs.norm_s(r)):>8}  {fmt_pace(r.pace_s_per_mi):>9}  "
            f"{hr:>10}  {(f'{r.cadence_spm:.0f}' if r.cadence_spm else '--'):>5}  "
            f"{(f'{r.recovery_s}s' if r.recovery_s is not None else '--'):>5}"
        )

    c = compliance(rs)
    if c["work_pct"] is not None:
        lines.append(f"    work: {rs.work_m:.0f}m of {c['prescribed_m']:.0f}m "
                     f"prescribed ({c['work_pct']:.0f}%)")
    sr = c["short_reps"]
    if sr:
        lines.append(f"    cut short: rep{'s' if len(sr) > 1 else ''} "
                     f"{', '.join(str(x) for x in sr)}")

    f = c["fade"]
    if f and f["delta_s"] >= 3:
        lines.append(f"    fade: last {f['tail']} reps ran "
                     f"{f['delta_s']:.0f}s slower per {lbl} than the best "
                     f"{f['tail']} ({fmt_clock(f['best_mean_s'])} -> "
                     f"{fmt_clock(f['last_mean_s'])})")

    pm = c["pace_matched"]
    if pm:
        bits = []
        if pm["hr_start_delta"] is not None:
            bits.append(f"{pm['hr_start_delta']:+d} bpm at the start")
        bits.append(f"{pm['hr_end_delta']:+d} bpm at the end")
        lines.append(f"    pace-matched: rep {pm['early_idx']} vs rep "
                     f"{pm['late_idx']} both {fmt_clock(pm['early_norm_s'])} "
                     f"-- {', '.join(bits)} (that delta is fatigue, nothing else)")

    rec = c["recovery"]
    if rec and rec["delta"] >= 10:
        lines.append(f"    recovery: rep-start HR climbed {rec['first']} -> "
                     f"{rec['last']} ({rec['delta']:+d}); arriving less "
                     f"recovered each rep")
    return lines
