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
import sys
from datetime import date
from functools import lru_cache
from pathlib import Path

_HERE = Path(__file__).parent
CONFIG_PATH = _HERE / "config.json"
EXAMPLE_PATH = _HERE / "config.example.json"
STATE_PATH = _HERE / "data" / "plan_state.json"

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
            "Run `python3 setup.py` to create your own.",
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


# ---------------------------------------------------------------------------
# Races + active-race resolution
# ---------------------------------------------------------------------------

def races() -> list:
    return load_config()["races"]


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


def has_structured_plan(race: dict) -> bool:
    """True if the race points at a JSON plan file (vs a generated short plan)."""
    plan = (race or {}).get("plan", "")
    return isinstance(plan, str) and plan.endswith(".json")


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
