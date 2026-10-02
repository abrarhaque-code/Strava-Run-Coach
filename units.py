"""Display units: miles or kilometres, chosen by `config.athlete.units`.

The engine's internal canonical never changes: distances in metres (cache) or
miles (plan JSON, config paces), paces in minutes per mile. Every formula,
the CSV seam and both the private and public plan files already agree on
that, so this module converts only at the edges: when a number is printed
(`fmt_dist`, `fmt_pace`) and when a user types one (`parse_dist`,
`parse_pace`). Nothing here touches the math.

Imports only `config`, so it can be used from any module without a cycle.
"""

import math
import re
from typing import Optional

import config

MI_M = 1609.34
KM_M = 1000.0
KM_PER_MI = 1.609344

_DIST_RE = re.compile(
    r"^\s*([0-9]*\.?[0-9]+)\s*(mi|mile|miles|k|km|kms|kilometers|kilometres)?\s*$",
    re.IGNORECASE,
)
_PACE_RE = re.compile(
    r"^\s*(\d{1,2}):(\d{2})\s*(?:/\s*)?(mi|mile|k|km)?\s*$", re.IGNORECASE)


def unit() -> str:
    """'mi' or 'km' (anything starting with 'k' means kilometres)."""
    try:
        u = str(config.units() or "mi").strip().lower()
    except Exception:
        u = "mi"
    return "km" if u.startswith("k") else "mi"


def unit_label() -> str:
    return unit()


def pace_label() -> str:
    return "/" + unit()


def volume_label() -> str:
    """Weekly-volume shorthand: 'mpw' for miles, 'km/wk' for kilometres."""
    return "mpw" if unit() == "mi" else "km/wk"


def mi_to_user(mi: float) -> float:
    return mi if unit() == "mi" else mi * KM_PER_MI


def user_to_mi(x: float) -> float:
    return x if unit() == "mi" else x / KM_PER_MI


def to_mi(value: float, u: Optional[str] = None) -> float:
    """Convert a distance stated in unit `u` ('mi', 'km', 'k'; default the
    user's unit) into miles."""
    u = (u or unit()).strip().lower()
    return value if u.startswith("mi") else value / KM_PER_MI


def dist(m: float) -> float:
    """Metres -> distance in the user's unit."""
    return m / (MI_M if unit() == "mi" else KM_M)


def fmt_dist(m: Optional[float] = None, *, mi: Optional[float] = None,
             nd: int = 1, label: bool = True) -> str:
    """Format a distance given in metres (`m`) or miles (`mi`)."""
    if mi is None:
        if m is None:
            return "N/A"
        mi = m / MI_M
    v = mi_to_user(mi)
    s = f"{v:.{nd}f}"
    return f"{s} {unit()}" if label else s


def fmt_pace(min_per_mi: Optional[float] = None, *,
             sec_per_m: Optional[float] = None, label: bool = True) -> str:
    """Format a pace as M:SS in the user's unit. Input is minutes per mile
    (or seconds per metre). Seconds are rounded; :60 rolls over."""
    if sec_per_m is not None:
        min_per_mi = sec_per_m * MI_M / 60.0
    if min_per_mi is None or not math.isfinite(min_per_mi) or min_per_mi <= 0:
        return "N/A"
    per_unit = min_per_mi if unit() == "mi" else min_per_mi / KM_PER_MI
    total = int(round(per_unit * 60))
    m, s = divmod(total, 60)
    out = f"{m}:{s:02d}"
    return out + pace_label() if label else out


def fmt_pace_range(lo_min_per_mi: float, hi_min_per_mi: float,
                   label: bool = True) -> str:
    """'9:00-9:30/mi' (faster first) from two min/mi values."""
    a, b = sorted((lo_min_per_mi, hi_min_per_mi))
    out = f"{fmt_pace(a, label=False)}-{fmt_pace(b, label=False)}"
    return out + pace_label() if label else out


def round_dist(mi: float, step: float = 0.5) -> float:
    """Round a distance in miles to `step` of the USER's unit; returns miles."""
    v = mi_to_user(mi)
    r = round(v / step) * step
    return user_to_mi(r)


def parse_dist(text) -> float:
    """'25', '25mi', '40km', '40 k' -> miles. Bare numbers use the user's unit."""
    if isinstance(text, (int, float)):
        return to_mi(float(text))
    m = _DIST_RE.match(str(text))
    if not m:
        raise ValueError(f"not a distance: {text!r}")
    value = float(m.group(1))
    suffix = (m.group(2) or "").lower()
    if not suffix:
        return to_mi(value)
    return value if suffix.startswith("mi") else value / KM_PER_MI


def parse_pace(text) -> float:
    """'6:13/km', '10:00', '10:00/mi' -> minutes per mile. A bare M:SS uses
    the user's unit."""
    m = _PACE_RE.match(str(text))
    if not m:
        raise ValueError(f"not a pace: {text!r}")
    minutes = int(m.group(1)) + int(m.group(2)) / 60.0
    suffix = (m.group(3) or "").lower()
    if not suffix:
        suffix = unit()
    return minutes if suffix.startswith("mi") else minutes * KM_PER_MI


def per_mi_to_user(x: float) -> float:
    """A per-mile rate (seconds per mile, effort per mile) in the user's unit."""
    return x if unit() == "mi" else x / KM_PER_MI
