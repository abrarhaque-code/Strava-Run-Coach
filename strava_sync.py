"""Sync Strava activities into local activities.csv.

Replaces manual ZIP export. Pulls all activities (or only new ones since last
sync) and writes them to activities.csv in the same format the Strava bulk
export uses, so downstream code (analysis.py, planner.py, etc.) keeps working
without changes.

Usage:
    python3 strava_sync.py                # incremental sync (only new)
    python3 strava_sync.py --full         # full re-sync from Strava
    python3 strava_sync.py --backfill 90  # last 90 days

The cache stores raw API responses in data/strava_cache/ for richer downstream
analysis (laps, streams, splits) without repeated API calls.
"""

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from enrichment import enrich, needs_enrichment, backfill_enrichment
from strava_api import StravaAPI, RateLimitError, StravaAPIError


CACHE_DIR = Path(__file__).parent / "data" / "strava_cache"
ACTIVITIES_DIR = CACHE_DIR / "activities"
LAST_SYNC_FILE = CACHE_DIR / "last_sync.json"
SYNC_STATE_FILE = CACHE_DIR / "sync_state.json"
LOCK_FILE = Path(__file__).parent / "data" / ".sync.lock"
CSV_PATH = Path(__file__).parent / "activities.csv"

# Lock is considered stale and reclaimable after this many seconds
LOCK_STALE_SEC = 600  # 10 min
DEFAULT_MAX_PER_RUN = 50

# Strava bulk export CSV header (104 columns). Must match exactly so existing
# parsers (analysis.py) work unchanged.
CSV_HEADER = (
    "Activity ID,Activity Date,Activity Name,Activity Type,Activity Description,"
    "Elapsed Time,Distance,Max Heart Rate,Relative Effort,Commute,"
    "Activity Private Note,Activity Gear,Filename,Athlete Weight,Bike Weight,"
    "Elapsed Time,Moving Time,Distance,Max Speed,Average Speed,"
    "Elevation Gain,Elevation Loss,Elevation Low,Elevation High,Max Grade,"
    "Average Grade,Average Positive Grade,Average Negative Grade,Max Cadence,"
    "Average Cadence,Max Heart Rate,Average Heart Rate,Max Watts,Average Watts,"
    "Calories,Max Temperature,Average Temperature,Relative Effort,Total Work,"
    "Number of Runs,Uphill Time,Downhill Time,Other Time,Perceived Exertion,"
    "Type,Start Time,Weighted Average Power,Power Count,"
    "Prefer Perceived Exertion,Perceived Relative Effort,Commute,"
    "Total Weight Lifted,From Upload,Grade Adjusted Distance,"
    "Weather Observation Time,Weather Condition,Weather Temperature,"
    "Apparent Temperature,Dewpoint,Humidity,Weather Pressure,Wind Speed,"
    "Wind Gust,Wind Bearing,Precipitation Intensity,Sunrise Time,Sunset Time,"
    "Moon Phase,Bike,Gear,Precipitation Probability,Precipitation Type,"
    "Cloud Cover,Weather Visibility,UV Index,Weather Ozone,Jump Count,"
    "Total Grit,Average Flow,Flagged,Average Elapsed Speed,Dirt Distance,"
    "Newly Explored Distance,Newly Explored Dirt Distance,Activity Count,"
    "Total Steps,Carbon Saved,Pool Length,Training Load,Intensity,"
    "Average Grade Adjusted Pace,Timer Time,Total Cycles,Recovery,With Pet,"
    "Competition,Long Run,For a Cause,With Kid,Downhill Distance,"
    "Total Sets,Total Reps,Media"
)


def _ensure_dirs():
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    ACTIVITIES_DIR.mkdir(parents=True, exist_ok=True)


def _last_sync_ts() -> Optional[int]:
    if LAST_SYNC_FILE.exists():
        try:
            return json.loads(LAST_SYNC_FILE.read_text())["last_sync_ts"]
        except (json.JSONDecodeError, KeyError):
            return None
    return None


def _save_last_sync(ts: int):
    LAST_SYNC_FILE.write_text(json.dumps({
        "last_sync_ts": ts,
        "last_sync_iso": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
    }, indent=2))


