"""What the athlete said they would run, read from their own Strava description.

A long run titled "midweek long run" and described "13 easy, 3 hmp, 2 easy"
should be judged against THAT, not against whatever the plan had pencilled in
for the day. When the description spells the session out, it is the session
to grade execution against; the plan stays a separate compliance line.

Rep sessions ("7 x 1k", "16 x 400") are not segments: `intervals` owns them.
Distances are read in the athlete's configured unit unless a suffix says
otherwise ("5k", "3 mi"); every segment comes back in miles, the engine's
internal unit.
"""

from __future__ import annotations

import re
from typing import Optional

import units

# label -> segment kind
_LABELS = (
    (r"easy|ez|e|wu|warm[\s-]?up|cd|cool[\s-]?down|recovery|rec|jog|shakeout", "easy"),
    (r"steady|moderate|mod", "steady"),
    (r"hmp|hm\s*pace|hm|half(?:\s*marathon)?(?:\s*pace)?", "half"),
    (r"mp|marathon(?:\s*pace)?|m\s*pace", "mp"),
    (r"tempo|t|threshold|lt", "tempo"),
    (r"10k(?:\s*pace)?", "10k"),
    (r"5k(?:\s*pace)?", "5k"),
    (r"prog(?:ression)?|progressive", "progression"),
)
_LABEL_RE = "|".join(f"(?:{p})" for p, _ in _LABELS)
_NUM = r"(\d+(?:\.\d+)?)"
_UNIT = r"\s*(mi(?:les?)?|m|km|k)?\s*"
_SEG_RE = re.compile(rf"^\s*{_NUM}{_UNIT}(?:@|at)?\s*({_LABEL_RE})\b", re.I)
_EMBED_RE = re.compile(
    rf"{_NUM}{_UNIT}(?:\w+\s+)?(?:w/|with)\s*(?:the\s+)?(last|final|first|opening)?\s*{_NUM}{_UNIT}"
    rf"(?:@|at)\s*({_LABEL_RE})\b", re.I)
_REPS_RE = re.compile(r"\d+\s*[x×]\s*\d", re.I)
QUALITY_KINDS = ("steady", "half", "mp", "tempo", "10k", "5k", "progression")


def _kind(label: str) -> Optional[str]:
    lab = label.strip().lower()
    for pat, kind in _LABELS:
        if re.fullmatch(pat, lab, re.I):
            return kind
    return None


def _mi(value: float, suffix: Optional[str], unit: str) -> float:
    """A typed distance -> miles. An explicit suffix wins; a bare number is in
    `unit`; a bare 'm' means miles (the way runners abbreviate it)."""
    suf = (suffix or "").lower()
    if suf in ("k", "km"):
        return value / units.KM_PER_MI
    if suf.startswith("m"):
        return value
    return units.to_mi(value, unit)


def _tolerance_ok(total_mi: float, run_mi: float, unit: str) -> bool:
    one_unit = units.to_mi(1.0, unit)
    return abs(total_mi - run_mi) <= max(one_unit, 0.10 * run_mi)


def parse_intent(text: Optional[str], run_mi: float, unit: Optional[str] = None) -> Optional[dict]:
    """Segments the description commits to, or None.

    Returns {"source": "description", "text", "unit", "segments": [{"start_mi",
    "end_mi", "kind", "miles", "placed"}]}. `placed` is False for an embedded
    block ("20 w/ 10 @ MP") whose position the review must find in the laps;
    "last"/"final" places it at the end, "first"/"opening" at the start.
    `unit` is the unit bare numbers are typed in (default: the configured one).
    """
    if not text or not run_mi:
        return None
    unit = unit or units.unit()
    t = str(text)
    if _REPS_RE.search(t):
        return None
    m = _EMBED_RE.search(t)
    if m:
        total = _mi(float(m.group(1)), m.group(2), unit)
        where = (m.group(3) or "").lower()
        inner = _mi(float(m.group(4)), m.group(5), unit)
        kind = _kind(m.group(6))
        if kind and _tolerance_ok(total, run_mi, unit) and 0 < inner < total:
            if where in ("first", "opening"):
                segs = [(0.0, inner, kind, True), (inner, run_mi, "easy", True)]
            elif where in ("last", "final"):
                segs = [(0.0, run_mi - inner, "easy", True), (run_mi - inner, run_mi, kind, True)]
            else:
                segs = [(None, None, kind, False)]
            return {"source": "description", "text": t.strip(), "unit": unit,
                    "segments": [_seg(a, b, k, p, inner if k == kind else None)
                                 for a, b, k, p in segs]}
    parts = [p for p in re.split(r"[,;+\n/]|\bthen\b|\band\b", t, flags=re.I) if p.strip()]
    segs, pos = [], 0.0
    for p in parts:
        sm = _SEG_RE.match(p)
        if not sm:
            continue
        miles = _mi(float(sm.group(1)), sm.group(2), unit)
        kind = _kind(sm.group(3))
        if not kind or miles <= 0:
            continue
        segs.append(_seg(pos, pos + miles, kind, True, miles))
        pos += miles
    if not segs or not _tolerance_ok(pos, run_mi, unit):
        return None
    # Stretch the plan to the run: GPS reads long, so a stated "18" may log 18.1.
    scale = run_mi / pos
    for s in segs:
        s["start_mi"] *= scale
        s["end_mi"] *= scale
    return {"source": "description", "text": t.strip(), "unit": unit, "segments": segs}


def _seg(a, b, kind, placed, miles=None) -> dict:
    return {"start_mi": a, "end_mi": b, "kind": kind, "placed": placed,
            "miles": miles if miles is not None else ((b - a) if a is not None else None)}


def quality_segments(intent: Optional[dict]) -> list:
    return [s for s in (intent or {}).get("segments", []) if s["kind"] in QUALITY_KINDS]


def summary(intent: Optional[dict]) -> str:
    """'13 easy, 3 half, 2 easy' in the unit the athlete typed."""
    if not intent:
        return ""
    unit = intent.get("unit") or units.unit()
    bits = []
    for s in intent["segments"]:
        n = s.get("miles")
        if n:
            shown = n if unit.startswith("mi") else n * units.KM_PER_MI
            bits.append(f"{round(shown, 2):g} {s['kind']}")
        else:
            bits.append(s["kind"])
    return ", ".join(bits)
