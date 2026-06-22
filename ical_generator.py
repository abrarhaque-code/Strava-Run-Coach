"""Generate .ics (iCalendar) files for importing training workouts into Outlook.

Produces RFC 5545 compliant iCalendar files with a VTIMEZONE block. Each
PlannedWorkout becomes a VEVENT with appropriate timing, duration, and a
VALARM reminder.

Timing, timezone, and reminder lead time come from the `calendar` section of
config.json (weekday_run_time, saturday_long_time, lift_time, timezone,
reminder_minutes). Rest days are skipped (no event).
"""

import os
import re
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import config
from models import PlannedWorkout, PlannedWeek, TrainingPlan


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PRODID = "-//Training Planner//EN"

# VTIMEZONE block for America/New_York (EST/EDT).  Covers the standard US
# transition rules so Outlook and Google Calendar interpret times correctly.
# Kept as an acceptable default block; the configured TZID is used in
# DTSTART/DTEND so events resolve against the right zone.
VTIMEZONE_BLOCK = """\
BEGIN:VTIMEZONE
TZID:America/New_York
BEGIN:DAYLIGHT
TZOFFSETFROM:-0500
TZOFFSETTO:-0400
TZNAME:EDT
DTSTART:19700308T020000
RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=2SU
END:DAYLIGHT
BEGIN:STANDARD
TZOFFSETFROM:-0400
TZOFFSETTO:-0500
TZNAME:EST
DTSTART:19701101T020000
RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=1SU
END:STANDARD
END:VTIMEZONE"""

LIFT_DURATION_MIN = 45


def _tz() -> str:
    """Configured calendar TZID (defaults to America/New_York)."""
    return config.calendar_cfg().get("timezone", "America/New_York")


def _parse_hhmm(value: str, default: tuple) -> tuple:
    """Parse 'HH:MM' into (hour, minute), falling back to `default`."""
    try:
        h, m = str(value).split(":")
        return (int(h), int(m))
    except (ValueError, AttributeError):
        return default


def _start_times() -> dict:
    """Default start times by workout category (hour, minute) from config."""
    cal = config.calendar_cfg()
    return {
        "weekday_run": _parse_hhmm(cal.get("weekday_run_time"), (20, 30)),
        "saturday_long": _parse_hhmm(cal.get("saturday_long_time"), (9, 0)),
        "lift": _parse_hhmm(cal.get("lift_time"), (20, 30)),
    }


def _reminder_minutes() -> int:
    """VALARM lead time in minutes (defaults to 30)."""
    return int(config.calendar_cfg().get("reminder_minutes", 30))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fold_line(line: str) -> str:
    """RFC 5545 content-line folding: max 75 octets per line."""
    encoded = line.encode("utf-8")
    if len(encoded) <= 75:
        return line
    parts = []
    while len(encoded) > 75:
        # First chunk is 75, subsequent chunks are 74 (leading space counts).
        cut = 75 if not parts else 74
        parts.append(encoded[:cut].decode("utf-8", errors="ignore"))
        encoded = encoded[cut:]
    if encoded:
        parts.append(encoded.decode("utf-8", errors="ignore"))
    return "\r\n ".join(parts)


def _ics_escape(text: str) -> str:
    """Escape special characters for iCalendar text values."""
    text = text.replace("\\", "\\\\")
    text = text.replace(";", "\\;")
    text = text.replace(",", "\\,")
    # Newlines -> literal \n in the ics value.
    text = text.replace("\n", "\\n")
    return text


def _format_dt(d: date, hour: int, minute: int) -> str:
    """Return an iCalendar datetime string like 20260415T213000."""
    return f"{d.year:04d}{d.month:02d}{d.day:02d}T{hour:02d}{minute:02d}00"