def _activity_cache_path(activity_id: int) -> Path:
    return ACTIVITIES_DIR / f"{activity_id}.json"


def _load_cached_activity(activity_id: int) -> Optional[dict]:
    p = _activity_cache_path(activity_id)
    if p.exists():
        return json.loads(p.read_text())
    return None


def _save_cached_activity(activity: dict):
    # Enrich before write so downstream consumers always read pre-computed fields
    activity = enrich(activity)
    p = _activity_cache_path(activity["id"])
    p.write_text(json.dumps(activity, indent=2))


def _all_cached_activities() -> list:
    """Load every activity JSON from the cache."""
    activities = []
    for p in ACTIVITIES_DIR.glob("*.json"):
        try:
            activities.append(json.loads(p.read_text()))
        except json.JSONDecodeError:
            continue
    return activities


def _format_csv_date(iso_str: str) -> str:
    """Convert '2026-04-29T22:34:00Z' to 'Apr 29, 2026, 10:34:00 PM'."""
    dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    # Match Strava export format: "Apr 29, 2026, 10:34:00 PM"
    return dt.strftime("%b %d, %Y, %I:%M:%S %p").replace(" 0", " ")


def _activity_to_csv_row(a: dict) -> list:
    """Convert a Strava API activity dict to a CSV row matching the export format.

    Note: many bulk-export-only fields (weather details, calories from FIT, etc)
    aren't returned by the API. Those columns are left blank.
    """
    distance_m = a.get("distance", 0) or 0
    moving_time = a.get("moving_time", 0) or 0
    elapsed_time = a.get("elapsed_time", 0) or 0

    # The CSV has 104 columns. We fill the ones we can, leave the rest blank.
    row = [""] * 104

    row[0] = str(a["id"])
    row[1] = _format_csv_date(a["start_date_local"])
    row[2] = a.get("name", "")
    row[3] = a.get("type", "")  # 'Run', 'Ride', 'Weight Training', etc.
    row[4] = a.get("description", "") or ""
    row[5] = str(elapsed_time)
    # First Distance column: km
    row[6] = f"{distance_m / 1000:.2f}" if distance_m else ""
    row[7] = str(a.get("max_heartrate", "")) if a.get("max_heartrate") else ""
    row[8] = str(a.get("suffer_score", "")) if a.get("suffer_score") else ""
    row[9] = ""  # commute
    row[10] = ""  # private note
    row[11] = a.get("gear_id", "") or ""

    # Second elapsed/moving/distance block (full precision, in m and s)
    row[15] = str(elapsed_time)
    row[16] = str(moving_time)
    row[17] = f"{distance_m:.1f}" if distance_m else ""  # meters
    row[18] = str(a.get("max_speed", "")) if a.get("max_speed") else ""
    row[19] = str(a.get("average_speed", "")) if a.get("average_speed") else ""
    row[20] = str(a.get("total_elevation_gain", "")) if a.get("total_elevation_gain") else ""
    row[21] = ""  # elevation loss
    row[22] = str(a.get("elev_low", "")) if a.get("elev_low") else ""
    row[23] = str(a.get("elev_high", "")) if a.get("elev_high") else ""

    row[28] = str(a.get("max_cadence", "")) if a.get("max_cadence") else ""
    row[29] = str(a.get("average_cadence", "")) if a.get("average_cadence") else ""
    row[30] = str(a.get("max_heartrate", "")) if a.get("max_heartrate") else ""
    row[31] = str(a.get("average_heartrate", "")) if a.get("average_heartrate") else ""
    row[32] = str(a.get("max_watts", "")) if a.get("max_watts") else ""
    row[33] = str(a.get("average_watts", "")) if a.get("average_watts") else ""
    row[34] = str(a.get("calories", "")) if a.get("calories") else ""

    row[37] = str(a.get("suffer_score", "")) if a.get("suffer_score") else ""

    row[44] = a.get("type", "")  # Type column appears twice
    row[45] = a.get("start_date_local", "")  # Start Time

    row[55] = "true" if a.get("commute") else ""
    row[56] = ""  # total weight lifted

    return row


