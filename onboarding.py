"""config.json from what the Strava MCP already knows: no questionnaire.

    python3 coach.py init --from-mcp [data/mcp/] [--goal-time 3:45:00]
        [--race-name "..."] [--race-date YYYY-MM-DD] [--distance 26.2|42.2km]
        [--units mi|km] [--name "..."] [--no-race] [--no-plan] [--force] [--dry-run]

Inputs, all optional: data/mcp/profile.json (get_athlete_profile: name,
measurement preference, the event you told Strava you are training for),
data/mcp/zones.json (get_athlete_zones: HR and pace zones) and the ingested
activity cache (observed max HR, your actual easy pace, trailing volume).
Anything it could not derive is listed under NEEDS; the goal time is the
one thing worth asking the athlete for. Writes config.json (never over an
existing one without --force, and then only after a backup), re-enriches
the cache so TSS follows the new zones, and generates a plan.
"""

import argparse
import copy
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import config
import mcp_adapter
import units
import wizard
from enrichment import is_real_run

MI_M = 1609.34
KM_PER_MI = units.KM_PER_MI
MIN_HR_RUNS = 3            # runs with HR before observed max HR is trusted
MAX_HR_AGREE_BPM = 5       # the top reading needs a second run this close
EASY_BAND_HALF_WIDTH = 0.25  # min/mi either side of the observed easy pace
EASY_PACE_DAYS = 90
_HERE = Path(__file__).resolve().parent

# Order matters: "half marathon" must not read as a marathon.
_DISTANCE_WORDS = [
    (re.compile(r"\bhalf\b", re.I), 13.1),
    (re.compile(r"\bmarathon\b", re.I), 26.2),
    (re.compile(r"\b10\s*k\b", re.I), 6.21),
    (re.compile(r"\b5\s*k\b", re.I), 3.11),
    (re.compile(r"\b10\s*mi(?:le|ler)?s?\b", re.I), 10.0),
]
_DISTANCE_NUM = re.compile(r"(\d+(?:\.\d+)?)\s*(km|k|mi|miles?)\b", re.I)
_NAME_PREFIX = re.compile(
    r"^(?:(?:a\s+)?pr\s+(?:at|in)\s+|training\s+for\s+|run(?:ning)?\s+|finish(?:ing)?\s+|"
    r"race\s+|racing\s+|sub[- ]?\d[\d:]*\s+(?:at|in)\s+)?(?:the\s+)?", re.I)


# ---------------------------------------------------------------------------
# Inbox
# ---------------------------------------------------------------------------

def read_inbox(inbox) -> tuple:
    """(profile, zones) from data/mcp/profile.json and zones.json; None when absent."""
    inbox = Path(inbox)
    out = []
    for stem in ("profile", "zones"):
        payload = None
        for cand in (inbox / f"{stem}.json", inbox / "athlete.json" if stem == "profile" else None):
            if cand and cand.exists():
                try:
                    payload = mcp_adapter._read_json(cand)
                except (OSError, ValueError, json.JSONDecodeError):
                    payload = None
                if isinstance(payload, dict):
                    break
        out.append(payload if isinstance(payload, dict) else None)
    return tuple(out)


# ---------------------------------------------------------------------------
# Derivations (pure)
# ---------------------------------------------------------------------------

def profile_name(profile: Optional[dict]) -> Optional[str]:
    p = profile or {}
    if isinstance(p.get("athlete"), dict):
        p = p["athlete"]
    name = " ".join(str(p.get(k) or "").strip() for k in ("first_name", "last_name")).strip()
    return name or (str(p.get("name") or "").strip() or None)


def profile_units(profile: Optional[dict]) -> Optional[str]:
    p = profile or {}
    if isinstance(p.get("athlete"), dict):
        p = p["athlete"]
    pref = str(p.get("measurement_preference") or "").lower()
    if pref.startswith("met"):
        return "km"
    if pref.startswith("imp") or pref in ("feet", "miles"):
        return "mi"
    return None


def distance_from_text(text: str) -> Optional[float]:
    """Race distance in miles from free text ('PR at the NYC Marathon', '42.2 km')."""
    text = text or ""
    m = _DISTANCE_NUM.search(text)
    if m:
        val, unit = float(m.group(1)), m.group(2).lower()
        return round(val / KM_PER_MI, 2) if unit.startswith("k") else val
    for rx, mi in _DISTANCE_WORDS:
        if rx.search(text):
            return mi
    return None