def _parse_pace_to_min(pace_str: str) -> Optional[float]:
    """Parse a pace string like '10:15' or '9:00-9:05' into minutes per mile.

    For ranges, returns the average. Returns None if unparseable.
    """
    if not pace_str:
        return None
    # Handle ranges like "9:00-9:05"
    parts = pace_str.split("-")
    values = []
    for p in parts:
        p = p.strip()
        m = re.match(r"(\d+):(\d{2})", p)
        if m:
            values.append(int(m.group(1)) + int(m.group(2)) / 60.0)
    if not values:
        return None
    return sum(values) / len(values)


def _estimate_duration_min(workout: PlannedWorkout) -> int:
    """Estimate workout duration in minutes from distance and pace.

    Falls back to 10 min/mi if pace is missing, and adds a small buffer for
    warmup/cooldown transitions.
    """
    if workout.workout_type == "lift":
        return LIFT_DURATION_MIN
    if workout.distance_mi <= 0:
        return 30  # minimal default for rest / unknown

    pace = _parse_pace_to_min(workout.target_pace)
    if pace is None:
        pace = 10.0  # sensible default

    raw = workout.distance_mi * pace
    # Round up to nearest 5 minutes and add a small buffer.
    duration = int(raw) + 5
    duration = max(duration, 15)
    return duration


def _start_time_for(workout: PlannedWorkout) -> tuple[int, int]:
    """Return (hour, minute) start time in the configured TZ for the workout."""
    start_times = _start_times()
    if workout.workout_type == "lift":
        return start_times["lift"]

    # Saturday long runs
    if workout.day.weekday() == 5 and workout.workout_type == "long":
        return start_times["saturday_long"]

    # All other runs default to the weekday-run time slot.
    return start_times["weekday_run"]


def _location(workout: PlannedWorkout) -> str:
    if workout.workout_type == "lift":
        return "Gym"
    if not workout.is_outdoor:
        return "Gym"
    return "Outdoor"