def _read_existing_csv_rows() -> tuple[list, list]:
    """Read existing activities.csv. Returns (header, rows).

    Empty if the file doesn't exist.
    """
    if not CSV_PATH.exists():
        return CSV_HEADER.split(","), []

    with CSV_PATH.open("r", encoding="utf-8") as f:
        reader = csv.reader(f)
        try:
            header = next(reader)
        except StopIteration:
            return CSV_HEADER.split(","), []
        rows = list(reader)
    return header, rows


def _write_csv(api_activities: list):
    """Write activities.csv merging API data with existing CSV.

    Activities present in the API cache replace existing rows by Activity ID.
    Existing CSV rows for activities NOT in the cache are preserved (e.g. very
    old runs we haven't backfilled, or non-Run activities like Weight Training
    that come from the bulk export but not the API).

    Sorted by Activity Date, newest first.
    """
    # Existing CSV — preserve historical data
    existing_header, existing_rows = _read_existing_csv_rows()
    existing_by_id = {row[0]: row for row in existing_rows if row and row[0]}

    # API rows — these win for any ID we have in cache
    api_by_id = {}
    for a in api_activities:
        api_by_id[str(a["id"])] = _activity_to_csv_row(a)

    # Merge: API data wins, fall back to existing
    merged = {**existing_by_id, **api_by_id}

    # Sort by Activity Date (col 1) — newest first
    def parse_date_for_sort(row):
        try:
            return datetime.strptime(row[1].strip(), "%b %d, %Y, %I:%M:%S %p")
        except (ValueError, IndexError):
            return datetime.min

    rows = sorted(merged.values(), key=parse_date_for_sort, reverse=True)

    # Backup before overwrite
    if CSV_PATH.exists():
        backup = CSV_PATH.with_suffix(".csv.bak")
        backup.write_text(CSV_PATH.read_text(encoding="utf-8"), encoding="utf-8")

    with CSV_PATH.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(CSV_HEADER.split(","))
        for row in rows:
            # Pad/truncate to header length
            row = (row + [""] * 104)[:104]
            writer.writerow(row)

    api_only = sum(1 for aid in api_by_id if aid not in existing_by_id)
    preserved = sum(1 for aid in existing_by_id if aid not in api_by_id)
    overlap = sum(1 for aid in api_by_id if aid in existing_by_id)
    print(f"  CSV merge: {api_only} new from API, {overlap} updated, "
          f"{preserved} historical preserved")
    print(f"  Wrote {len(rows)} total activities to {CSV_PATH}")


def _load_sync_state() -> dict:
    if SYNC_STATE_FILE.exists():
        try:
            return json.loads(SYNC_STATE_FILE.read_text())
        except json.JSONDecodeError:
            pass
    return {"last_full_sync_ts": 0, "backfill_cursor_ts": 0}


def _save_sync_state(state: dict) -> None:
    SYNC_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    SYNC_STATE_FILE.write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------------------
# Mutex (file-based, single-host)
# ---------------------------------------------------------------------------

class SyncLockError(Exception):
    """Raised when another sync is running and we refuse to start."""
    pass


def _acquire_lock() -> None:
    """Acquire the sync lock. Raises SyncLockError if another sync is active.

    Lock file contains JSON: {pid, started_at}. Stale locks (>10min old) are
    reclaimed automatically.
    """
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    if LOCK_FILE.exists():
        try:
            existing = json.loads(LOCK_FILE.read_text())
            age = time.time() - existing.get("started_at", 0)
            if age < LOCK_STALE_SEC:
                raise SyncLockError(
                    f"Sync already in progress (PID {existing.get('pid')}, "
                    f"started {age:.0f}s ago). Refusing to run concurrently."
                )
            else:
                print(f"  [strava_sync] Stale lock detected ({age:.0f}s old). Reclaiming.")
        except (json.JSONDecodeError, OSError):
            pass  # corrupt lock, reclaim it

    LOCK_FILE.write_text(json.dumps({
        "pid": os.getpid(),
        "started_at": time.time(),
        "started_iso": datetime.now(timezone.utc).isoformat(),
    }))


