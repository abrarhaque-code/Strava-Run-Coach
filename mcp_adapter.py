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

MCP `list_activities` shape (per activity):
    {"id": "...", "name": "...", "sport_type": "Run", "description": "...",
     "start_local": "2026-05-16T07:34:30",
     "summary": {"distance": 21331.7, "moving_time": 6929, "elapsed_time": 6937,
                 "elevation_gain": 78, "avg_speed": 3.07, "max_speed": 4.02,
                 "relative_effort": 190, "avg_cadence": 79.6, "total_calories": 1569}}

Pagination: list_activities caps at 100 per call and returns has_next_page /
end_cursor. Save each page verbatim and either concatenate the pages into one
JSON array (this module accepts an array of page objects) or pass several
files to ingest_mcp_file / `coach.py analyze --from-mcp p1.json p2.json`.

Heart rate is NOT in the MCP list summary — it lives in the separate
`get_activity_performance` tool (avg/max HR, watts, laps, best efforts).
merge_performance() folds those payloads into cached activities, keyed by
activity id, and re-enriches so TSS flips from pace-based to HR-based and
lap-level analysis (post_run_review, MP-segment verification) lights up.

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
    """Convert MCP list_activities output to cache dicts.

    Accepts three shapes: a single response dict ({"activities": [...]}), a
    bare list of activity dicts, or a list of PAGE objects (each carrying its
    own "activities") so a paginated pull can be saved as one JSON array.
    """
    if isinstance(mcp_json, dict):
        activities = mcp_json.get("activities", [])
    elif isinstance(mcp_json, list) and any(
            isinstance(x, dict) and "activities" in x for x in mcp_json):
        activities = []
        for page in mcp_json:
            if isinstance(page, dict):
                activities.extend(page.get("activities", []) or [])
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
    """Write each cache dict to <id>.json (idempotent by id). Returns count.

    Activities are enriched before writing (parity with the REST sync path)
    so downstream consumers always read pre-computed _activity_class/_run_tss.
    """
    from enrichment import enrich
    cache_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    for a in cache_dicts:
        aid = a.get("id")
        if aid is None:
            continue
        (cache_dir / f"{aid}.json").write_text(
            json.dumps(enrich(a), indent=2), encoding="utf-8"
        )
        n += 1
    return n


# ---------------------------------------------------------------------------
# get_activity_performance merge (HR + laps + best efforts)
# ---------------------------------------------------------------------------

# MCP performance fields -> REST names the engine reads.
_PERF_SCALAR_FIELDS = (
    "average_heartrate", "max_heartrate", "has_heartrate", "has_device_watts",
    "average_watts", "average_cadence", "calories", "perceived_exertion",
)

_LAP_FIELD_MAP = {
    "avg_hr": "average_heartrate",
    "max_hr": "max_heartrate",
    "avg_cadence": "average_cadence",
    "avg_watts": "average_watts",
}


def _map_lap(lap: dict, idx: int) -> dict:
    """Rename MCP lap fields to the REST names lap consumers expect."""
    out = dict(lap)
    for src, dst in _LAP_FIELD_MAP.items():
        if src in out:
            out[dst] = out.pop(src)
    out.setdefault("lap_index", idx + 1)
    if "elevation_gain" in out:
        out.setdefault("total_elevation_gain", out["elevation_gain"])
    return out


