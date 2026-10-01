"""Activity enrichment pipeline.

Pre-computes analytical fields at ingest time so coach modules read instead of
recompute. Each enricher takes an activity dict and returns it with new fields
under the reserved `_*` namespace (won't collide with Strava's fields).

Inspired by statistics-for-strava ActivityImportPipeline (we steal the idea,
not the Symfony abstraction).

Usage:
    from enrichment import enrich, needs_enrichment, ENRICHMENT_VERSION

    activity = api.get_activity(123)
    activity = enrich(activity)
    # Now has _enriched_v, _workout_type, _pace_zone, _run_tss, etc.
"""


import config


# Bump this when enrichers are added or their logic changes.
# All cached activities with a lower _enriched_v will be re-enriched on next sync.
ENRICHMENT_VERSION = 4

# Tolerance around the bike-equivalence speed signature. The signature itself
# is derived from config (see bike_equiv_mps) so the "N min bike = 1 mi"
# convention stays a single knob.
BIKE_EQUIV_EPS = 0.01


# ---------------------------------------------------------------------------
# Activity classification (single source of truth for "is this a real run?")
# ---------------------------------------------------------------------------

def bike_equiv_mps() -> float:
    """Speed signature of a bike session manually logged as a Run.

    Athletes who log stationary-bike work as manual "Run" entries encode a
    time-for-distance convention (config crosstrain.bike_min_per_mi, e.g.
    10 min bike = 1 mi => exactly 6.0 mph). Manual entries carry no max_speed,
    so this exact average speed is the discriminator.
    """
    min_per_mi = config.crosstrain_cfg().get("bike_min_per_mi", 10.0)
    return 1609.34 / (min_per_mi * 60.0)


def looks_like_bike_equiv(avg_speed, max_speed, distance, moving_time) -> bool:
    """True if the numbers carry the manual bike-as-run signature.

    Shared with mcp_adapter so both ingest paths (REST sync and MCP JSON)
    agree on what a fake run looks like: a manual entry (max_speed absent or
    zero — real treadmill runs record max_speed > 0) whose average speed sits
    exactly on the configured bike-equivalence speed.
    """
    if _as_float(max_speed):
        return False
    signature = bike_equiv_mps()
    candidates = [_as_float(avg_speed)]
    dist = _as_float(distance) or 0
    moving = _as_float(moving_time) or 0
    if dist > 0 and moving > 0:
        candidates.append(dist / moving)
    return any(v and abs(v - signature) <= BIKE_EQUIV_EPS for v in candidates)


def classify_activity(activity: dict) -> str:
    """Classify an activity for downstream metric eligibility.

    Returns one of:
        "run"           — real outdoor run
        "treadmill_run" — real run on a treadmill (counts as run)
        "bike_equiv"    — bike session manually logged as Run at the
                          configured equivalence speed
        "invalid"       — Run with zero distance or zero moving time
        "ride"          — Ride / VirtualRide / mcp-typed CrossTrain
        "other"         — anything else (walks, weight training, ...)
    """
    atype = activity.get("type")
    if atype in ("Ride", "VirtualRide", "CrossTrain"):
        # CrossTrain is mcp_adapter's ingest-time rewrite of bike-as-run and
        # real rides; both belong in the cross-training TSS bucket.
        return "ride"
    if atype != "Run":
        return "other"

    dist = activity.get("distance", 0) or 0
    moving = activity.get("moving_time", 0) or 0
    if dist <= 0 or moving <= 0:
        return "invalid"

    max_speed = _as_float(activity.get("max_speed")) or 0
    if not max_speed:
        # Manual entry. Check the signature on the average_speed field AND on
        # computed speed (CSV rows may carry only one of them).
        if looks_like_bike_equiv(activity.get("average_speed"), None, dist, moving):
            return "bike_equiv"
        return "run"  # manual but not the bike signature — trust it

    if activity.get("trainer"):
        return "treadmill_run"
    return "run"