def _release_lock() -> None:
    try:
        LOCK_FILE.unlink()
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# Set-based diff
# ---------------------------------------------------------------------------

def _all_cached_ids() -> set:
    """Set of activity IDs currently in the cache (as strings)."""
    if not ACTIVITIES_DIR.exists():
        return set()
    return {p.stem for p in ACTIVITIES_DIR.glob("*.json")}


def _mark_deleted(activity_id: str) -> None:
    """Soft-delete: mark cached activity with _deleted_at timestamp.

    Doesn't remove the JSON (audit trail). analysis modules should filter on
    activity.get('_deleted_at') is None.
    """
    p = ACTIVITIES_DIR / f"{activity_id}.json"
    if not p.exists():
        return
    try:
        a = json.loads(p.read_text())
        if "_deleted_at" not in a:
            a["_deleted_at"] = datetime.now(timezone.utc).isoformat()
            p.write_text(json.dumps(a, indent=2))
    except json.JSONDecodeError:
        pass


def sync(full: bool = False, backfill_days: Optional[int] = None,
         fetch_detail: bool = True, backfill_enrich: bool = True,
         max_per_run: int = DEFAULT_MAX_PER_RUN) -> dict:
    """Pull activities from Strava API into the local cache + CSV.

    Parameters
    ----------
    full : bool
        If True, re-sync everything from the beginning of time.
    backfill_days : int, optional
        Pull activities from the last N days. Useful for first sync.
    fetch_detail : bool
        Whether to fetch full activity detail (HR avg, laps available, etc).
        Set False for fast sync. Default True.

    Returns
    -------
    dict
        Summary with new_count, updated_count, total_in_cache, last_sync_ts.
    """
    _ensure_dirs()

    # Acquire mutex (refuses concurrent syncs)
    _acquire_lock()

    try:
        return _sync_inner(full, backfill_days, fetch_detail,
                            backfill_enrich, max_per_run)
    finally:
        _release_lock()


