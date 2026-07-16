"""Parse Strava CSV and compute training metrics."""

import csv
import io
import statistics
from datetime import datetime, timedelta, date
from collections import defaultdict
from typing import List, Tuple

import config
from enrichment import classify_activity
from models import RunActivity, StrengthSession, WeekSummary, PaceZones


def load_activities(csv_path: str = 'activities.csv') -> Tuple[List[RunActivity], List[StrengthSession]]:
    """Parse activities.csv into RunActivity and StrengthSession lists."""
    with open(csv_path, 'r', encoding='utf-8') as f:
        content = f.read()

    reader = csv.reader(io.StringIO(content))
    header = next(reader)

    # Duplicate column names exist. Use explicit indices:
    # 1=Date, 2=Name, 3=Type, 4=Description, 6=Distance(km), 7=MaxHR, 8=RelEffort
    # 15=ElapsedTime(s), 16=MovingTime(s), 20=ElevGain, 29=AvgCadence, 31=AvgHR

    runs = []
    strength = []

    for row in reader:
        if len(row) < 20:
            continue

        activity_type = row[3]
        try:
            dt = datetime.strptime(row[1].strip(), '%b %d, %Y, %I:%M:%S %p')
        except (ValueError, IndexError):
            continue

        if activity_type == 'Run':
            try:
                dist_km = float(row[6]) if row[6] else 0
            except ValueError:
                continue
            if dist_km == 0:
                continue

            dist_mi = dist_km * 0.621371
            mt = float(row[16]) if len(row) > 16 and row[16] else 0
            et = float(row[15]) if len(row) > 15 and row[15] else 0
            pace = (mt / 60) / dist_mi if dist_mi > 0 and mt > 0 else 0

            def safe_float(idx):
                try:
                    return float(row[idx]) if len(row) > idx and row[idx] else None
                except (ValueError, IndexError):
                    return None

            # Skip bike-as-run and zero-time entries: the weekly aggregates
            # here feed mileage and pace stats, which fake runs would skew.
            probe = {
                "type": "Run",
                "distance": dist_km * 1000,
                "moving_time": mt,
                "max_speed": safe_float(18),
                "average_speed": safe_float(19),
            }
            if classify_activity(probe) not in ("run", "treadmill_run"):
                continue

            runs.append(RunActivity(
                date=dt,
                name=row[2],
                distance_mi=dist_mi,
                moving_time_min=mt / 60,
                pace_min_per_mi=pace,
                max_hr=safe_float(7),
                avg_hr=safe_float(31),
                cadence=safe_float(29),
                elevation_gain_m=safe_float(20) or 0.0,
                relative_effort=safe_float(8),
                training_load=safe_float(76),
                elapsed_time_min=et / 60,
            ))

        elif activity_type == 'Weight Training':
            elapsed = float(row[5]) if row[5] else 0
            desc = row[4][:500] if len(row) > 4 else ""
            strength.append(StrengthSession(
                date=dt,
                name=row[2],
                elapsed_min=elapsed / 60,
                description=desc,
            ))

    runs.sort(key=lambda x: x.date, reverse=True)
    strength.sort(key=lambda x: x.date, reverse=True)
    return runs, strength