def _build_description(workout: PlannedWorkout) -> str:
    """Build a rich text description for the event body."""
    lines = []
    lines.append(workout.description)
    if workout.target_pace:
        lines.append(f"Target pace: {workout.target_pace}")
    if workout.hr_cap:
        lines.append(f"HR cap: {workout.hr_cap}")
    if workout.notes:
        lines.append(f"Notes: {workout.notes}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# UID generation (deterministic, no uuid library needed)
# ---------------------------------------------------------------------------

def _uid(workout: PlannedWorkout) -> str:
    """Generate a deterministic UID for a workout event."""
    day_str = workout.day.isoformat()
    return f"{day_str}-{workout.workout_type}@training-planner"


# ---------------------------------------------------------------------------
# VEVENT generation
# ---------------------------------------------------------------------------

def _build_vevent(workout: PlannedWorkout) -> str:
    """Return a VEVENT block (without enclosing VCALENDAR)."""
    if workout.workout_type == "rest":
        return ""  # skip rest days

    hour, minute = _start_time_for(workout)
    duration = _estimate_duration_min(workout)

    end_hour = hour
    end_minute = minute + duration
    # Roll over hours.
    end_hour += end_minute // 60
    end_minute = end_minute % 60
    # Handle day rollover (e.g. 9:30 PM + 90 min).
    end_day = workout.day
    if end_hour >= 24:
        end_hour -= 24
        end_day = workout.day + timedelta(days=1)

    dt_start = _format_dt(workout.day, hour, minute)
    dt_end = _format_dt(end_day, end_hour, end_minute)

    summary = _ics_escape(workout.description)
    description = _ics_escape(_build_description(workout))
    location = _location(workout)
    uid = _uid(workout)

    dtstamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    tzid = _tz()
    reminder = _reminder_minutes()

    lines = [
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{dtstamp}",
        "SEQUENCE:0",
        f"DTSTART;TZID={tzid}:{dt_start}",
        f"DTEND;TZID={tzid}:{dt_end}",
        f"SUMMARY:{summary}",
        f"DESCRIPTION:{description}",
        f"LOCATION:{location}",
        "CLASS:PRIVATE",
        "BEGIN:VALARM",
        f"TRIGGER:-PT{reminder}M",
        "ACTION:DISPLAY",
        "DESCRIPTION:Training time",
        "END:VALARM",
        "END:VEVENT",
    ]

    # Fold long lines per RFC 5545.
    return "\r\n".join(_fold_line(l) for l in lines)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_week_ics(week: PlannedWeek) -> str:
    """Generate a complete .ics file string for a single training week.

    Parameters
    ----------
    week : PlannedWeek
        A planned training week containing a list of PlannedWorkout objects.

    Returns
    -------
    str
        The full iCalendar file content, ready to write to disk or import.
    """
    events = []
    for w in week.workouts:
        block = _build_vevent(w)
        if block:
            events.append(block)

    header = "\r\n".join([
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:Training Week {week.week_num} - {week.phase.title()}",
    ])

    # Replace \n in the VTIMEZONE_BLOCK with \r\n for consistency.
    tz_block = VTIMEZONE_BLOCK.replace("\n", "\r\n")

    body = "\r\n".join(events)
    footer = "END:VCALENDAR"

    parts = [header, tz_block]
    if body:
        parts.append(body)
    parts.append(footer)
    return "\r\n".join(parts) + "\r\n"


def write_week_ics(week: PlannedWeek, output_dir: str = "plan_output") -> str:
    """Write a .ics file for a single training week.

    Parameters
    ----------
    week : PlannedWeek
        The planned week to export.
    output_dir : str
        Directory to write the file into. Created if it doesn't exist.

    Returns
    -------
    str
        The path to the written .ics file.
    """
    os.makedirs(output_dir, exist_ok=True)
    filename = f"week{week.week_num:02d}_{week.week_start.isoformat()}.ics"
    path = os.path.join(output_dir, filename)
    content = generate_week_ics(week)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(content)
    print(f"  Wrote {path}")
    return path


def generate_plan_ics(plan: TrainingPlan) -> str:
    """Generate ONE .ics for the whole plan (a subscribable feed for Outlook).

    Every workout across every week becomes a VEVENT in a single VCALENDAR, so
    the user adds one calendar and re-generation updates it (adaptive feed).
    """
    events = []
    for week in plan.weeks:
        for w in week.workouts:
            block = _build_vevent(w)
            if block:
                events.append(block)

    header = "\r\n".join([
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{plan.race_name} Training",
    ])
    tz_block = VTIMEZONE_BLOCK.replace("\n", "\r\n")
    parts = [header, tz_block]
    if events:
        parts.append("\r\n".join(events))
    parts.append("END:VCALENDAR")
    return "\r\n".join(parts) + "\r\n"


def write_plan_ics(plan: TrainingPlan, output_dir: str = "plan_output",
                   filename: str = "training.ics") -> str:
    """Write the combined plan feed to a single .ics file. Returns the path."""
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, filename)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(generate_plan_ics(plan))
    print(f"  Wrote subscribable feed: {path}")
    return path


def generate_all_ics(plan: TrainingPlan, output_dir: str = "plan_output") -> list[str]:
    """Write .ics files for every week in a training plan.

    Parameters
    ----------
    plan : TrainingPlan
        The full training plan.
    output_dir : str
        Directory to write files into.

    Returns
    -------
    list[str]
        List of file paths written.
    """
    print(f"Generating calendar files for: {plan.race_name}")
    print(f"  Race date: {plan.race_date}  |  Goal: {plan.goal_time}")
    print(f"  Weeks: {len(plan.weeks)}")
    print()

    paths = []
    for week in plan.weeks:
        p = write_week_ics(week, output_dir=output_dir)
        paths.append(p)

    print(f"\nDone. {len(paths)} .ics files written to {output_dir}/")
    return paths


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from planner import generate_half_plan

    plan = generate_half_plan()
    generate_all_ics(plan, output_dir="plan_output")