def _sync_inner(full: bool, backfill_days: Optional[int],
                fetch_detail: bool, backfill_enrich: bool,
                max_per_run: int) -> dict:
    # Eager enrichment backfill — on startup, enrich any cached activities
    # whose enrichment version is stale. Idempotent + cheap if all are current.
    if backfill_enrich:
        backfill_enrichment(ACTIVITIES_DIR)

    api = StravaAPI()
    state = _load_sync_state()

    # Determine 'after' timestamp
    if full:
        after = None
    elif backfill_days:
        after = int(time.time()) - backfill_days * 86400
    else:
        last = _last_sync_ts()
        # Re-fetch a small overlap (3 days) in case activities were edited
        after = (last - 3 * 86400) if last else None

    print(f"  Fetching activities from Strava (after={after})...")
    summaries = api.list_all_activities(after=after)
    print(f"  Got {len(summaries)} activities from API")

    # Set-based diff: compute new vs already-cached
    cached_ids = _all_cached_ids()
    api_ids = {str(a["id"]) for a in summaries}
    new_ids = api_ids - cached_ids
    overlap_ids = api_ids & cached_ids

    # Detection of deletes only meaningful on a full sync (otherwise the
    # "missing from window" set is an artifact of the time filter)
    deleted = []
    if full or after is None:
        deleted = list(cached_ids - api_ids)
        for did in deleted:
            _mark_deleted(did)

    print(f"  Diff: {len(new_ids)} new, {len(overlap_ids)} known, "
          f"{len(deleted)} marked deleted")

    new_count = 0
    updated_count = 0
    processed = 0
    remaining = 0

    for i, summary in enumerate(summaries, 1):
        aid = summary["id"]
        cached = _load_cached_activity(aid)

        # Chunking: if we've processed max_per_run NEW activities, stop and
        # save state so the next call resumes
        if processed >= max_per_run:
            remaining = len(summaries) - i + 1
            print(f"    [chunk] Hit max_per_run={max_per_run}, "
                  f"{remaining} remaining. Run again to continue.")
            state["backfill_cursor_ts"] = int(summary.get("start_date_local_unix",
                                              time.time()))
            break

        # Strava 'summary' lacks some fields (avg HR, calories) — fetch detail.
        if fetch_detail and summary.get("type") == "Run":
            try:
                detail = api.get_activity(aid)
                merged = {**summary, **detail}
            except StravaAPIError as e:
                print(f"    [warn] Failed to fetch detail for {aid}: {e}")
                merged = summary
        else:
            merged = summary

        if cached is None:
            new_count += 1
            processed += 1  # only chunked count is new fetches
        elif cached.get("upload_id") != merged.get("upload_id") or \
             cached.get("name") != merged.get("name"):
            updated_count += 1

        _save_cached_activity(merged)

        if i % 25 == 0:
            print(f"    Cached {i}/{len(summaries)}...")

    # Rebuild CSV from full cache (handles edits + deletes from re-syncs)
    all_activities = _all_cached_activities()
    # Filter soft-deleted from CSV
    all_activities = [a for a in all_activities if not a.get("_deleted_at")]
    _write_csv(all_activities)

    # Update state
    if remaining == 0:
        state["backfill_cursor_ts"] = 0  # backfill complete
    if full or after is None:
        state["last_full_sync_ts"] = int(time.time())
    _save_sync_state(state)

    # Auto-regen dashboard at end of successful sync
    try:
        from dashboard import generate_dashboard
        generate_dashboard()
    except ImportError:
        pass  # dashboard.py not present yet (during build)
    except Exception as e:
        print(f"  [warn] Dashboard regen failed: {e}")

    now_ts = int(time.time())
    _save_last_sync(now_ts)

    summary = {
        "new_count": new_count,
        "updated_count": updated_count,
        "deleted_count": len(deleted),
        "remaining": remaining,
        "total_in_cache": len(all_activities),
        "last_sync_ts": now_ts,
    }
    print()
    print("=" * 50)
    print(f"  New activities:     {summary['new_count']}")
    print(f"  Updated:            {summary['updated_count']}")
    print(f"  Marked deleted:     {summary['deleted_count']}")
    if remaining:
        print(f"  Remaining (chunk):  {remaining} -- run again to continue")
    print(f"  Total in cache:     {summary['total_in_cache']}")
    print(f"  Last sync:          {datetime.fromtimestamp(now_ts).isoformat()}")
    print(f"  API rate limits:    {api.rate_limits}")
    print("=" * 50)
    return summary


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    parser = argparse.ArgumentParser(description="Sync Strava activities to local CSV.")
    parser.add_argument("--full", action="store_true",
                        help="Full re-sync from Strava (slow first time)")
    parser.add_argument("--backfill", type=int, metavar="DAYS",
                        help="Pull activities from the last N days")
    parser.add_argument("--no-detail", action="store_true",
                        help="Skip per-activity detail fetch (faster, less data)")
    parser.add_argument("--reenrich", action="store_true",
                        help="Force re-enrichment of all cached activities even if up to date")
    parser.add_argument("--no-backfill-enrich", action="store_true",
                        help="Skip the eager enrichment backfill at sync start")
    parser.add_argument("--max-per-run", type=int, default=DEFAULT_MAX_PER_RUN,
                        metavar="N",
                        help=f"Cap activities processed per invocation (default {DEFAULT_MAX_PER_RUN}). "
                             "Useful for big backfills under rate limits.")
    args = parser.parse_args()

    if args.reenrich:
        # Force re-enrich by zeroing the version on every cached file
        _ensure_dirs()
        import json as _json
        n = 0
        for p in ACTIVITIES_DIR.glob("*.json"):
            try:
                a = _json.loads(p.read_text())
                a.pop("_enriched_v", None)
                a = enrich(a)
                p.write_text(_json.dumps(a, indent=2))
                n += 1
            except _json.JSONDecodeError:
                continue
        print(f"  [strava_sync] Re-enriched {n} activities.")
        return

    try:
        sync(full=args.full, backfill_days=args.backfill,
             fetch_detail=not args.no_detail,
             backfill_enrich=not args.no_backfill_enrich,
             max_per_run=args.max_per_run)
    except SyncLockError as e:
        print(f"  [strava_sync] {e}")
        sys.exit(2)


if __name__ == "__main__":
    main()