def race_name_from_text(text: str) -> str:
    name = _NAME_PREFIX.sub("", (text or "").strip(), count=1).strip(" .!")
    return name or "Goal Race"


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return s or "goal_race"


def race_from_profile(profile: Optional[dict], today: Optional[date] = None) -> Optional[dict]:
    """The event Strava says you are training for, as a race stub.

    Strava's focus expires the day AFTER the event, so the race date is the
    expiry minus one day; the athlete confirms it in the same breath as the
    goal time."""
    p = profile or {}
    if isinstance(p.get("athlete"), dict) and "current_focus" not in p:
        p = p["athlete"]
    focus = p.get("current_focus") or {}
    kind = re.sub(r"[^a-z]", "", str(focus.get("focus_type") or "").lower())
    if kind not in ("trainforevent", "event", "race"):
        return None
    text = str(focus.get("open_text") or focus.get("name") or "").strip()
    raw = str(focus.get("expires_at_local") or focus.get("expires_at") or "")[:10]
    try:
        expiry = date.fromisoformat(raw)
    except ValueError:
        return None
    race_date = expiry - timedelta(days=1)
    return {"name": race_name_from_text(text), "date": race_date.isoformat(),
            "distance_mi": distance_from_text(text), "open_text": text,
            "source": "strava_focus"}


def observed_max_hr(acts: list) -> tuple:
    """(max_hr or None, note). The top reading counts only when another run
    came within MAX_HR_AGREE_BPM of it; otherwise the second-highest stands
    (a lone spike is usually the optical sensor). It is a floor: a race or a
    hard 5k will raise it."""
    vals = []
    for a in acts:
        if a.get("type") != "Run" or not is_real_run(a):
            continue
        try:
            v = float(a.get("max_heartrate") or 0)
        except (TypeError, ValueError):
            continue
        if 120 <= v <= 230:
            vals.append(v)
    vals.sort(reverse=True)
    if len(vals) < MIN_HR_RUNS:
        return None, (f"max_hr left at the default: fewer than {MIN_HR_RUNS} runs with heart rate "
                      "(unverified)")
    top, second = vals[0], vals[1]
    if top - second <= MAX_HR_AGREE_BPM:
        return int(round(top)), (f"max_hr {top:.0f}: highest recorded, matched within "
                                 f"{MAX_HR_AGREE_BPM} bpm by another run (a floor; a race will raise it)")
    return int(round(second)), (f"max_hr {second:.0f}: second-highest recorded ({top:.0f} stood alone "
                                "and reads as a sensor spike)")


def _run_date(a: dict) -> Optional[date]:
    try:
        return date.fromisoformat(str(a.get("start_date_local") or a.get("start_date"))[:10])
    except (TypeError, ValueError):
        return None


def easy_pace_from_data(acts: list, easy_cap: float, today: Optional[date] = None,
                        days: int = EASY_PACE_DAYS) -> Optional[tuple]:
    """(floor, ceiling) in min/mi around the median pace of recent runs that
    averaged under the easy cap. Needs MIN_HR_RUNS such runs."""
    today = today or date.today()
    since = today - timedelta(days=days)
    paces = []
    for a in acts:
        if a.get("type") != "Run" or not is_real_run(a):
            continue
        d = _run_date(a)
        hr = a.get("average_heartrate")
        dist_mi = (a.get("distance") or 0) / MI_M
        mt = a.get("moving_time") or 0
        if d is None or d < since or not hr or hr >= easy_cap or dist_mi < 2 or mt <= 0:
            continue
        paces.append((mt / 60) / dist_mi)
    if len(paces) < MIN_HR_RUNS:
        return None
    med = statistics.median(paces)
    return (round(med + EASY_BAND_HALF_WIDTH, 2), round(med - EASY_BAND_HALF_WIDTH, 2))


