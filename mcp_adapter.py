#!/usr/bin/env python3
"""Strava MCP payloads -> the local activity cache (the zero-OAuth data path).

The official Strava MCP returns three payloads per run, in a dialect that
differs from the Strava REST shape the engine reads:

    list_activities            summaries, NO heart rate, nested under "summary"
    get_activity_performance   HR, watts, cadence, best efforts, laps (avg_hr/max_hr)
    get_activity_streams       per-second time series (heart_rate, velocity_smooth, ...)

Save each result to a file and ingest the folder in one go:

    data/mcp/list/page-1.json          verbatim pages (or Claude's persisted tool-result files)
    data/mcp/perf/<id>.json            performance, one file per activity
    data/mcp/streams/<id>.json         streams, one file per activity
    data/mcp/profile.json, zones.json  ignored here; `coach.py init --from-mcp` reads them

    python3 coach.py ingest data/mcp/              # merge by id, write cache + streams, rebuild CSV
    python3 coach.py ingest data/mcp/ --dry-run    # match counts only, nothing written

Payloads are joined by activity id: an embedded `id`/`activity_id`, else the
leading digits of the file name, else a {"<id>": payload} map. Persisted
tool-result files ([{"type": "text", "text": "<json>"}]) are unwrapped. A list
of page objects in one file is accepted. Performance or streams that arrive
for a run already in the cache use the cached activity as their base, so HR
and streams can be fetched after the fact. A summary-only re-import never
drops HR, laps, best efforts or streams a previous pass stored.

Classification happens here too: a `sport_type` of Run/TrailRun/VirtualRun is
a Run; strength sports are WeightTraining; everything else, plus Run entries
whose name says "bike"/"spin" or that carry the manual bike-equivalence speed
signature, becomes CrossTrain (aerobic load, never run mileage).
`enrichment.enrich()` then stamps the same precomputed fields a REST sync
would. Stdlib only.
"""

import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import config

CACHE_DIR = config.ACTIVITIES_DIR      # tests swap these three
STREAMS_DIR = config.STREAMS_DIR
MCP_DIR = config.MCP_DIR

REQUIRED_FIELDS = ("id", "type", "start_date_local")

# sport_type values that count as running
_RUN_SPORTS = {"Run", "TrailRun", "VirtualRun"}
# sport_type values that are clearly strength work (distance 0)
_STRENGTH_SPORTS = {"WeightTraining", "Workout", "Crossfit"}
# keywords that betray a cross-training session mislabeled as a Run
_CROSSTRAIN_WORDS = ("bike", "ride", "cycling", "spin", "elliptical",
                     "pool", "swim", "aqua")

# --- MCP -> Strava REST vocabulary -----------------------------------------

_ACTIVITY_KEY_MAP = {
    "start_local": "start_date_local",
    "is_trainer": "trainer",        # enrichment keys treadmill detection off `trainer`
    "is_commute": "commute",
}

# MCP nests these under activity["summary"]; REST has them at the top level.
_SUMMARY_KEY_MAP = {
    "distance": "distance",
    "moving_time": "moving_time",
    "elapsed_time": "elapsed_time",
    "elevation_gain": "total_elevation_gain",
    "avg_speed": "average_speed",
    "max_speed": "max_speed",
    "avg_cadence": "average_cadence",
    "avg_hr": "average_heartrate",
    "max_hr": "max_heartrate",
    "total_calories": "calories",
    "relative_effort": "relative_effort",
}

_LAP_KEY_MAP = {
    "avg_hr": "average_heartrate",
    "max_hr": "max_heartrate",
    "avg_cadence": "average_cadence",
    "avg_watts": "average_watts",
    "elevation_gain": "total_elevation_gain",
    "avg_grade": "average_grade",
}

# Fields a summary-only re-import must never erase from a cached activity.
_PRESERVE_KEYS = ("laps", "best_efforts", "average_heartrate", "max_heartrate",
                  "average_watts", "average_cadence", "calories",
                  "perceived_exertion", "trainer", "description", "has_heartrate")

# Bulky performance sub-payloads nothing downstream reads.
_DROP_KEYS = ("segment_efforts", "pr_achievements")


def _text(*vals) -> str:
    return " ".join(str(v or "") for v in vals).lower()


