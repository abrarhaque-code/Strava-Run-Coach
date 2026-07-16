#!/usr/bin/env python3
"""First-run setup wizard for strava-run-coach.

Walks you through creating config.json from config.example.json, fills in the
high-value athlete fields and your primary race, then offers to generate sample
data so you can see output immediately. Stdlib only, plain input() prompts.

Run it:
    python3 setup.py
    python3 setup.py --from-mcp-zones zones.json   # non-interactive: calibrate
        # HR caps + threshold/tempo bands from a saved Strava-MCP
        # get_athlete_zones payload
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


# ---------------------------------------------------------------------------
# Strava-MCP zones -> config calibration
# ---------------------------------------------------------------------------

def _pace_from_mps(speed) -> float:
    """m/s -> min/mi. Run pace zones from the MCP arrive as speed bands."""
    try:
        speed = float(speed)
    except (TypeError, ValueError):
        return None
    if speed <= 0:
        return None
    return round((1609.34 / speed) / 60.0, 2)


def zones_to_config_patch(zones: dict) -> tuple:
    """Map a Strava-MCP get_athlete_zones payload onto config fields.

    Conservative by design — only boundaries with a standard 5-zone
    interpretation are mapped:
      HR Z1 top -> recovery_hr_cap, HR Z2 top -> easy_hr_cap,
      HR Z4 floor -> threshold_hr (the classic LTHR convention),
      run Z4 midpoint -> threshold_pace, run Z3/Z4 bands -> tempo/threshold
      pace zones (floor = slower boundary) with HR ranges attached.
    max_hr is NOT derived: zone formulas differ by source, so a wrong
    inference would poison every HR-based number. Returns (patch, notes).
    """
    patch = {}
    notes = []

    hr = zones.get("heart_rate_zones") or []
    if len(hr) >= 5:
        ath = patch.setdefault("athlete", {})
        if hr[0].get("max"):
            ath["recovery_hr_cap"] = int(hr[0]["max"])
            notes.append(f"recovery_hr_cap = {ath['recovery_hr_cap']} (HR Z1 top)")
        if hr[1].get("max"):
            ath["easy_hr_cap"] = int(hr[1]["max"])
            notes.append(f"easy_hr_cap = {ath['easy_hr_cap']} (HR Z2 top)")
        if hr[3].get("min"):
            ath["threshold_hr"] = int(hr[3]["min"])
            notes.append(f"threshold_hr = {ath['threshold_hr']} (HR Z4 floor)")

    run = zones.get("run_zones") or []
    if len(run) >= 5:
        z3, z4 = run[2], run[3]
        tempo_floor = _pace_from_mps(z3.get("min"))
        tempo_ceil = _pace_from_mps(z3.get("max"))
        thr_floor = _pace_from_mps(z4.get("min"))
        thr_ceil = _pace_from_mps(z4.get("max"))
        if z4.get("min") and z4.get("max"):
            mid = (float(z4["min"]) + float(z4["max"])) / 2.0
            patch.setdefault("athlete", {})["threshold_pace_min_per_mi"] = \
                _pace_from_mps(mid)
            notes.append(
                f"threshold_pace_min_per_mi = "
                f"{patch['athlete']['threshold_pace_min_per_mi']} (run Z4 midpoint)")
        pz = patch.setdefault("pace_zones", {})
        if tempo_floor and tempo_ceil:
            pz["tempo"] = {"floor": tempo_floor, "ceiling": tempo_ceil}
            if len(hr) >= 3 and hr[2].get("min") and hr[2].get("max"):
                pz["tempo"]["hr_range"] = [int(hr[2]["min"]), int(hr[2]["max"])]
            notes.append(f"pace_zones.tempo = {tempo_floor}-{tempo_ceil} (run Z3)")
        if thr_floor and thr_ceil:
            pz["threshold"] = {"floor": thr_floor, "ceiling": thr_ceil}
            if len(hr) >= 4 and hr[3].get("min") and hr[3].get("max"):
                pz["threshold"]["hr_range"] = [int(hr[3]["min"]), int(hr[3]["max"])]
            notes.append(f"pace_zones.threshold = {thr_floor}-{thr_ceil} (run Z4)")

    src_bits = []
    for k in ("heart_rate_zone_source", "run_zone_source"):
        if zones.get(k):
            src_bits.append(f"{k}={zones[k]}")
    if src_bits:
        notes.append("sources: " + ", ".join(src_bits) +
                     " (formulaic sources are a starting point, not a calibration)")
    return patch, notes


def _deep_merge(dst: dict, patch: dict) -> None:
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_merge(dst[k], v)
        else:
            dst[k] = v


def apply_mcp_zones(path: str) -> int:
    """Non-interactive: patch config.json from a saved get_athlete_zones JSON."""
    zones = json.loads(Path(path).read_text(encoding="utf-8"))
    patch, notes = zones_to_config_patch(zones)
    if not patch:
        print("No mappable zone fields found in that payload.")
        return 1
    base = CONFIG_PATH if CONFIG_PATH.exists() else EXAMPLE_PATH
    cfg = json.loads(base.read_text(encoding="utf-8"))
    _deep_merge(cfg, patch)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {CONFIG_PATH.name} with zone-derived fields:")
    for n in notes:
        print(f"  {n}")
    print("Everything else keeps its previous value. Edit config.json to adjust.")
    return 0


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

    args = sys.argv[1:]
    if "--from-mcp-zones" in args:
        idx = args.index("--from-mcp-zones")
        if idx + 1 >= len(args):
            print("Usage: python3 setup.py --from-mcp-zones <zones.json>")
            sys.exit(1)
        sys.exit(apply_mcp_zones(args[idx + 1]))

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
