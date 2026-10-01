"""Central configuration loader.

Single source of truth for athlete physiology, pace zones, races, theme, and
calendar defaults. Every other module reads from here instead of hardcoding
constants, so the whole system is personalized by editing one JSON file.

Design notes:
- Stdlib only (json + pathlib + datetime). No pyyaml/pydantic.
- This module imports nothing from the project, so it sits at the bottom of the
  import graph and can never cause an import cycle.
- `config.json` is gitignored. If it is missing, we fall back to
  `config.example.json` so a fresh clone (and CI) still runs.

Usage:
    import config
    cfg = config.load_config()
    hr = config.max_hr()
    race = config.active_race()        # resolved race dict for "today"
"""

import json
import os
import sys
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Optional

_CODE_DIR = Path(__file__).parent
_HERE = _CODE_DIR  # legacy alias


# ---------------------------------------------------------------------------
# Where state lives (the "home" directory)
# ---------------------------------------------------------------------------
#
# The code can run from a clone (state next to the code), from a Claude Code
# plugin install (code under ~/.claude/plugins/..., which is replaced on
# update, so state must live elsewhere), or anywhere else with
# STRAVA_COACH_HOME pointing at a writable directory. Shipped files
# (config.example.json) stay next to the code; everything personal or
# generated lives under HOME.

PLUGIN_ID = "strava-run-coach"


def _plugin_data_home(code_dir: Path) -> Optional[Path]:
    """If `code_dir` sits under a Claude plugins tree (.../.claude/plugins/...),
    return that tree's persistent data dir for this plugin, else None.

    Mirrors ${CLAUDE_PLUGIN_DATA} = ~/.claude/plugins/data/<plugin-id>/, which
    survives plugin updates. Pure: no filesystem access.
    """
    parts = Path(code_dir).parts
    for i in range(len(parts) - 1):
        if parts[i] == ".claude" and parts[i + 1] == "plugins":
            return Path(*parts[: i + 2]) / "data" / PLUGIN_ID
    return None


def home() -> Path:
    """Resolve the state directory: $STRAVA_COACH_HOME, else the plugin data
    dir when running from a plugin install, else the code directory."""
    env = os.environ.get("STRAVA_COACH_HOME")
    if env:
        return Path(env).expanduser()
    plug = _plugin_data_home(_CODE_DIR.resolve())
    return plug if plug is not None else _CODE_DIR


HOME = home()
DATA_DIR = HOME / "data"
CACHE_DIR = DATA_DIR / "strava_cache"
ACTIVITIES_DIR = CACHE_DIR / "activities"
STREAMS_DIR = CACHE_DIR / "streams"
MCP_DIR = DATA_DIR / "mcp"
CSV_PATH = HOME / "activities.csv"
CONFIG_PATH = HOME / "config.json"
STATE_PATH = DATA_DIR / "plan_state.json"
PLAN_OUTPUT_DIR = HOME / "plan_output"
ENV_PATH = HOME / ".env"
LOCK_FILE = DATA_DIR / ".sync.lock"
EXAMPLE_PATH = _CODE_DIR / "config.example.json"

_REQUIRED_TOP = {"athlete", "pace_zones", "races", "theme"}
_REQUIRED_RACE = {"id", "name", "date", "distance_mi", "goal_pace_min_per_mi"}


@lru_cache(maxsize=1)
def load_config() -> dict:
    """Load and validate config.json, falling back to config.example.json.

    Cached after first call. Call `reload()` in tests to pick up edits.
    """
    path = CONFIG_PATH if CONFIG_PATH.exists() else EXAMPLE_PATH
    if not path.exists():
        raise FileNotFoundError(
            "No config found. Expected config.json or config.example.json "
            f"in {_HERE}."
        )
    if path == EXAMPLE_PATH:
        print(
            "[config] config.json not found; using config.example.json. "
            "Run `python3 coach.py init` to create your own.",
            file=sys.stderr,
        )
    cfg = json.loads(path.read_text(encoding="utf-8"))
    _validate(cfg)
    return cfg


def reload() -> dict:
    """Clear the cache and reload. Useful in tests after editing config."""
    load_config.cache_clear()
    return load_config()