def trailing_mpw(acts: list, today: Optional[date] = None, weeks: int = 4) -> float:
    today = today or date.today()
    since = today - timedelta(weeks=weeks)
    mi = sum((a.get("distance") or 0) / MI_M for a in acts
             if a.get("type") == "Run" and is_real_run(a)
             and (d := _run_date(a)) is not None and since < d <= today)
    return mi / weeks


def local_timezone() -> Optional[str]:
    tz = os.environ.get("TZ")
    if not tz:
        try:
            target = os.readlink("/etc/localtime")
            tz = target.split("zoneinfo/", 1)[1] if "zoneinfo/" in target else None
        except OSError:
            tz = None
    if not tz:
        return None
    try:
        import zoneinfo
        zoneinfo.ZoneInfo(tz)
    except Exception:
        return None
    return tz


def _parse_distance(text, unit: str) -> Optional[float]:
    if text is None:
        return None
    s = str(text).strip().lower()
    m = re.match(r"^(\d+(?:\.\d+)?)\s*(km|k|mi|miles?)?$", s)
    if not m:
        return distance_from_text(s)
    val, u = float(m.group(1)), (m.group(2) or unit)
    return round(val / KM_PER_MI, 2) if u.startswith("k") else val


def _goal_sec(goal_time: str) -> int:
    try:
        return config.goal_time_to_sec(goal_time)
    except Exception:
        return 0


def _fmt_hms(sec: int) -> str:
    sec = int(sec)
    return f"{sec // 3600}:{(sec % 3600) // 60:02d}:{sec % 60:02d}"


def _estimated_goal(acts: list, dist_mi: float, today: date) -> Optional[tuple]:
    """(goal_time, vdot) from current fitness, as a starting point only."""
    try:
        import race_predictor as rp
        rows = rp.load_activities(acts)
        if not rows:
            return None
        vdot, _src = rp.current_fitness_vdot(rows, today=datetime(today.year, today.month, today.day))
        sec = rp.predict_race_time(vdot, dist_mi * MI_M)
        sec -= sec % 60
        return _fmt_hms(sec), round(vdot, 1)
    except Exception:
        return None