def load_performance(path) -> dict:
    """Load performance payloads keyed by activity id.

    The MCP get_activity_performance response doesn't echo the id, so callers
    key it themselves: either one JSON dict {"<id>": {...}, ...} or a
    directory of <id>.json files.
    """
    p = Path(path)
    if p.is_dir():
        out = {}
        for fp in p.glob("*.json"):
            try:
                out[fp.stem] = json.loads(fp.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
        return out
    data = json.loads(p.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def merge_performance(perf_by_id: dict, cache_dir: Path = CACHE_DIR) -> int:
    """Merge get_activity_performance payloads into cached activities.

    HR landing on an activity flips its TSS from pace-based to HR-based on
    the re-enrich; laps enable post_run_review's lap breakdown and the
    lap-verified MP-segment plan metric. Returns activities updated.
    """
    from enrichment import enrich
    n = 0
    for aid, perf in (perf_by_id or {}).items():
        if not isinstance(perf, dict):
            continue
        p = Path(cache_dir) / f"{aid}.json"
        if not p.exists():
            continue
        a = json.loads(p.read_text(encoding="utf-8"))
        for field in _PERF_SCALAR_FIELDS:
            if perf.get(field) is not None:
                a[field] = perf[field]
        laps = perf.get("laps")
        if isinstance(laps, list) and laps:
            a["laps"] = [_map_lap(lap, i) for i, lap in enumerate(laps)
                         if isinstance(lap, dict)]
        efforts = perf.get("best_efforts")
        if isinstance(efforts, list):
            # Defensive: only pass through entries metrics can actually read.
            clean = [e for e in efforts if isinstance(e, dict) and e.get("name")
                     and (e.get("elapsed_time") or e.get("moving_time"))
                     and e.get("distance")]
            if clean:
                a["best_efforts"] = clean
        p.write_text(json.dumps(enrich(a), indent=2), encoding="utf-8")
        n += 1
    return n


def rebuild_csv() -> int:
    """Regenerate activities.csv from the full cache via strava_sync's writers.

    Keeps the CSV seam (analysis.py and the CSV fallbacks) in sync with
    MCP-ingested data exactly as a REST sync would — existing historical
    rows are preserved, cached ids win. Returns rows written from cache.
    """
    import strava_sync
    strava_sync._ensure_dirs()
    acts = [a for a in strava_sync._all_cached_activities()
            if not a.get("_deleted_at")]
    strava_sync._write_csv(acts)
    return len(acts)


def ingest_mcp_file(paths, cache_dir: Path = CACHE_DIR,
                    performance=None, csv: bool = None) -> dict:
    """Load MCP JSON file(s), convert, write into the cache; return a summary.

    paths: one path or a list of paths (multi-page pulls).
    performance: optional path to get_activity_performance payloads
        (dict keyed by id, or a directory of <id>.json) merged after ingest.
    csv: rebuild activities.csv from the cache afterward. Defaults to True
        for the real cache and False when cache_dir is overridden (tests),
        since the CSV writers always target strava_sync's own paths.
    """
    if isinstance(paths, (str, Path)):
        paths = [paths]
    cache_dicts = []
    for path in paths:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        cache_dicts.extend(convert(raw))
    written = write_to_cache(cache_dicts, cache_dir)

    merged = 0
    if performance:
        merged = merge_performance(load_performance(performance), cache_dir)

    if csv is None:
        csv = (Path(cache_dir) == CACHE_DIR)
    csv_rows = rebuild_csv() if csv else 0

    by_type = {}
    for a in cache_dicts:
        by_type[a["type"]] = by_type.get(a["type"], 0) + 1
    return {"written": written, "by_type": by_type, "cache_dir": str(cache_dir),
            "performance_merged": merged, "csv_rows": csv_rows}


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print("Usage: python3 mcp_adapter.py <mcp_activities.json>... "
              "[--performance <file-or-dir>] [--no-csv]")
        return 1
    performance = None
    csv = None
    paths = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--performance" and i + 1 < len(argv):
            performance = argv[i + 1]
            i += 2
        elif a.startswith("--performance="):
            performance = a.split("=", 1)[1]
            i += 1
        elif a == "--no-csv":
            csv = False
            i += 1
        else:
            paths.append(a)
            i += 1
    if not paths:
        print("No input files given.")
        return 1
    summary = ingest_mcp_file(paths, performance=performance, csv=csv)
    print(f"Ingested {summary['written']} activities -> {summary['cache_dir']}")
    for t, c in sorted(summary["by_type"].items()):
        print(f"  {t:14} {c}")
    if summary["performance_merged"]:
        print(f"  performance merged into {summary['performance_merged']} activities")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