def _validate(cfg: dict) -> None:
    missing = _REQUIRED_TOP - set(cfg.keys())
    if missing:
        raise ValueError(f"config missing top-level keys: {sorted(missing)}")

    races = cfg.get("races")
    if not isinstance(races, list) or not races:
        raise ValueError("config.races must be a non-empty list")

    ids = set()
    for r in races:
        rmissing = _REQUIRED_RACE - set(r.keys())
        if rmissing:
            raise ValueError(
                f"race {r.get('id', '?')} missing keys: {sorted(rmissing)}"
            )
        try:
            date.fromisoformat(r["date"])
        except (ValueError, TypeError) as e:
            raise ValueError(f"race {r['id']} has bad date: {e}")
        if r["id"] in ids:
            raise ValueError(f"duplicate race id: {r['id']}")
        ids.add(r["id"])

    ar = cfg.get("active_race", "auto")
    if ar not in ("auto", None, "") and ar not in ids:
        raise ValueError(
            f"active_race '{ar}' is not 'auto' and does not match any race id"
        )

    # race_history is optional; validate entries only when present
    for h in cfg.get("race_history", []) or []:
        try:
            date.fromisoformat(h["date"])
        except (KeyError, ValueError, TypeError) as e:
            raise ValueError(f"race_history entry {h.get('id', '?')} has bad date: {e}")
        if not (h.get("distance_mi") or 0) > 0:
            raise ValueError(f"race_history entry {h.get('id', '?')} needs distance_mi > 0")
        if goal_time_to_sec(h.get("result_time", "")) <= 0:
            raise ValueError(
                f"race_history entry {h.get('id', '?')} has unparseable result_time"
            )


# ---------------------------------------------------------------------------
# Athlete getters
# ---------------------------------------------------------------------------

def athlete() -> dict:
    return load_config()["athlete"]


def athlete_name() -> str:
    return athlete().get("name", "Athlete")


def units() -> str:
    return athlete().get("units", "mi")


def max_hr() -> int:
    return int(athlete()["max_hr"])


def threshold_hr() -> int:
    return int(athlete()["threshold_hr"])


def easy_hr_cap() -> int:
    return int(athlete()["easy_hr_cap"])


def recovery_hr_cap() -> int:
    return int(athlete().get("recovery_hr_cap", 130))


def long_run_hr_cap() -> int:
    return int(athlete().get("long_run_hr_cap", 145))


def threshold_pace() -> float:
    """Threshold pace in minutes per mile."""
    return float(athlete()["threshold_pace_min_per_mi"])


def vo2_pct_max() -> float:
    """Fraction of max HR above which effort reads as VO2max work (default 0.90)."""
    return float(athlete().get("vo2_pct_max", 0.90))


def hr_source() -> str:
    """'wrist' (optical, the default) or 'chest' (strap). Wording only."""
    return str(athlete().get("hr_source", "wrist"))


# ---------------------------------------------------------------------------
# Pace zones
# ---------------------------------------------------------------------------

def pace_zones() -> dict:
    return load_config()["pace_zones"]


def tempo_hr_upper() -> int:
    """Upper bound of the tempo HR band (boundary into threshold)."""
    return int(pace_zones().get("tempo", {}).get("hr_range", [150, 160])[1])


def threshold_hr_upper() -> int:
    """Upper bound of the threshold HR band (boundary into vo2max)."""
    return int(pace_zones().get("threshold", {}).get("hr_range", [160, 170])[1])


def race_pace_hr_range() -> list:
    """[lo, hi] HR band for marathon/race-pace work. Falls back to the tempo
    band's floor and the legacy `race_pace.hr_cap`."""
    rp = pace_zones().get("race_pace", {}) or {}
    rng = rp.get("hr_range")
    if isinstance(rng, (list, tuple)) and len(rng) == 2:
        return [int(rng[0]), int(rng[1])]
    lo = int(pace_zones().get("tempo", {}).get("hr_range", [150, 160])[0])
    hi = int(rp.get("hr_cap", max(lo, easy_hr_cap() + 14)))
    return [lo, max(lo, hi)]


def zone_edges() -> list:
    """HR bands as (name, lo, hi) tuples, lo inclusive / hi exclusive, in
    ascending order and guaranteed monotonic: recovery, easy, steady, mp,
    threshold, vo2. Derived entirely from config."""
    rec = recovery_hr_cap()
    easy = max(easy_hr_cap(), rec)
    mp_lo, mp_hi = race_pace_hr_range()
    mp_lo = max(mp_lo, easy)
    mp_hi = max(mp_hi, mp_lo)
    vo2 = max(int(round(max_hr() * vo2_pct_max())), mp_hi)
    return [
        ("recovery", 0, rec),
        ("easy", rec, easy),
        ("steady", easy, mp_lo),
        ("mp", mp_lo, mp_hi),
        ("threshold", mp_hi, vo2),
        ("vo2", vo2, 999),
    ]


# ---------------------------------------------------------------------------
# Theme + calendar
# ---------------------------------------------------------------------------

def theme() -> dict:
    return load_config()["theme"]


def calendar_cfg() -> dict:
    return load_config().get("calendar", {})


def crosstrain_cfg() -> dict:
    return load_config().get("crosstrain", {})


def strength_cfg() -> dict:
    return load_config().get("strength", {})


