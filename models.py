"""Data models for the training planning system."""

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

import config


@dataclass
class RunActivity:
    """A single run from Strava."""
    date: datetime
    name: str
    distance_mi: float
    moving_time_min: float
    pace_min_per_mi: float
    max_hr: Optional[float] = None
    avg_hr: Optional[float] = None
    cadence: Optional[float] = None          # single-leg spm
    elevation_gain_m: float = 0.0
    relative_effort: Optional[float] = None
    training_load: Optional[float] = None
    elapsed_time_min: float = 0.0

    @property
    def hr_zone(self) -> str:
        """Classify by avg HR using the configured zone boundaries."""
        if not self.avg_hr:
            return "unknown"
        if self.avg_hr < config.recovery_hr_cap():
            return "recovery"
        if self.avg_hr < config.easy_hr_cap():
            return "easy"
        if self.avg_hr < config.tempo_hr_upper():
            return "tempo"
        if self.avg_hr < config.threshold_hr_upper():
            return "threshold"
        return "vo2max"

    @property
    def is_treadmill(self) -> bool:
        return self.elevation_gain_m == 0 and self.distance_mi >= 1.0

    @property
    def cardiac_drift(self) -> Optional[float]:
        if self.max_hr and self.avg_hr:
            return self.max_hr - self.avg_hr
        return None

    def pace_str(self) -> str:
        if self.pace_min_per_mi <= 0:
            return "N/A"
        m = int(self.pace_min_per_mi)
        s = int((self.pace_min_per_mi - m) * 60)
        return f"{m}:{s:02d}"


@dataclass
class StrengthSession:
    """A weight training session from Strava (logged via Hevy)."""
    date: datetime
    name: str
    elapsed_min: float
    description: str = ""


@dataclass
class WeekSummary:
    """Aggregated stats for one training week."""
    week_start: date
    total_miles: float = 0.0
    run_count: int = 0
    longest_run_mi: float = 0.0
    avg_pace: float = 0.0
    total_relative_effort: float = 0.0
    strength_sessions: int = 0
    easy_run_count: int = 0      # HR < 148
    hard_run_count: int = 0      # HR >= 148
    is_travel_week: bool = False
    target_miles: float = 0.0
    runs: list = field(default_factory=list)

    @property
    def compliance(self) -> float:
        """Actual / target ratio. 1.0 = hit the target."""
        return self.total_miles / self.target_miles if self.target_miles > 0 else 0.0

    @property
    def long_run_ratio(self) -> float:
        """Longest run as % of weekly total. Ideal: 25-35%."""
        return self.longest_run_mi / self.total_miles if self.total_miles > 0 else 0.0

    @property
    def polarization_pct(self) -> float:
        """% of runs that were easy. Target: >= 75%."""
        total = self.easy_run_count + self.hard_run_count
        return self.easy_run_count / total if total > 0 else 0.0


# --- Plan structures ---

@dataclass
class PlannedWorkout:
    """A single planned workout."""
    day: date
    workout_type: str          # easy, key, long, rest, lift, shakeout
    description: str           # "3mi easy @ 10:00-10:15 HR<148"
    distance_mi: float = 0.0
    target_pace: str = ""      # "10:00-10:15"
    hr_cap: Optional[int] = None
    is_outdoor: bool = True
    notes: str = ""


@dataclass
class PlannedWeek:
    """A planned training week."""
    week_num: int
    week_start: date
    phase: str                 # build, travel, sharpen, taper
    target_miles: float
    long_run_target_mi: float
    key_workout: str
    lift_sessions: int
    lift_notes: str = ""
    workouts: list = field(default_factory=list)  # list of PlannedWorkout
    notes: str = ""


@dataclass
class TrainingPlan:
    """The full training plan."""
    race_name: str
    race_date: date
    race_distance_mi: float
    goal_time: str             # "1:59:59"
    goal_pace: str             # "9:09"
    travel_windows: list = field(default_factory=list)  # list of (start_date, end_date)
    weeks: list = field(default_factory=list)            # list of PlannedWeek


# --- Pace zone config ---

@dataclass
class PaceZones:
    """Personalized pace zones. All paces in min/mi.

    Defaults are sensible examples. Call PaceZones.from_config() to populate
    these from config.json (athlete + pace_zones sections).
    """
    easy_floor: float = 10.5     # 10:30/mi
    easy_ceiling: float = 10.0   # 10:00/mi
    tempo_floor: float = 9.17    # 9:10/mi
    tempo_ceiling: float = 8.83  # 8:50/mi
    threshold_floor: float = 8.5 # 8:30/mi
    threshold_ceiling: float = 8.17  # 8:10/mi
    race_pace_floor: float = 9.17    # 9:10/mi
    race_pace_ceiling: float = 9.0   # 9:00/mi
    long_run_pace: float = 10.25     # 10:15/mi

    easy_hr_cap: int = 148
    tempo_hr_range: tuple = (150, 160)
    threshold_hr_range: tuple = (160, 170)
    race_hr_cap: int = 162
    long_run_hr_cap: int = 145

    max_hr: int = 189

    @classmethod
    def from_config(cls) -> "PaceZones":
        """Build pace zones from config.json. Falls back to defaults per field."""
        ath = config.athlete()
        pz = config.pace_zones()
        easy = pz.get("easy", {})
        tempo = pz.get("tempo", {})
        thr = pz.get("threshold", {})
        race = pz.get("race_pace", {})
        long = pz.get("long_run", {})
        d = cls()  # defaults as fallback
        return cls(
            easy_floor=easy.get("floor", d.easy_floor),
            easy_ceiling=easy.get("ceiling", d.easy_ceiling),
            tempo_floor=tempo.get("floor", d.tempo_floor),
            tempo_ceiling=tempo.get("ceiling", d.tempo_ceiling),
            threshold_floor=thr.get("floor", d.threshold_floor),
            threshold_ceiling=thr.get("ceiling", d.threshold_ceiling),
            race_pace_floor=race.get("floor", d.race_pace_floor),
            race_pace_ceiling=race.get("ceiling", d.race_pace_ceiling),
            long_run_pace=long.get("pace", d.long_run_pace),
            easy_hr_cap=int(ath.get("easy_hr_cap", d.easy_hr_cap)),
            tempo_hr_range=tuple(tempo.get("hr_range", d.tempo_hr_range)),
            threshold_hr_range=tuple(thr.get("hr_range", d.threshold_hr_range)),
            race_hr_cap=int(race.get("hr_cap", d.race_hr_cap)),
            long_run_hr_cap=int(ath.get("long_run_hr_cap", d.long_run_hr_cap)),
            max_hr=int(ath.get("max_hr", d.max_hr)),
        )
