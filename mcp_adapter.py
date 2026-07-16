#!/usr/bin/env python3
"""Adapter: Strava MCP `list_activities` output -> canonical cache-JSON shape.

The Strava MCP returns activities in a different shape than the Strava REST
cache (data/strava_cache/activities/*.json) that the coach engine reads. This
module converts MCP activities into that cache shape so the whole engine
(metrics, fitness_tracker, race_predictor, analysis) runs on MCP-pulled data
with no OAuth / sync pipeline.

It CLASSIFIES rather than deletes cross-training that is logged as a Run on
Strava (e.g. Zone-2 bike sessions done through a niggle). Those are tagged
`_crosstrain: true` and given type "CrossTrain" so the run-mileage stream
excludes them while the aerobic-load stream can still count them.

MCP shape (per activity):
    {"id": "...", "name": "...", "sport_type": "Run", "description": "...",
     "start_local": "2026-05-16T07:34:30",
     "summary": {"distance": 21331.7, "moving_time": 6929, "elapsed_time": 6937,
                 "elevation_gain": 78, "avg_speed": 3.07, "max_speed": 4.02,
                 "relative_effort": 190, "avg_cadence": 79.6, "total_calories": 1569}}

Cache shape (subset the engine reads): id, name, type, distance (m),
moving_time (s), elapsed_time (s), start_date_local, total_elevation_gain,
average_cadence, average_speed, relative_effort, calories. Heart rate is not
present in the MCP list summary, so TSS falls back to pace-based downstream.

Stdlib only.
"""

import json
import sys
from pathlib import Path

CACHE_DIR = Path(__file__).parent / "data" / "strava_cache" / "activities"

# sport_type values that count as running
_RUN_SPORTS = {"Run", "TrailRun", "VirtualRun"}
# sport_type values that are clearly strength work (distance 0)
_STRENGTH_SPORTS = {"WeightTraining", "Workout", "Crossfit"}
# keywords that betray a cross-training session mislabeled as a Run
_CROSSTRAIN_WORDS = ("bike", "ride", "cycling", "spin", "elliptical",
                     "pool", "swim", "aqua")


def _text(*vals) -> str:
    return " ".join(str(v or "") for v in vals).lower()


def looks_like_crosstrain(sport_type: str, name: str, description: str,
                          summary: dict = None) -> bool:
    """True if this should count as aerobic cross-training, not running mileage.

    Catches non-run sports, runs whose name/description say "bike", "ride",
    etc. (e.g. an athlete logging Zone-2 bike sessions as "Run" while working
    through a niggle), and — when the summary numbers are provided — manual
    entries carrying the bike-equivalence speed signature even with a clean
    name (enrichment.looks_like_bike_equiv is the shared detector).
    """
    if sport_type and sport_type not in _RUN_SPORTS and sport_type not in _STRENGTH_SPORTS:
        return True
    blob = _text(name, description)
    if any(w in blob for w in _CROSSTRAIN_WORDS):
        return True
    if summary:
        from enrichment import looks_like_bike_equiv
        return looks_like_bike_equiv(
            summary.get("avg_speed"), summary.get("max_speed"),
            summary.get("distance"), summary.get("moving_time"))
    return False


def classify_type(sport_type: str, name: str, description: str, distance_m: float,
                  summary: dict = None) -> str:
    """Map an MCP activity to a cache `type`: Run | CrossTrain | WeightTraining."""
    if sport_type in _STRENGTH_SPORTS:
        return "WeightTraining"
    if sport_type in _RUN_SPORTS and not looks_like_crosstrain(
            sport_type, name, description, summary):
        return "Run"
    # Everything else (rides, swims, run-labeled bike sessions) is aerobic
    # cross-training: excluded from running mileage, counted toward aerobic load.
    return "CrossTrain"


def mcp_to_cache_activity(act: dict) -> dict:
    """Convert one MCP activity dict into the cache-JSON shape."""
    summary = act.get("summary", {}) or {}
    sport_type = act.get("sport_type", "")
    name = act.get("name", "")
    description = act.get("description", "")
    distance_m = summary.get("distance", 0) or 0
    atype = classify_type(sport_type, name, description, distance_m, summary)

    out = {
        "id": act.get("id"),
        "name": name,
        "type": atype,
        "sport_type": sport_type,
        "distance": distance_m,
        "moving_time": summary.get("moving_time", 0) or 0,
        "elapsed_time": summary.get("elapsed_time", 0) or 0,
        "total_elevation_gain": summary.get("elevation_gain", 0) or 0,
        "average_cadence": summary.get("avg_cadence"),
        "average_speed": summary.get("avg_speed"),
        "max_speed": summary.get("max_speed"),
        "relative_effort": summary.get("relative_effort"),
        "calories": summary.get("total_calories"),
        "start_date_local": act.get("start_local"),
        "start_date": act.get("start_local"),
        "description": description,
        "_source": "mcp",
    }
    if atype == "CrossTrain":
        out["_crosstrain"] = True
        out["_orig_sport_type"] = sport_type
    return out


def convert(mcp_json) -> list:
    """Convert an MCP list_activities response (or a bare list) to cache dicts."""
    if isinstance(mcp_json, dict):
        activities = mcp_json.get("activities", [])
    else:
        activities = mcp_json
    out = []
    for act in activities:
        if not isinstance(act, dict):
            continue
        if act.get("id") is None:
            continue
        out.append(mcp_to_cache_activity(act))
    return out


def write_to_cache(cache_dicts: list, cache_dir: Path = CACHE_DIR) -> int:
    """Write each cache dict to <id>.json (idempotent by id). Returns count."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    for a in cache_dicts:
        aid = a.get("id")
        if aid is None:
            continue
        (cache_dir / f"{aid}.json").write_text(
            json.dumps(a, indent=2), encoding="utf-8"
        )
        n += 1
    return n


def ingest_mcp_file(path: str, cache_dir: Path = CACHE_DIR) -> dict:
    """Load an MCP JSON file, convert, and write into the cache. Returns a summary."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    cache_dicts = convert(raw)
    written = write_to_cache(cache_dicts, cache_dir)
    by_type = {}
    for a in cache_dicts:
        by_type[a["type"]] = by_type.get(a["type"], 0) + 1
    return {"written": written, "by_type": by_type, "cache_dir": str(cache_dir)}


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print("Usage: python3 mcp_adapter.py <mcp_activities.json>")
        return 1
    summary = ingest_mcp_file(argv[0])
    print(f"Ingested {summary['written']} activities -> {summary['cache_dir']}")
    for t, c in sorted(summary["by_type"].items()):
        print(f"  {t:14} {c}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
