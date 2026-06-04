#!/usr/bin/env python3
"""First-run setup wizard for strava-run-coach.

Walks you through creating config.json from config.example.json, fills in the
high-value athlete fields and your primary race, then offers to generate sample
data so you can see output immediately. Stdlib only, plain input() prompts.

Run it:
    python3 setup.py
"""

import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
EXAMPLE_PATH = _HERE / "config.example.json"
CONFIG_PATH = _HERE / "config.json"


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        raw = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        raw = ""
    return raw or default


def _ask_int(prompt: str, default: int) -> int:
    while True:
        raw = _ask(prompt, str(default))
        try:
            return int(raw)
        except ValueError:
            print("  Please enter a whole number.")


def _ask_float(prompt: str, default: float) -> float:
    while True:
        raw = _ask(prompt, str(default))
        try:
            return float(raw)
        except ValueError:
            print("  Please enter a number.")


def _pace_to_minutes(text: str, fallback: float) -> float:
    """Parse 'mm:ss' per mile into a float in minutes. Falls back on bad input."""
    text = text.strip()
    if not text:
        return fallback
    if ":" in text:
        try:
            m, s = text.split(":")
            return int(m) + int(s) / 60.0
        except ValueError:
            print("  Could not parse pace, keeping default.")
            return fallback
    try:
        return float(text)
    except ValueError:
        return fallback


def _goal_time_to_sec(goal_time: str) -> int:
    parts = [int(p) for p in goal_time.split(":")]
    if len(parts) == 3:
        h, m, s = parts
    elif len(parts) == 2:
        h, m, s = 0, parts[0], parts[1]
    else:
        return 0
    return h * 3600 + m * 60 + s


def _confirm(prompt: str, default_yes: bool = True) -> bool:
    d = "Y/n" if default_yes else "y/N"
    raw = _ask(f"{prompt} ({d})").lower()
    if not raw:
        return default_yes
    return raw.startswith("y")


def run_wizard() -> dict:
    cfg = json.loads(EXAMPLE_PATH.read_text(encoding="utf-8"))

    print()
    print("strava-run-coach setup")
    print("----------------------")
    print("A few questions to personalize config.json. Press Enter to accept a")
    print("default. You can edit config.json by hand anytime.")
    print()

    # --- Athlete ---
    ath = cfg["athlete"]
    print("Athlete")
    ath["name"] = _ask("  Your name", ath.get("name", "Athlete"))
    units = _ask("  Units (mi/km)", ath.get("units", "mi")).lower()
    ath["units"] = "km" if units == "km" else "mi"
    ath["max_hr"] = _ask_int("  Max heart rate (bpm)", ath.get("max_hr", 189))
    ath["easy_hr_cap"] = _ask_int(
        "  Easy-run HR cap (stay under this on easy days)",
        ath.get("easy_hr_cap", 148),
    )
    ath["threshold_hr"] = _ask_int(
        "  Lactate-threshold HR (bpm)", ath.get("threshold_hr", 165)
    )
    cur_pace = ath.get("threshold_pace_min_per_mi", 9.0333)
    pace_txt = _ask("  Threshold pace per mile (mm:ss)",
                    _fmt_pace(cur_pace))
    ath["threshold_pace_min_per_mi"] = round(
        _pace_to_minutes(pace_txt, cur_pace), 4
    )

    # --- Primary race ---
    print()
    print("Primary race")
    race = cfg["races"][0]
    race["name"] = _ask("  Race name", race.get("name", "Goal Race"))
    race["date"] = _ask("  Race date (YYYY-MM-DD)", race.get("date", ""))
    race["distance_mi"] = _ask_float(
        "  Distance in miles (13.1 half, 26.2 full)",
        race.get("distance_mi", 13.1),
    )
    goal_time = _ask("  Goal finish time (H:MM:SS)",
                     race.get("goal_time", "1:59:59"))
    race["goal_time"] = goal_time
    goal_sec = _goal_time_to_sec(goal_time)
    if goal_sec > 0 and race["distance_mi"] > 0:
        race["goal_pace_min_per_mi"] = round(
            goal_sec / 60.0 / race["distance_mi"], 3
        )

    return cfg


def _fmt_pace(minutes: float) -> str:
    m = int(minutes)
    s = int(round((minutes - m) * 60))
    if s == 60:
        m += 1
        s = 0
    return f"{m}:{s:02d}"


def _maybe_generate_sample_data() -> None:
    print()
    if not _confirm("Generate sample training data now so you can see output?"):
        print("  Skipped. Run `python3 scripts/generate_sample_data.py` later.")
        return
    print()
    try:
        sys.path.insert(0, str(_HERE))
        from scripts import generate_sample_data
        generate_sample_data.main()
    except Exception as e:  # keep setup resilient
        print(f"  Could not generate sample data: {e}")
        print("  You can run `python3 scripts/generate_sample_data.py` manually.")


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    if not EXAMPLE_PATH.exists():
        print(f"Missing {EXAMPLE_PATH.name}. Cannot run setup.")
        sys.exit(1)

    if CONFIG_PATH.exists():
        print(f"{CONFIG_PATH.name} already exists.")
        if not _confirm("Overwrite it?", default_yes=False):
            print("Leaving your existing config untouched.")
            sys.exit(0)

    cfg = run_wizard()
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    print()
    print(f"Wrote {CONFIG_PATH.name}.")

    _maybe_generate_sample_data()

    print()
    print("Connect Strava (optional, your data stays local):")
    print("  1. Create an API app at https://www.strava.com/settings/api")
    print("  2. cp .env.example .env")
    print("  3. Fill STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET in .env")
    print("  4. python3 strava_authorize.py   (one-time OAuth)")
    print("  5. python3 strava_sync.py        (pull your activities)")
    print()
    print("Run `python3 coach.py` to see your dashboard and brief.")
    print()


if __name__ == "__main__":
    main()