def _rename(d: dict, key_map: dict, src: dict = None, drop_old: bool = True) -> dict:
    """Copy src[old] -> d[new] for each mapping, without clobbering an
    existing value. Idempotent: a dict already in REST shape is untouched."""
    same = src is None
    src = d if same else src
    for old, new in key_map.items():
        if d.get(new) is None and src.get(old) is not None:
            d[new] = src[old]
        if same and drop_old and old != new and old in d:
            del d[old]
    return d


# ---------------------------------------------------------------------------
# Classification of an MCP list item
# ---------------------------------------------------------------------------

def looks_like_crosstrain(sport_type: str, name: str, description: str,
                          summary: dict = None) -> bool:
    """True if this should count as aerobic cross-training, not running mileage.

    Catches non-run sports, runs whose name/description say "bike", "ride",
    etc. (e.g. an athlete logging Zone-2 bike sessions as "Run" while working
    through a niggle), and, when the summary numbers are provided, manual
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


def normalize_mcp_activity(a: dict) -> dict:
    """Translate one MCP-shaped activity (a list item, a {**item, **perf}
    merge, or a cached dict) into Strava REST shape.

    Idempotent and non-clobbering, so a REST-shaped activity passes through
    unchanged and re-importing is safe. Returns a new dict.
    """
    a = dict(a)
    _rename(a, _ACTIVITY_KEY_MAP)

    summary = a.pop("summary", None)
    if isinstance(summary, dict):
        _rename(a, _SUMMARY_KEY_MAP, src=summary)
    if a.get("suffer_score") is None and a.get("relative_effort") is not None:
        a["suffer_score"] = a["relative_effort"]      # the CSV column name

    sport_type = a.get("sport_type")
    if a.get("type") is None and sport_type:
        probe = {"avg_speed": a.get("average_speed"), "max_speed": a.get("max_speed"),
                 "distance": a.get("distance"), "moving_time": a.get("moving_time")}
        atype = classify_type(sport_type, a.get("name", ""), a.get("description", ""),
                              a.get("distance", 0) or 0, probe)
        a["type"] = atype
        a["_source"] = "mcp"
        if atype == "CrossTrain":
            a["_crosstrain"] = True
            a["_orig_sport_type"] = sport_type

    laps = a.get("laps")
    if isinstance(laps, list):
        normalized = []
        for i, lap in enumerate(laps):
            if not isinstance(lap, dict):
                continue
            lap = _rename(dict(lap), _LAP_KEY_MAP)
            # post_run_review keys its lap table off lap_index; MCP omits it.
            if lap.get("lap_index") is None:
                lap["lap_index"] = i + 1
            normalized.append(lap)
        a["laps"] = normalized

    efforts = a.get("best_efforts")
    if isinstance(efforts, list):
        clean = [e for e in efforts if isinstance(e, dict) and e.get("name")
                 and (e.get("elapsed_time") or e.get("moving_time")) and e.get("distance")]
        if clean:
            a["best_efforts"] = clean
        else:
            a.pop("best_efforts", None)

    for k in _DROP_KEYS:
        a.pop(k, None)
    return a


# Legacy name (v1.0): one list item -> cache dict.
mcp_to_cache_activity = normalize_mcp_activity


def convert(mcp_json) -> list:
    """Convert list_activities output to cache dicts.

    Accepts a response dict ({"activities": [...]}), a bare list of activity
    dicts, or a list of PAGE objects (each carrying its own "activities").
    """
    activities = _items_of(mcp_json)
    return [normalize_mcp_activity(a) for a in activities
            if isinstance(a, dict) and a.get("id") is not None]


def _items_of(mcp_json) -> list:
    if isinstance(mcp_json, dict):
        return list(mcp_json.get("activities", []) or [])
    if isinstance(mcp_json, list) and any(
            isinstance(x, dict) and "activities" in x for x in mcp_json):
        out = []
        for page in mcp_json:
            if isinstance(page, dict):
                out.extend(page.get("activities", []) or [])
        return out
    return list(mcp_json or [])


# ---------------------------------------------------------------------------
# Writing the cache
# ---------------------------------------------------------------------------

def _cache_dir(cache_dir) -> Path:
    return Path(cache_dir) if cache_dir is not None else CACHE_DIR


def _streams_dir(streams_dir, cache_dir=None) -> Path:
    if streams_dir is not None:
        return Path(streams_dir)
    if cache_dir is not None and Path(cache_dir) != CACHE_DIR:
        return Path(cache_dir).parent / "streams"
    return STREAMS_DIR


def write_to_cache(cache_dicts: list, cache_dir=None) -> int:
    """Write each cache dict to <id>.json (idempotent by id). Returns count.
    Activities are enriched before writing (parity with the REST sync path)."""
    from enrichment import enrich
    cdir = _cache_dir(cache_dir)
    cdir.mkdir(parents=True, exist_ok=True)
    n = 0
    for a in cache_dicts:
        aid = a.get("id")
        if aid is None:
            continue
        (cdir / f"{aid}.json").write_text(json.dumps(enrich(dict(a)), indent=2), encoding="utf-8")
        n += 1
    return n


def _read_cached(cdir: Path, aid) -> Optional[dict]:
    p = Path(cdir) / f"{aid}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def rebuild_csv() -> int:
    """Regenerate activities.csv from the full cache via strava_sync's writers
    (existing historical rows are preserved, cached ids win). Returns rows."""
    import strava_sync
    strava_sync._ensure_dirs()
    acts = [a for a in strava_sync._all_cached_activities() if not a.get("_deleted_at")]
    strava_sync._write_csv(acts)
    return len(acts)


def import_activities(activities: list, cache_dir=None, streams_dir=None,
                      csv: bool = None, verbose: bool = True) -> dict:
    """Normalize, enrich and cache each activity; split streams off to their
    own files; rebuild the CSV. Returns a summary dict."""
    from enrichment import enrich
    cdir = _cache_dir(cache_dir)
    sdir = _streams_dir(streams_dir, cache_dir)
    cdir.mkdir(parents=True, exist_ok=True)
    if csv is None:
        csv = (cdir == CACHE_DIR)

    new = updated = skipped = with_laps = with_streams = with_hr = 0
    by_type = {}
    for a in activities:
        if not isinstance(a, dict):
            skipped += 1
            continue
        a = normalize_mcp_activity(a)
        if any(not a.get(k) for k in REQUIRED_FIELDS):
            skipped += 1
            continue
        # Streams never reach the activity JSON or the CSV: ~2500 samples per
        # channel. Absent streams leave any earlier file alone.
        streams = a.pop("streams", None)
        if isinstance(streams, dict) and streams:
            sdir.mkdir(parents=True, exist_ok=True)
            (sdir / f"{a['id']}.json").write_text(json.dumps(streams), encoding="utf-8")
            with_streams += 1
        # Every cache writer elsewhere sets both date fields; downstream sort
        # keys assume start_date exists. MCP items only carry the local one.
        if not a.get("start_date"):
            a["start_date"] = a["start_date_local"]
        prior = _read_cached(cdir, a["id"])
        if prior is not None:
            updated += 1
            # A later import carrying only summary data must not delete what a
            # previous performance fetch stored.
            for k in _PRESERVE_KEYS:
                if (a.get(k) is None or a.get(k) == []) and prior.get(k) not in (None, []):
                    a[k] = prior[k]
        else:
            new += 1
        if a.get("laps"):
            with_laps += 1
        if a.get("average_heartrate") is not None:
            with_hr += 1
        by_type[a["type"]] = by_type.get(a["type"], 0) + 1
        a = enrich(a)
        (cdir / f"{a['id']}.json").write_text(json.dumps(a, indent=2), encoding="utf-8")

    csv_rows = rebuild_csv() if csv else 0
    total = len(list(cdir.glob("*.json")))
    summary = {"new": new, "updated": updated, "skipped": skipped, "by_type": by_type,
               "with_laps": with_laps, "with_streams": with_streams, "with_hr": with_hr,
               "written": new + updated, "csv_rows": csv_rows, "total_in_cache": total,
               "cache_dir": str(cdir)}
    if verbose:
        imported = new + updated
        print(f"  [mcp] {new} new, {updated} updated, {skipped} skipped "
              f"(missing {'/'.join(REQUIRED_FIELDS)}). Cache total: {total}.")
        for t, c in sorted(by_type.items()):
            print(f"  [mcp]   {t:14} {c}")
        if imported and not with_hr:
            print("  [mcp] WARNING: 0 activities carried heart rate. TSS falls back to pace "
                  "and the run review is shallow: fetch get_activity_performance per run "
                  "and ingest again.")
        elif imported:
            print(f"  [mcp] {with_hr}/{imported} carried HR; {with_laps}/{imported} carried laps"
                  + (f"; {with_streams} carried streams." if with_streams else "."))
        if csv_rows:
            print(f"  [mcp] activities.csv rebuilt: {csv_rows} rows.")
    return summary


# ---------------------------------------------------------------------------
# Joining list / performance / streams payloads by activity id
# ---------------------------------------------------------------------------

_STREAM_KEYS = {"time", "distance", "heartrate", "heart_rate", "velocity_smooth",
                "cadence", "altitude", "moving", "latlng", "location", "watts",
                "grade_smooth", "temp"}
_PERF_KEYS = {"laps", "summary", "average_heartrate", "avg_hr", "max_heartrate", "max_hr",
              "splits_metric", "has_heartrate", "best_efforts", "segment_efforts",
              "average_watts", "has_device_watts", "perceived_exertion"}
_IGNORED_STEMS = {"profile", "zones", "athlete", "training_plan"}


def id_from_path(path) -> Optional[int]:
    """Leading digits of the file stem: perf/20145951475.json, 20145951475_streams.json."""
    if path is None:
        return None
    stem = Path(str(path)).stem
    digits = ""
    for ch in stem:
        if ch.isdigit():
            digits += ch
        else:
            break
    return int(digits) if len(digits) >= 5 else None


def _embedded_id(obj: dict) -> Optional[int]:
    for k in ("id", "activity_id"):
        v = obj.get(k)
        if v in (None, ""):
            continue
        try:
            return int(v)
        except (TypeError, ValueError):
            return None
    return None


def _unwrap_tool_result(data):
    """Claude Code persists large MCP results as [{"type": "text", "text": "<json>"}]."""
    if (isinstance(data, list) and data
            and all(isinstance(x, dict) and x.get("type") == "text"
                    and isinstance(x.get("text"), str) for x in data)):
        try:
            return json.loads("".join(x["text"] for x in data))
        except json.JSONDecodeError:
            return data
    return data


def _read_json(path):
    text = sys.stdin.read() if str(path) == "-" else Path(path).read_text(encoding="utf-8")
    return _unwrap_tool_result(json.loads(text))


def expand_paths(paths: list) -> list:
    """Directories expand to every *.json under them (sorted); files pass through."""
    out = []
    for p in paths:
        if str(p) == "-":
            out.append("-")
            continue
        pp = Path(p)
        if pp.is_dir():
            out.extend(sorted(pp.rglob("*.json")))
        else:
            out.append(pp)
    return out


def classify_payload(obj, path=None) -> tuple:
    """-> (kind, activity_id). kind in summaries | activity | idmap | streams |
    performance | ignored | unknown. The id comes from the payload, else the
    file name."""
    if path is not None and str(path) != "-" and Path(str(path)).stem.lower() in _IGNORED_STEMS:
        return "ignored", None
    if isinstance(obj, list):
        return "summaries", None
    if not isinstance(obj, dict):
        return "unknown", None
    if isinstance(obj.get("activities"), list):
        return "summaries", None
    keys = list(obj.keys())
    if keys and all(str(k).isdigit() for k in keys) and all(isinstance(v, dict) for v in obj.values()):
        return "idmap", None
    aid = _embedded_id(obj)
    if aid is None:
        aid = id_from_path(path)
    stream_hits = [k for k in keys if k in _STREAM_KEYS
                   and (isinstance(obj[k], list)
                        or (isinstance(obj[k], dict) and isinstance(obj[k].get("data"), list)))]
    has_perf = any(k in _PERF_KEYS for k in keys)
    if stream_hits and not has_perf:
        return "streams", aid
    if isinstance(obj.get("streams"), dict) and not has_perf and not obj.get("sport_type"):
        return "streams", aid
    has_type = bool(obj.get("type") or obj.get("sport_type"))
    has_start = bool(obj.get("start_date_local") or obj.get("start_local"))
    if has_type and has_start and aid is not None:
        return "activity", aid          # a full activity dict (list item or pre-merged)
    if has_perf:
        return "performance", aid
    return "unknown", aid


def _absorb(data, src, summaries: list, perf_by_id: dict, streams_by_id: dict, warnings: list):
    kind, aid = classify_payload(data, None if src == "-" else src)
    if kind == "ignored":
        return
    if kind == "summaries":
        summaries.extend(x for x in _items_of(data) if isinstance(x, dict))
    elif kind == "activity":
        summaries.append(data)
    elif kind == "idmap":
        for k, v in data.items():
            sub_kind, _ = classify_payload(v, None)
            sub_id = int(k)
            if sub_kind == "streams":
                streams_by_id[sub_id] = v.get("streams", v) if isinstance(v.get("streams"), dict) else v
            elif sub_kind == "performance":
                perf_by_id[sub_id] = v
            elif sub_kind == "activity":
                v = dict(v)
                v.setdefault("id", sub_id)
                summaries.append(v)
            else:
                warnings.append(f"{src}[{k}]: unrecognised payload, skipped")
    elif kind == "streams":
        if aid is None:
            warnings.append(f"{src}: streams without an id (name the file <id>.json)")
        else:
            streams_by_id[aid] = data.get("streams", data) if isinstance(data.get("streams"), dict) else data
    elif kind == "performance":
        if aid is None:
            warnings.append(f"{src}: performance data without an id (name the file <id>.json)")
        else:
            perf_by_id[aid] = data
    else:
        warnings.append(f"{src}: unrecognised payload, skipped")


def load_merge_inputs(paths: list) -> tuple:
    """-> (summaries, perf_by_id, streams_by_id, warnings)."""
    summaries, perf_by_id, streams_by_id, warnings = [], {}, {}, []
    for p in expand_paths(paths):
        try:
            data = _read_json(p)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
            warnings.append(f"{p}: unreadable ({e})")
            continue
        _absorb(data, p, summaries, perf_by_id, streams_by_id, warnings)
    return summaries, perf_by_id, streams_by_id, warnings


def merge_by_id(summaries: list, perf_by_id: dict, streams_by_id: dict,
                cached_lookup=None) -> tuple:
    """{**summary, **performance} + streams, keyed by activity id.

    Performance or streams with no summary in this batch fall back to the
    cached activity (`cached_lookup(id)`), else to a standalone import when the
    performance payload itself carries type + start; otherwise they are
    reported unmatched. -> (merged_activities, stats)
    """
    merged, seen = [], set()
    stats = {"summaries": len(summaries), "perf": len(perf_by_id), "streams": len(streams_by_id),
             "perf_matched": 0, "streams_matched": 0, "cache_backed": 0,
             "perf_unmatched": [], "streams_unmatched": []}
    for s in summaries:
        try:
            aid = int(s.get("id"))
        except (TypeError, ValueError):
            merged.append(dict(s))
            continue
        m = dict(s)
        if aid in perf_by_id:
            m.update(perf_by_id[aid])
            m.setdefault("id", aid)
            stats["perf_matched"] += 1
        if aid in streams_by_id:
            m["streams"] = streams_by_id[aid]
            stats["streams_matched"] += 1
        merged.append(m)
        seen.add(aid)
    for aid in sorted(set(perf_by_id) | set(streams_by_id)):
        if aid in seen:
            continue
        base = cached_lookup(aid) if cached_lookup else None
        if base is not None:
            stats["cache_backed"] += 1
        else:
            perf = perf_by_id.get(aid) or {}
            if ((perf.get("type") or perf.get("sport_type"))
                    and (perf.get("start_date_local") or perf.get("start_local"))):
                base = {"id": aid}
            else:
                if aid in perf_by_id:
                    stats["perf_unmatched"].append(aid)
                if aid in streams_by_id:
                    stats["streams_unmatched"].append(aid)
                continue
        m = dict(base)
        m.setdefault("id", aid)
        if aid in perf_by_id:
            m.update(perf_by_id[aid])
            m["id"] = m.get("id") or aid
            stats["perf_matched"] += 1
        if aid in streams_by_id:
            m["streams"] = streams_by_id[aid]
            stats["streams_matched"] += 1
        merged.append(m)
    return merged, stats


def run_merge(paths: list, dry_run: bool = False, verbose: bool = True,
              cache_dir=None, streams_dir=None, csv: bool = None) -> dict:
    """Load, join and (unless dry_run) import. Returns {stats, warnings, import}."""
    cdir = _cache_dir(cache_dir)
    summaries, perf_by_id, streams_by_id, warnings = load_merge_inputs(paths)
    merged, stats = merge_by_id(summaries, perf_by_id, streams_by_id,
                                cached_lookup=lambda aid: _read_cached(cdir, aid))
    if verbose:
        print(f"  [mcp] merge: {stats['summaries']} summaries, "
              f"{stats['perf']} performance ({stats['perf_matched']} matched, "
              f"{len(stats['perf_unmatched'])} unmatched), "
              f"{stats['streams']} streams ({stats['streams_matched']} matched, "
              f"{len(stats['streams_unmatched'])} unmatched), "
              f"{stats['cache_backed']} cache-backed")
        for w in warnings:
            print(f"  [mcp] warning: {w}")
        for label in ("perf_unmatched", "streams_unmatched"):
            if stats[label]:
                print(f"  [mcp] {label.replace('_', ' ')}: "
                      + ", ".join(str(i) for i in stats[label][:10])
                      + (" ..." if len(stats[label]) > 10 else "")
                      + "  (no summary in this batch and not in the cache)")
        if dry_run:
            print(f"  [mcp] dry run: {len(merged)} activities would be imported.")
    result = {"stats": stats, "warnings": warnings, "import": None}
    if not dry_run:
        result["import"] = import_activities(merged, cache_dir=cache_dir, streams_dir=streams_dir,
                                             csv=csv, verbose=verbose)
        if verbose:
            days = history_days(_all_cached(cdir))
            if 0 < days < 60:
                print(f"  [mcp] history: {days} days in the cache. CTL is a 42-day average and "
                      "needs 60+ days before fitness/fatigue numbers can be trusted; pull a "
                      "longer window.")
    return result


# ---------------------------------------------------------------------------
# Legacy v1.0 entry points (kept for callers and tests)
# ---------------------------------------------------------------------------

def load_performance(path) -> dict:
    """Performance payloads keyed by activity id: one JSON dict {"<id>": {...}}
    or a directory of <id>.json files."""
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


def merge_performance(perf_by_id: dict, cache_dir=None) -> int:
    """Merge get_activity_performance payloads into cached activities.
    Ids not in the cache are skipped. Returns activities updated."""
    cdir = _cache_dir(cache_dir)
    by_int = {}
    for k, v in (perf_by_id or {}).items():
        try:
            by_int[int(k)] = v
        except (TypeError, ValueError):
            continue
    merged, stats = merge_by_id([], by_int, {}, cached_lookup=lambda aid: _read_cached(cdir, aid))
    if not merged:
        return 0
    res = import_activities(merged, cache_dir=cache_dir, csv=False, verbose=False)
    return res["new"] + res["updated"]


def ingest_mcp_file(paths, cache_dir=None, performance=None, csv: bool = None) -> dict:
    """Ingest list file(s) [+ a performance file/dir]; return the v1 summary."""
    if isinstance(paths, (str, Path)):
        paths = [paths]
    paths = [str(p) for p in paths]
    if performance:
        paths.append(str(performance))
    res = run_merge(paths, cache_dir=cache_dir, csv=csv, verbose=False)
    imp = res["import"] or {}
    return {"written": imp.get("written", 0), "by_type": imp.get("by_type", {}),
            "cache_dir": str(_cache_dir(cache_dir)),
            "performance_merged": res["stats"]["perf_matched"],
            "csv_rows": imp.get("csv_rows", 0)}


# ---------------------------------------------------------------------------
# What to fetch next (drives the skills' MCP calls)
# ---------------------------------------------------------------------------

_WORKOUT_WORDS = ("tempo", "threshold", "interval", "track", "workout", "race", "5k", "10k",
                  "half", "marathon", "reps", "fartlek", "progression", "hills", "strides",
                  "long run", "long")
_LONG_RUN_MI = 7.0


def _local_date(a: dict) -> Optional[date]:
    iso = a.get("start_date_local") or a.get("start_date") or ""
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).date()
    except ValueError:
        return None


def workout_like(a: dict) -> bool:
    """Long run or a named quality session: the runs whose laps and streams
    the review needs. HR-free on purpose (it runs before performance is fetched)."""
    import re
    dist_mi = (a.get("distance", 0) or 0) / 1609.34
    if dist_mi >= _LONG_RUN_MI:
        return True
    blob = _text(a.get("name"), a.get("description"))
    if re.search(r"\b\d+\s*[x×]\s*\d", blob):
        return True
    return any(w in blob for w in _WORKOUT_WORDS)


def _all_cached(cdir: Path) -> list:
    out = []
    for p in Path(cdir).glob("*.json") if Path(cdir).exists() else []:
        a = _read_cached(cdir, p.stem)
        if a is not None and not a.get("_deleted_at"):
            out.append(a)
    return out


def history_days(cache_acts: list, today: date = None) -> int:
    """Days from the oldest cached activity to `today` (0 when empty)."""
    today = today or date.today()
    dates = [d for d in (_local_date(a) for a in cache_acts) if d is not None]
    return (today - min(dates)).days if dates else 0


def fetch_plan(cache_acts: list = None, today: date = None, perf_days: int = 21,
               window_days: int = 90, streams_days: int = 28, inbox=None,
               streams_dir=None) -> dict:
    """Which MCP calls are still worth making.

    perf_needed: real runs in the last `perf_days` lacking HR, plus every
    workout-like run in the window lacking HR. streams_needed: workout-like
    runs in the last `streams_days` with no cached stream. Ids whose payload
    already sits in the inbox are skipped.
    """
    from enrichment import is_real_run
    today = today or date.today()
    if cache_acts is None:
        cache_acts = _all_cached(CACHE_DIR)
    inbox = Path(inbox) if inbox is not None else MCP_DIR
    sdir = _streams_dir(streams_dir)
    perf_needed, streams_needed = [], []
    for a in sorted(cache_acts, key=lambda x: str(x.get("start_date_local") or ""), reverse=True):
        if not is_real_run(a):
            continue
        d = _local_date(a)
        if d is None or (today - d).days > window_days:
            continue
        age = (today - d).days
        aid = a.get("id")
        wl = workout_like(a)
        if a.get("average_heartrate") is None and (age <= perf_days or wl):
            if not (inbox / "perf" / f"{aid}.json").exists():
                perf_needed.append(aid)
        if wl and age <= streams_days:
            if not (sdir / f"{aid}.json").exists() and not (inbox / "streams" / f"{aid}.json").exists():
                streams_needed.append(aid)
    return {"range_start": (today - timedelta(days=window_days)).isoformat() + "T00:00:00",
            "window_days": window_days, "perf_days": perf_days, "streams_days": streams_days,
            "perf_needed": perf_needed, "streams_needed": streams_needed,
            "history_days": history_days(cache_acts, today)}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_cli(argv: list) -> dict:
    """-> {"paths": [...], "dry_run": bool}. Legacy `--from-mcp X` and
    `--performance X` tokens are folded into the path list."""
    opts = {"paths": [], "dry_run": False}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--dry-run":
            opts["dry_run"] = True
        elif a in ("--merge", "--no-csv"):
            pass
        elif a in ("--from-mcp", "--performance") and i + 1 < len(argv):
            opts["paths"].append(argv[i + 1])
            i += 1
        elif a.startswith("--from-mcp=") or a.startswith("--performance="):
            opts["paths"].append(a.split("=", 1)[1])
        elif a.startswith("--"):
            raise ValueError(f"unknown flag {a}")
        else:
            opts["paths"].append(a)
        i += 1
    if not opts["paths"]:
        if MCP_DIR.exists():
            opts["paths"] = [str(MCP_DIR)]
        else:
            raise ValueError(f"no input given and {MCP_DIR} does not exist")
    return opts


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    argv = sys.argv[1:] if argv is None else list(argv)
    try:
        opts = parse_cli(argv)
    except ValueError as e:
        print(f"  [mcp] {e}\n")
        print("Usage: python3 coach.py ingest <dir|files...> [--dry-run]")
        return 1
    res = run_merge(opts["paths"], dry_run=opts["dry_run"])
    imp = res["import"]
    if imp is not None and imp["written"] == 0 and imp["skipped"] == 0 and not res["stats"]["summaries"]:
        print("  [mcp] nothing ingested: no activity payloads found in the input.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