def plan_config(profile: Optional[dict], zones: Optional[dict], cache_acts: list,
                answers: Optional[dict] = None, today: Optional[date] = None) -> tuple:
    """-> (cfg, needs, notes). Pure: reads the example config and the inputs."""
    today = today or date.today()
    answers = answers or {}
    cfg = copy.deepcopy(json.loads(config.EXAMPLE_PATH.read_text(encoding="utf-8")))
    needs, notes = [], []
    ath = cfg["athlete"]

    name = answers.get("name") or profile_name(profile)
    if name:
        ath["name"] = name
        notes.append(f"name: {name} ({'--name' if answers.get('name') else 'Strava profile'})")
    unit = answers.get("units") or profile_units(profile)
    if unit:
        ath["units"] = unit
        notes.append(f"units: {unit} ({'--units' if answers.get('units') else 'Strava measurement preference'})")
    unit = ath["units"]

    if zones:
        patch, znotes = wizard.zones_to_config_patch(zones)
        wizard._deep_merge(cfg, patch)
        notes += [f"zones: {n}" for n in znotes]
    else:
        notes.append("zones: no zones.json in the inbox; HR caps and bands are the example defaults")

    mx, note = observed_max_hr(cache_acts)
    if mx and mx <= int(ath.get("threshold_hr") or 0):
        notes.append(f"observed max HR {mx} sits below the zone-derived threshold HR "
                     f"{ath['threshold_hr']}; max_hr kept at {ath['max_hr']} (check both)")
    elif mx:
        ath["max_hr"] = mx
        notes.append(note)
    else:
        notes.append(note)

    band = easy_pace_from_data(cache_acts, float(ath["easy_hr_cap"]), today)
    if band:
        cfg["pace_zones"]["easy"] = {"floor": band[0], "ceiling": band[1]}
        cfg["pace_zones"]["long_run"] = {"pace": round(band[0], 2)}
        notes.append(f"easy pace: {units.fmt_pace(band[1], label=False)}-{units.fmt_pace(band[0], label=False)}/mi, "
                     f"the median of your runs under HR {ath['easy_hr_cap']} in the last {EASY_PACE_DAYS} days")
    elif zones and len(zones.get("run_zones") or []) >= 2:
        z2 = zones["run_zones"][1]
        slow, fast = wizard._pace_from_mps(z2.get("min")), wizard._pace_from_mps(z2.get("max"))
        if slow and fast:
            cfg["pace_zones"]["easy"] = {"floor": slow, "ceiling": fast}
            cfg["pace_zones"]["long_run"] = {"pace": round((slow + fast) / 2, 2)}
            notes.append("easy pace: Strava run zone 2 (formulaic; your own easy runs will replace it)")
    else:
        notes.append("easy pace: example defaults (too few easy runs with heart rate to read it)")

    # --- race ---
    race = None
    if not answers.get("no_race"):
        stub = race_from_profile(profile, today) or {}
        name_r = answers.get("race_name") or stub.get("name")
        date_r = answers.get("race_date") or stub.get("date")
        dist = _parse_distance(answers.get("distance"), unit) if answers.get("distance") else stub.get("distance_mi")
        if name_r and date_r:
            try:
                date.fromisoformat(str(date_r))
            except ValueError:
                date_r = None
        if name_r and date_r:
            if not dist:
                dist = 26.2
                needs.append("distance")
                notes.append("race distance not stated; assumed a marathon (26.2 mi), confirm it")
            src = "flags" if (answers.get("race_name") or answers.get("race_date")) else "Strava training focus"
            goal = answers.get("goal_time")
            if goal and _goal_sec(goal) <= 0:
                notes.append(f"--goal-time {goal!r} is not H:MM:SS; ignored")
                goal = None
            if not goal:
                est = _estimated_goal(cache_acts, float(dist), today)
                if est:
                    goal = est[0]
                    notes.append(f"goal time {goal} is ESTIMATED from current fitness (VDOT {est[1]}); "
                                 "set your own with --goal-time")
                else:
                    goal = _fmt_hms(int(float(dist) * 10 * 60))
                    notes.append(f"goal time {goal} is a placeholder (10:00/mi); set it with --goal-time")
                needs.append("goal_time")
            sec = _goal_sec(goal)
            race = {
                "id": _slug(name_r),
                "name": name_r,
                "date": str(date_r),
                "distance_mi": float(dist),
                "goal_time": goal,
                "goal_pace_min_per_mi": round(sec / 60 / float(dist), 3),
                "start_time_local": "08:00",
            }
            try:
                from race_predictor import compute_vdot
                race["vdot_required"] = round(compute_vdot(float(dist) * MI_M, sec), 1)
            except Exception:
                pass
            cfg["races"] = [race]
            cfg["active_race"] = race["id"]
            cfg["pace_zones"].setdefault("race_pace", {})
            cfg["pace_zones"]["race_pace"]["ceiling"] = round(race["goal_pace_min_per_mi"], 2)
            cfg["pace_zones"]["race_pace"]["floor"] = round(race["goal_pace_min_per_mi"] + 0.17, 2)
            notes.append(f"race: {name_r} on {date_r}, {units.fmt_dist(mi=float(dist))}, goal {goal} ({src}"
                         + ("; date is the Strava focus expiry minus a day" if src != "flags" else "") + ")")
        else:
            needs.append("race")
            notes.append("race: none found in the Strava profile; the example races stay until you set one "
                         "(--race-name, --race-date, --distance, --goal-time)")
    else:
        notes.append("race: skipped (--no-race); the example races stay")
    cfg["race_history"] = []

    mpw = trailing_mpw(cache_acts, today)
    if mpw > 0:
        base = max(10, int(5 * round(mpw / 5)))
        cfg.setdefault("scenario", {})["entries"] = [base, base + 5, base + 10]
        notes.append(f"scenario entries {cfg['scenario']['entries']} mi/wk around your trailing "
                     f"{mpw:.0f} mi/wk")

    tz = local_timezone()
    if tz:
        cfg.setdefault("calendar", {})["timezone"] = tz
        notes.append(f"calendar timezone {tz} (this machine)")

    meta = cfg.setdefault("_meta", {})
    meta["description"] = (f"Written by `coach.py init --from-mcp` on {today.isoformat()} from your Strava "
                           "profile, zones and history. Edit freely; config.json is gitignored.")
    meta["written_on"] = today.isoformat()
    meta["notes"] = notes
    meta["needs"] = needs
    return cfg, needs, notes


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def apply(cfg: dict, force: bool = False) -> tuple:
    """Write config.json. Refuses to overwrite without force; backs up first."""
    path = config.CONFIG_PATH
    backup = None
    if path.exists():
        if not force:
            raise FileExistsError(f"{path} exists. Re-run with --force to replace it "
                                  "(a timestamped backup is kept).")
        backup = path.with_name(f"config.json.bak-{datetime.now():%Y%m%d%H%M%S}")
        shutil.copy2(path, backup)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    return path, backup