def scenario_cfg() -> dict:
    return load_config().get("scenario", {})


def report_cfg() -> dict:
    return load_config().get("report", {})


def trends_cfg() -> dict:
    return load_config().get("trends", {}) or {}


def long_run_min_mi() -> float:
    """Shortest run that counts as a long run when no plan says otherwise."""
    return float(trends_cfg().get("long_run_min_mi", 6))


def hard_hr_floor() -> int:
    """Average HR at or above which a run reads as a hard effort."""
    return int(trends_cfg().get("hard_hr_floor", 155))


PLAN_DEFAULTS = {
    "days_per_week": 5,
    "long_run_day": "sat",
    "quality_day": "wed",
    "rest_days": [],
    "lift_days": [],
    "quality": "full",          # none | strides | tempo | full
    "max_long_run_mi": None,
    "long_run_time_cap_min": None,
    "block_weeks": None,        # None -> distance preset
    "taper_weeks": None,
    "peak_multiplier": None,
}


def plan_cfg() -> dict:
    """Plan-generation preferences (the `plan` block) over PLAN_DEFAULTS."""
    merged = dict(PLAN_DEFAULTS)
    merged.update(load_config().get("plan", {}) or {})
    return merged


# ---------------------------------------------------------------------------
# Races + active-race resolution
# ---------------------------------------------------------------------------

def races() -> list:
    return load_config()["races"]


def race_history() -> list:
    """Completed race results (optional). Strongest VDOT anchors available:
    an actual race beats any training-run inference."""
    return load_config().get("race_history", [])


def race_by_id(race_id: str):
    for r in races():
        if r["id"] == race_id:
            return dict(r)
    return None


def _state_active_race() -> str:
    """Manual override from data/plan_state.json, or 'auto' if unset/missing."""
    if not STATE_PATH.exists():
        return "auto"
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return state.get("active_race", "auto") or "auto"
    except (json.JSONDecodeError, OSError):
        return "auto"


def active_race(today: date = None) -> dict:
    """Resolve which race is active for `today`.

    Resolution order:
    1. data/plan_state.json `active_race` if set to a known race id (takes
       precedence so the marathon slide tooling keeps working).
    2. config.json `active_race` if set to a known race id.
    3. Auto: the earliest race whose date is today or later. If every race is
       in the past, the last race by date.

    The two-race "switch the day after the half" behavior falls out of the auto
    rule exactly, and it generalizes to any number of races.
    """
    if today is None:
        today = date.today()

    # Manual overrides (state file wins over config)
    for override in (_state_active_race(), load_config().get("active_race", "auto")):
        if override not in ("auto", None, ""):
            r = race_by_id(override)
            if r:
                return r

    ordered = sorted(races(), key=lambda r: r["date"])
    for r in ordered:
        if date.fromisoformat(r["date"]) >= today:
            return dict(r)
    return dict(ordered[-1])


def active_race_id(today: date = None) -> str:
    return active_race(today)["id"]


def plan_path(race: dict) -> Path:
    """Where this race's plan JSON lives.

    An explicit `plan` ending in .json is honored (relative to HOME); otherwise
    the convention is data/<race_id>.generated.json, which `coach.py plan`
    writes and .gitignore already excludes.
    """
    race = race or {}
    plan = race.get("plan", "")
    if isinstance(plan, str) and plan.endswith(".json"):
        p = Path(plan).expanduser()
        return p if p.is_absolute() else HOME / p
    rid = race.get("id") or "plan"
    return DATA_DIR / f"{rid}.generated.json"


def has_structured_plan(race: dict) -> bool:
    """True if a plan JSON exists for this race (explicit or generated)."""
    try:
        return plan_path(race).exists()
    except (TypeError, ValueError, OSError):
        return False


# ---------------------------------------------------------------------------
# Small parsing helpers shared across modules
# ---------------------------------------------------------------------------

def goal_time_to_sec(goal_time: str) -> int:
    """Parse 'H:MM:SS' or 'MM:SS' into seconds."""
    parts = [int(p) for p in str(goal_time).split(":")]
    if len(parts) == 3:
        h, m, s = parts
    elif len(parts) == 2:
        h, m, s = 0, parts[0], parts[1]
    else:
        return 0
    return h * 3600 + m * 60 + s


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    cfg = load_config()
    print(f"Athlete: {athlete_name()}  ({units()})")
    print(f"Max HR {max_hr()} | Threshold HR {threshold_hr()} | Easy cap {easy_hr_cap()}")
    print(f"Races ({len(races())}):")
    for r in races():
        print(f"  {r['id']:18} {r['date']}  {r['name']}  goal {r.get('goal_time', '?')}")
    ar = active_race()
    print(f"Active race today: {ar['name']} ({ar['id']}) on {ar['date']}")
