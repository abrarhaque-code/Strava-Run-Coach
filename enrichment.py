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

from typing import Optional

import config


# Bump this when enrichers are added or their logic changes.
# All cached activities with a lower _enriched_v will be re-enriched on next sync.
ENRICHMENT_VERSION = 2


# ---------------------------------------------------------------------------
# Individual enrichers — each is a pure function: dict -> dict
# ---------------------------------------------------------------------------

def attach_pace_zone(activity: dict) -> dict:
    """Classify the dominant pace zone of a run."""
    if activity.get("type") != "Run":
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


def attach_workout_type(activity: dict) -> dict:
    """Classify the run's training purpose.

    Wraps post_run_review.classify_run logic but keeps it self-contained so we
    don't pull in the API client just to enrich.
    """
    if activity.get("type") != "Run":
        return activity

    dist_m = activity.get("distance", 0) or 0
    moving_s = activity.get("moving_time", 0) or 0
    if dist_m == 0 or moving_s == 0:
        activity["_workout_type"] = "unknown"
        return activity

    dist_mi = dist_m / 1609.34
    pace = (moving_s / 60) / dist_mi
    avg_hr = activity.get("average_heartrate") or 0
    max_hr = activity.get("max_heartrate") or 0

    # Order matters — most specific first
    if pace >= 13 or (avg_hr and avg_hr < 110):
        wt = "walk"
    elif avg_hr and avg_hr < 130 and pace > 10.5:
        wt = "recovery"
    elif dist_mi >= 7:
        wt = "long"
    elif avg_hr and 145 <= avg_hr <= 165 and 8.0 <= pace <= 9.5:
        wt = "tempo"
    elif max_hr and max_hr >= 175 and avg_hr and avg_hr < 150:
        wt = "intervals"
    elif avg_hr and avg_hr < config.easy_hr_cap():
        wt = "easy"
    else:
        wt = "general_aerobic"

    activity["_workout_type"] = wt
    return activity


def compute_run_tss(activity: dict) -> dict:
    """Compute Training Stress Score for the run.

    HR-based when avg_hr available; pace-based fallback otherwise.
    Capped at 200 per session.
    """
    if activity.get("type") != "Run":
        return activity

    moving_s = activity.get("moving_time", 0) or 0
    dist_m = activity.get("distance", 0) or 0
    if moving_s == 0:
        activity["_run_tss"] = 0.0
        return activity

    duration_hr = moving_s / 3600.0
    avg_hr = activity.get("average_heartrate")

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

def backfill_enrichment(cache_dir, verbose: bool = True) -> int:
    """Walk all cached JSONs, re-enrich any with stale version. Returns count."""
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
        if not needs_enrichment(activity):
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
    from pathlib import Path
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    cache = Path(__file__).parent / "data" / "strava_cache" / "activities"
    n = backfill_enrichment(cache)
    print(f"Done. {n} activities enriched.")