def weekly_summaries(runs: List[RunActivity], strength: List[StrengthSession],
                     weeks_back: int = 12) -> List[WeekSummary]:
    """Compute weekly aggregates for the last N weeks."""
    now = date.today()
    cap = config.easy_hr_cap()
    summaries = []

    for wb in range(weeks_back):
        # ISO week: Monday to Sunday
        week_start = now - timedelta(days=now.weekday()) - timedelta(weeks=wb)
        week_end = week_start + timedelta(days=7)

        week_runs = [r for r in runs
                     if week_start <= r.date.date() < week_end]
        week_strength = [s for s in strength
                         if week_start <= s.date.date() < week_end]

        if not week_runs:
            summaries.append(WeekSummary(
                week_start=week_start,
                strength_sessions=len(week_strength),
                runs=[],
            ))
            continue

        total_mi = sum(r.distance_mi for r in week_runs)
        longest = max(r.distance_mi for r in week_runs)
        paces = [r.pace_min_per_mi for r in week_runs if r.pace_min_per_mi > 0]
        avg_pace = statistics.mean(paces) if paces else 0

        easy = sum(1 for r in week_runs if r.avg_hr and r.avg_hr < cap)
        hard = sum(1 for r in week_runs if r.avg_hr and r.avg_hr >= cap)
        total_re = sum(r.relative_effort or 0 for r in week_runs)

        summaries.append(WeekSummary(
            week_start=week_start,
            total_miles=total_mi,
            run_count=len(week_runs),
            longest_run_mi=longest,
            avg_pace=avg_pace,
            total_relative_effort=total_re,
            strength_sessions=len(week_strength),
            easy_run_count=easy,
            hard_run_count=hard,
            runs=week_runs,
        ))

    return summaries


def compute_actr(summaries: List[WeekSummary]) -> float:
    """Acute:Chronic Training Ratio. Recent 1 week / avg of last 4 weeks."""
    if len(summaries) < 4:
        return 0.0
    acute = summaries[0].total_miles
    chronic = statistics.mean(s.total_miles for s in summaries[:4])
    return acute / chronic if chronic > 0 else 0.0


def pace_hr_table(runs: List[RunActivity]) -> dict:
    """Bucket runs by pace and compute avg HR per bucket."""
    hr_runs = [r for r in runs if r.avg_hr and 7 < r.pace_min_per_mi < 12 and r.distance_mi > 1.5]
    buckets = defaultdict(list)
    for r in hr_runs:
        bucket = round(r.pace_min_per_mi * 2) / 2
        buckets[bucket].append(r)

    result = {}
    for bucket in sorted(buckets.keys()):
        group = buckets[bucket]
        if len(group) >= 2:
            result[bucket] = {
                'avg_pace': statistics.mean(r.pace_min_per_mi for r in group),
                'avg_hr': statistics.mean(r.avg_hr for r in group),
                'count': len(group),
            }
    return result


def polarization_stats(runs: List[RunActivity], months_back: int = 6) -> dict:
    """Compute training polarization for recent runs."""
    cutoff = datetime.now() - timedelta(days=months_back * 30)
    cap = config.easy_hr_cap()
    recent = [r for r in runs if r.avg_hr and r.date >= cutoff]
    easy = [r for r in recent if r.avg_hr < cap]
    hard = [r for r in recent if r.avg_hr >= cap]
    total = len(recent)
    return {
        'easy_count': len(easy),
        'hard_count': len(hard),
        'total': total,
        'easy_pct': len(easy) / total * 100 if total > 0 else 0,
        'hard_pct': len(hard) / total * 100 if total > 0 else 0,
        'easy_miles': sum(r.distance_mi for r in easy),
        'hard_miles': sum(r.distance_mi for r in hard),
    }


def race_pace_readiness(runs: List[RunActivity]) -> dict:
    """Check readiness for target race pace (9:00-9:10/mi)."""
    at_pace_5plus = [r for r in runs
                     if 8.9 <= r.pace_min_per_mi <= 9.3
                     and r.distance_mi >= 5 and r.avg_hr]
    sub_910_8plus = [r for r in runs
                     if r.pace_min_per_mi <= 9.17
                     and r.distance_mi >= 8 and r.avg_hr]
    return {
        'at_race_pace_5mi': at_pace_5plus,
        'sub_910_8mi': sub_910_8plus,
        'has_10mi_at_pace': any(r.distance_mi >= 10 and r.pace_min_per_mi <= 9.17
                                for r in runs if r.avg_hr),
    }