def _summary_lines(cfg: dict, needs: list, notes: list) -> list:
    ath = cfg["athlete"]
    lines = ["CONFIG FROM STRAVA",
             f"  Athlete:  {ath['name']}  |  units {ath['units']}  |  max HR {ath['max_hr']}  |  "
             f"easy cap {ath['easy_hr_cap']}  |  LT {ath['threshold_hr']}"]
    for r in cfg.get("races") or []:
        if r.get("id") == cfg.get("active_race"):
            lines.append(f"  Race:     {r['name']} on {r['date']}, {units.fmt_dist(mi=r['distance_mi'])}, "
                         f"goal {r.get('goal_time')} ({units.fmt_pace(r['goal_pace_min_per_mi'])})")
    lines.append("  Derived:")
    lines += [f"    - {n}" for n in notes]
    if needs:
        lines.append("  NEEDS (ask the athlete, then re-run with the flag or edit config.json):")
        lines += [f"    - {n}" for n in needs]
    return lines


def _parse(argv: list) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="coach.py init --from-mcp",
                                 description="config.json from the Strava MCP inbox + cache.")
    ap.add_argument("--from-mcp", nargs="?", const="", default="", metavar="INBOX",
                    help="inbox directory (default data/mcp/)")
    ap.add_argument("--goal-time", help="H:MM:SS for the race")
    ap.add_argument("--race-name")
    ap.add_argument("--race-date", help="YYYY-MM-DD")
    ap.add_argument("--distance", help="26.2, 42.2km, half, 10k ...")
    ap.add_argument("--units", choices=["mi", "km"])
    ap.add_argument("--name")
    ap.add_argument("--no-race", action="store_true")
    ap.add_argument("--no-plan", action="store_true", help="skip generating a plan afterwards")
    ap.add_argument("--force", action="store_true", help="replace an existing config.json (backed up)")
    ap.add_argument("--dry-run", action="store_true", help="show what would be written; write nothing")
    return ap.parse_args(argv)


def main(argv: Optional[list] = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    a = _parse(sys.argv[1:] if argv is None else list(argv))
    inbox = Path(a.from_mcp) if a.from_mcp else Path(mcp_adapter.MCP_DIR)
    profile, zones = read_inbox(inbox)
    acts = mcp_adapter._all_cached(Path(mcp_adapter.CACHE_DIR))
    answers = {"goal_time": a.goal_time, "race_name": a.race_name, "race_date": a.race_date,
               "distance": a.distance, "units": a.units, "name": a.name, "no_race": a.no_race}
    cfg, needs, notes = plan_config(profile, zones, acts, answers)

    print(f"  Inbox {inbox}: profile {'yes' if profile else 'no'}, zones {'yes' if zones else 'no'}; "
          f"{len(acts)} cached activities")
    print("\n".join(_summary_lines(cfg, needs, notes)))
    if a.dry_run:
        print("\n  Dry run: nothing written.")
        return 0
    try:
        path, backup = apply(cfg, force=a.force)
    except FileExistsError as e:
        print(f"\n  {e}")
        return 2
    print(f"\n  Wrote {path}" + (f" (previous copy: {backup.name})" if backup else ""))

    # Fresh processes: config is cached per process, and both steps must see the new one.
    if acts:
        subprocess.call([sys.executable, str(_HERE / "enrichment.py"), "--force"])
        if not a.no_plan:
            subprocess.call([sys.executable, str(_HERE / "plan_generator.py"), "--from-data"])
    elif not a.no_plan:
        print("  No activities in the cache yet, so no plan was generated: ingest your history, then "
              "`python3 coach.py plan --from-data`.")
    print("\n  Next: `python3 coach.py` for the full report.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