def is_real_run(activity: dict) -> bool:
    """True if the activity should count toward run mileage/metrics.

    Uses the pre-computed _activity_class when present (cache path), else
    classifies on the fly (CSV path). Soft-deleted activities never count.
    """
    if activity.get("_deleted_at"):
        return False
    cls = activity.get("_activity_class") or classify_activity(activity)
    return cls in ("run", "treadmill_run")


def _as_float(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Individual enrichers — each is a pure function: dict -> dict
# ---------------------------------------------------------------------------

def attach_activity_class(activity: dict) -> dict:
    """Stamp the classification so consumers read instead of recompute."""
    cls = classify_activity(activity)
    activity["_activity_class"] = cls
    activity["_is_real_run"] = cls in ("run", "treadmill_run")
    if cls == "bike_equiv":
        # The logged distance already encodes the equivalence convention; keep
        # it as cross-training credit, never as run mileage.
        activity["_bike_equiv_mi"] = round((activity.get("distance", 0) or 0) / 1609.34, 2)
    return activity


def attach_pace_zone(activity: dict) -> dict:
    """Classify the dominant pace zone of a run."""
    if activity.get("type") != "Run":
        return activity
    if not is_real_run(activity):
        activity["_pace_zone"] = "n/a"
        return activity

    dist_m = activity.get("distance", 0) or 0
    moving_s = activity.get("moving_time", 0) or 0
    if dist_m == 0 or moving_s == 0:
        activity["_pace_zone"] = "unknown"
        return activity

    pace = (moving_s / 60) / (dist_m / 1609.34)  # min/mi
    avg_hr = activity.get("average_heartrate")

    # HR-based when available (more reliable than pace alone)
    if avg_hr:
        if avg_hr < config.recovery_hr_cap():
            zone = "recovery"
        elif avg_hr < config.easy_hr_cap():
            zone = "easy"
        elif avg_hr < config.tempo_hr_upper():
            zone = "tempo"
        elif avg_hr < config.threshold_hr_upper():
            zone = "threshold"
        else:
            zone = "vo2max"
    else:
        # Pace fallback
        if pace >= 13:
            zone = "walk"
        elif pace >= 10:
            zone = "easy"
        elif pace >= 9:
            zone = "tempo"
        elif pace >= 8:
            zone = "threshold"
        else:
            zone = "vo2max"

    activity["_pace_zone"] = zone
    return activity


# ---------------------------------------------------------------------------
# What kind of run was it? (single source of truth; the review and the
# predictor read this too, so the cache, the debrief and VDOT always agree)
# ---------------------------------------------------------------------------

# A rep session spends a third of its elapsed time standing: moving/elapsed
# under this ratio on a run under INTERVAL_MAX_DIST_MI is the shape of a track
# night. The distance guard is load-bearing: a 3-hour long run through traffic
# lights can dip under the ratio too.
INTERVAL_MOVING_RATIO = 0.80
INTERVAL_MAX_DIST_MI = 8.0

RUN_KINDS = ("easy", "recovery", "long", "tempo", "threshold", "intervals",
             "walk", "general_aerobic", "unknown")


def looks_like_intervals(a: dict) -> bool:
    """Detect a rep session from its shape or its title, never its average HR.

    Average HR over a session that is one-third standing recovery comes out
    under the easy cap, so an 8 x 800 would score as a clean easy run and
    the reps would never be looked at.
    """
    dist_mi = (a.get("distance", 0) or 0) / 1609.34
    moving = a.get("moving_time") or 0
    elapsed = a.get("elapsed_time") or 0
    if (moving and elapsed and dist_mi
            and moving / elapsed < INTERVAL_MOVING_RATIO
            and dist_mi < INTERVAL_MAX_DIST_MI):
        return True
    try:
        import intervals
        for field in ("description", "name"):
            if a.get(field) and intervals.parse_prescription(str(a[field])):
                return True
    except Exception:
        pass
    return False


_INTENT_KIND = {"tempo": "tempo", "half": "tempo", "mp": "tempo", "steady": "tempo",
                "progression": "tempo", "10k": "threshold", "5k": "threshold"}


def _stated_kind(a: dict, dist_mi: float):
    """The run kind the description or title commits to, or None."""
    try:
        import session_intent
    except Exception:
        return None
    for field in ("description", "name"):
        if not a.get(field):
            continue
        try:
            intent = session_intent.parse_intent(str(a[field]), dist_mi)
        except Exception:
            intent = None
        if not intent:
            continue
        quality = session_intent.quality_segments(intent)
        if not quality:
            return "easy"
        if dist_mi >= config.long_run_min_mi():
            return "long"
        dominant = max(quality, key=lambda s: s.get("miles") or 0)
        return _INTENT_KIND.get(dominant["kind"], "tempo")
    return None


def classify_run(a: dict) -> str:
    """One of RUN_KINDS for a real run.

    Order: what the description says -> the shape of a rep session -> walking
    pace -> long-run distance -> HR and pace against the configured zones
    (pace alone when there is no HR). A low average HR by itself never means
    "walk": an optical under-read is not a walk; only pace is.
    """
    dist_m = a.get("distance", 0) or 0
    moving_s = a.get("moving_time", 0) or 0
    if dist_m <= 0 or moving_s <= 0:
        return "unknown"
    dist_mi = dist_m / 1609.34
    pace = (moving_s / 60) / dist_mi
    avg_hr = a.get("average_heartrate") or 0
    max_hr = a.get("max_heartrate") or 0

    stated = _stated_kind(a, dist_mi)
    if stated:
        return stated
    if looks_like_intervals(a):
        return "intervals"
    if pace >= 13:
        return "walk"
    if dist_mi >= config.long_run_min_mi():
        return "long"

    pz = config.pace_zones()
    easy_floor = float((pz.get("easy") or {}).get("floor", 10.5))
    tempo_lo, tempo_hi = (pz.get("tempo") or {}).get("hr_range", [150, 160])[:2]
    thr_lo, thr_hi = (pz.get("threshold") or {}).get("hr_range", [160, 170])[:2]
    if avg_hr:
        if avg_hr < config.recovery_hr_cap() and pace > easy_floor:
            return "recovery"
        if tempo_lo <= avg_hr <= tempo_hi:
            return "tempo"
        if thr_lo <= avg_hr <= thr_hi:
            return "threshold"
        if max_hr and max_hr >= config.threshold_hr_upper() and avg_hr <= config.easy_hr_cap():
            return "intervals"
        if avg_hr < config.easy_hr_cap():
            return "easy"
        return "general_aerobic"
    # No HR: pace against the configured bands (floor = slower edge).
    thr_floor = float((pz.get("threshold") or {}).get("floor", 8.5))
    tempo_floor = float((pz.get("tempo") or {}).get("floor", 9.17))
    if pace < thr_floor:
        return "threshold"
    if pace <= tempo_floor:
        return "tempo"
    return "easy"


def attach_workout_type(activity: dict) -> dict:
    """Stamp the run's training purpose (see classify_run)."""
    if activity.get("type") != "Run":
        return activity
    if not is_real_run(activity):
        activity["_workout_type"] = "n/a"
        return activity
    activity["_workout_type"] = classify_run(activity)
    return activity


def compute_run_tss(activity: dict) -> dict:
    """Compute Training Stress Score.

    Real runs: HR-based when avg_hr available; pace-based fallback otherwise
    (writes _run_tss). Rides and bike-equiv entries: HR-based only, with a
    conservative duration estimate when HR is missing (writes _tss, and
    _run_tss = 0 so no stale consumer credits bike work as run load). The
    pace fallback is NEVER applied to bike work — a fake run "pace" is
    exactly the pollution this classification exists to remove.
    Capped at 200 per session.
    """
    cls = activity.get("_activity_class") or classify_activity(activity)
    if cls == "other":
        return activity

    moving_s = activity.get("moving_time", 0) or 0
    dist_m = activity.get("distance", 0) or 0
    duration_hr = moving_s / 3600.0
    avg_hr = activity.get("average_heartrate")

    if cls in ("run", "treadmill_run"):
        if moving_s == 0:
            activity["_run_tss"] = 0.0
            return activity
        if avg_hr:
            intensity = avg_hr / config.threshold_hr()
            tss = duration_hr * (intensity ** 2) * 100
        elif dist_m > 0:
            pace = (moving_s / 60) / (dist_m / 1609.34)
            intensity = config.threshold_pace() / pace
            tss = duration_hr * (intensity ** 2) * 100
        else:
            tss = duration_hr * 50
        activity["_run_tss"] = round(min(tss, 200.0), 1)
        return activity

    # ride / bike_equiv / invalid: cross-training (or nothing), never run TSS
    activity["_run_tss"] = 0.0
    if cls == "invalid" and not avg_hr:
        activity["_tss"] = 0.0
        return activity
    if moving_s == 0:
        activity["_tss"] = 0.0
        return activity
    if avg_hr:
        intensity = avg_hr / config.threshold_hr()
        tss = duration_hr * (intensity ** 2) * 100
    else:
        tss = duration_hr * 40  # conservative aerobic-spin estimate
    activity["_tss"] = round(min(tss, 200.0), 1)
    return activity


def attach_classification(activity: dict) -> dict:
    """Single consolidated classification field for downstream consumers.

    Combines workout_type + pace_zone into a stable label.
    """
    wt = activity.get("_workout_type", "unknown")
    activity["_classification"] = wt
    return activity


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

ENRICHERS = [
    attach_activity_class,   # must run first — later enrichers read the class
    attach_pace_zone,
    attach_workout_type,
    compute_run_tss,
    attach_classification,
]


def enrich(activity: dict) -> dict:
    """Apply all enrichers to an activity dict, in order.

    Mutates and returns the input dict. Adds `_enriched_v` field.
    """
    for fn in ENRICHERS:
        activity = fn(activity)
    activity["_enriched_v"] = ENRICHMENT_VERSION
    return activity


def needs_enrichment(activity: dict) -> bool:
    """True if the activity hasn't been enriched at the current version."""
    return activity.get("_enriched_v", 0) < ENRICHMENT_VERSION


# ---------------------------------------------------------------------------
# Bulk backfill (called from strava_sync)
# ---------------------------------------------------------------------------

def backfill_enrichment(cache_dir, verbose: bool = True, force: bool = False) -> int:
    """Walk all cached JSONs, re-enrich any with a stale version (every one
    when `force`, e.g. after the athlete's zones changed). Returns count."""
    import json
    from pathlib import Path

    cache_dir = Path(cache_dir)
    if not cache_dir.exists():
        return 0

    count = 0
    for p in cache_dir.glob("*.json"):
        try:
            activity = json.loads(p.read_text())
        except json.JSONDecodeError:
            continue
        if not force and not needs_enrichment(activity):
            continue
        activity = enrich(activity)
        p.write_text(json.dumps(activity, indent=2))
        count += 1

    if verbose and count > 0:
        print(f"  [enrichment] Backfilled {count} activities to v{ENRICHMENT_VERSION}")
    return count


if __name__ == "__main__":
    # CLI usage: enrich all cached activities
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    force = "--force" in sys.argv[1:]
    n = backfill_enrichment(config.ACTIVITIES_DIR, force=force)
    print(f"Done. {n} activities {'re-' if force else ''}enriched.")


def reenrich_all(cache_dir=None, verbose: bool = True) -> int:
    """Re-enrich every cached activity regardless of version. Run after the
    config changes (thresholds, zones) so cached TSS and run kinds follow."""
    return backfill_enrichment(cache_dir if cache_dir is not None else config.ACTIVITIES_DIR,
                               verbose=verbose, force=True)