def monthly_mileage(runs: List[RunActivity], months_back: int = 12) -> dict:
    """Monthly mileage totals."""
    cutoff = datetime.now() - timedelta(days=months_back * 30)
    months = defaultdict(lambda: {'miles': 0, 'count': 0, 'longest': 0})
    for r in runs:
        if r.date >= cutoff:
            key = r.date.strftime('%Y-%m')
            months[key]['miles'] += r.distance_mi
            months[key]['count'] += 1
            months[key]['longest'] = max(months[key]['longest'], r.distance_mi)
    return dict(months)


def fmt_pace(p: float) -> str:
    if p <= 0:
        return "N/A"
    m = int(p)
    s = int((p - m) * 60)
    return f"{m}:{s:02d}"


# --- CLI output ---

def print_summary(runs, strength):
    """Print a full training summary to stdout."""
    import sys
    sys.stdout.reconfigure(encoding='utf-8')

    zones = PaceZones.from_config()
    summaries = weekly_summaries(runs, strength, weeks_back=12)
    actr = compute_actr(summaries)
    polar = polarization_stats(runs)
    readiness = race_pace_readiness(runs)

    print(f"Loaded {len(runs)} runs, {len(strength)} strength sessions\n")

    print("=" * 60)
    print("WEEKLY MILEAGE (last 12 weeks)")
    print("=" * 60)
    print(f"{'Week':>10} | {'Runs':>4} | {'Miles':>6} | {'Longest':>7} | "
          f"{'Pace':>7} | {'Easy':>4} | {'Hard':>4} | {'Lift':>4}")
    for s in summaries:
        print(f"{s.week_start.strftime('%b %d'):>10} | {s.run_count:4d} | "
              f"{s.total_miles:6.1f} | {s.longest_run_mi:7.1f} | "
              f"{fmt_pace(s.avg_pace):>7} | {s.easy_run_count:4d} | "
              f"{s.hard_run_count:4d} | {s.strength_sessions:4d}")

    print(f"\nACTR: {actr:.2f} (target 0.8-1.3)")

    print(f"\n{'=' * 60}")
    print("TRAINING POLARIZATION (last 6 months)")
    print("=" * 60)
    print(f"Easy (<148 bpm): {polar['easy_count']}/{polar['total']} = "
          f"{polar['easy_pct']:.0f}% ({polar['easy_miles']:.1f} mi)")
    print(f"Hard (>=148 bpm): {polar['hard_count']}/{polar['total']} = "
          f"{polar['hard_pct']:.0f}% ({polar['hard_miles']:.1f} mi)")
    print(f"Target: >= 75% easy")

    print(f"\n{'=' * 60}")
    print("PACE vs HR")
    print("=" * 60)
    ph = pace_hr_table(runs)
    for bucket, data in ph.items():
        pct = data['avg_hr'] / zones.max_hr * 100
        print(f"  {fmt_pace(data['avg_pace'])}/mi -> avg HR {data['avg_hr']:.0f} "
              f"({pct:.0f}% max) [{data['count']} runs]")

    print(f"\n{'=' * 60}")
    print("RACE PACE READINESS (9:00-9:10/mi)")
    print("=" * 60)
    print(f"Runs at race pace for 5+ mi: {len(readiness['at_race_pace_5mi'])}")
    print(f"Runs sub-9:10 for 8+ mi: {len(readiness['sub_910_8mi'])}")
    print(f"Has 10mi+ at race pace: {'YES' if readiness['has_10mi_at_pace'] else 'NO'}")

    for r in readiness['sub_910_8mi'][:5]:
        print(f"  {r.date.strftime('%b %d %Y')} | {r.distance_mi:.1f}mi | "
              f"{r.pace_str()}/mi | avg HR {r.avg_hr:.0f}")

    print(f"\n{'=' * 60}")
    print("MONTHLY MILEAGE (last 12 months)")
    print("=" * 60)
    mm = monthly_mileage(runs)
    for key in sorted(mm.keys()):
        m = mm[key]
        print(f"  {key}: {m['count']:2d} runs, {m['miles']:.1f} mi, "
              f"longest {m['longest']:.1f} mi")


if __name__ == '__main__':
    runs, strength = load_activities()
    print_summary(runs, strength)
