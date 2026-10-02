"""End to end over a hand-made Strava MCP inbox: list pages (one persisted as a
tool result), performance and stream payloads in the MCP dialect, a profile
and zones. Then the exact command sequence the entry skill runs, each in its
own process with STRAVA_COACH_HOME set and no STRAVA_* variables."""

import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from tests.test_e2e_cli import run_coach

TODAY = date.today()
MI = 1609.34
LONG_ID = 88100011
WORKOUT_ID = 88100012


def _day(days_ago: int, hour: int = 7) -> str:
    return (TODAY - timedelta(days=days_ago)).isoformat() + f"T{hour:02d}:00:00"


def _item(aid, days_ago, miles, pace_min, name="Morning Run", sport="Run", **extra):
    mt = int(miles * pace_min * 60)
    it = {"id": str(aid), "name": name, "sport_type": sport, "start_local": _day(days_ago),
          "is_trainer": False,
          "summary": {"distance": round(miles * MI, 1), "moving_time": mt, "elapsed_time": mt + 90,
                      "elevation_gain": 40, "avg_speed": round(miles * MI / mt, 3),
                      "max_speed": round(miles * MI / mt * 1.3, 3), "relative_effort": int(miles * 8)}}
    it.update(extra)
    return it


def _perf(hr, laps):
    return {"has_heartrate": True, "average_heartrate": hr, "max_heartrate": hr + 15,
            "average_cadence": 82.0, "laps": laps}


def _mile_laps(spec):
    return [{"elapsed_time": int(p * 60) + 3, "moving_time": int(p * 60), "distance": MI,
             "avg_hr": hr, "max_hr": hr + 6} for p, hr in spec]


def _stream(n_min: int, hr: int, mps: float = 2.8):
    t = list(range(0, n_min * 60, 5))
    return {"time": t, "heart_rate": [hr] * len(t), "distance": [round(x * mps, 1) for x in t],
            "velocity_smooth": [mps] * len(t), "moving": [True] * len(t), "cadence": [82] * len(t)}


def build_inbox(inbox: Path) -> None:
    (inbox / "list").mkdir(parents=True)
    (inbox / "perf").mkdir()
    (inbox / "streams").mkdir()
    runs = [_item(88100001 + i, 80 - i * 7, 5 + (i % 3), 10.1) for i in range(10)]   # easy, every week
    runs.append(_item(LONG_ID, 5, 12.4, 10.3, name="Long Run"))
    runs.append(_item(WORKOUT_ID, 3, 5.0, 8.4, name="6 x 800"))
    others = [_item(88100020, 4, 10.0, 4.0, name="Evening Ride", sport="Ride"),
              {"id": "88100021", "name": "Lift", "sport_type": "WeightTraining", "start_local": _day(6),
               "summary": {"distance": 0, "moving_time": 2400, "elapsed_time": 2400}}]
    page1 = {"activities": runs[:6], "pageInfo": {"hasNextPage": True, "endCursor": "c1"}}
    page2 = {"activities": runs[6:] + others, "pageInfo": {"hasNextPage": False}}
    (inbox / "list" / "page-1.json").write_text(json.dumps(page1))
    # Claude Code persists large tool results as [{"type": "text", "text": "<json>"}]
    (inbox / "list" / "page-2.json").write_text(json.dumps([{"type": "text", "text": json.dumps(page2)}]))
    (inbox / "perf" / f"{LONG_ID}.json").write_text(json.dumps(
        _perf(146, _mile_laps([(10.4, 138)] * 4 + [(10.2, 146)] * 6 + [(10.1, 152)] * 2))))
    (inbox / "perf" / f"{WORKOUT_ID}.json").write_text(json.dumps(_perf(158, _mile_laps([(8.4, 158)] * 5))))
    (inbox / "perf" / "88100010.json").write_text(json.dumps(_perf(139, _mile_laps([(10.1, 139)] * 7))))
    (inbox / "streams" / f"{LONG_ID}.json").write_text(json.dumps(_stream(128, 146)))
    (inbox / "profile.json").write_text(json.dumps({
        "first_name": "Sam", "last_name": "Runner", "measurement_preference": "Metric",
        "current_focus": {"focus_type": "TrainForEvent", "open_text": "PR at the City Marathon",
                          "expires_at_local": (TODAY + timedelta(days=61)).isoformat() + "T00:00:00"}}))
    (inbox / "zones.json").write_text(json.dumps({
        "heart_rate_zones": [{"min": 0, "max": 129}, {"min": 130, "max": 160}, {"min": 161, "max": 176},
                             {"min": 177, "max": 192}, {"min": 193}],
        "heart_rate_zone_source": "MaxHeartRate",
        "run_zones": [{"min": 0, "max": 2.673}, {"min": 2.673, "max": 3.106}, {"min": 3.106, "max": 3.46},
                      {"min": 3.46, "max": 3.695}, {"min": 3.695, "max": 3.931}, {"min": 3.931}],
        "run_zone_source": "PerformancePredictions"}))


class TestMcpFlow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.home = Path(cls._tmp.name)
        cls.inbox = cls.home / "data" / "mcp"
        build_inbox(cls.inbox)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_01_dry_run_then_ingest(self):
        r = run_coach(self.home, "ingest", "--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("[mcp]", r.stdout)
        self.assertFalse(any((self.home / "data" / "strava_cache").rglob("*.json")))
        r = run_coach(self.home, "ingest")
        self.assertEqual(r.returncode, 0, r.stderr)
        cached = {p.stem for p in (self.home / "data" / "strava_cache" / "activities").glob("*.json")}
        self.assertEqual(len(cached), 14)
        self.assertTrue((self.home / "data" / "strava_cache" / "streams" / f"{LONG_ID}.json").exists())
        long_run = json.loads((self.home / "data" / "strava_cache" / "activities" / f"{LONG_ID}.json").read_text())
        self.assertEqual(long_run["average_heartrate"], 146)
        self.assertEqual(len(long_run["laps"]), 12)
        self.assertEqual(long_run["laps"][0]["average_heartrate"], 138)

    def test_02_status_before_setup(self):
        r = run_coach(self.home, "status", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        s = json.loads(r.stdout)
        self.assertFalse(s["configured"])
        self.assertEqual(s["cache_count"], 14)
        self.assertGreaterEqual(s["history_days"], 80)
        self.assertNotIn(LONG_ID, s["fetch"]["streams_needed"])      # already cached
        self.assertTrue(any("init --from-mcp" in n for n in s["next"]))

    def test_03_init_from_mcp_then_everything_runs_in_km(self):
        r = run_coach(self.home, "init", "--from-mcp", "--goal-time", "3:45:00")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        cfg = json.loads((self.home / "config.json").read_text())
        self.assertEqual((cfg["athlete"]["name"], cfg["athlete"]["units"]), ("Sam Runner", "km"))
        self.assertEqual(cfg["races"][0]["name"], "City Marathon")
        self.assertEqual(cfg["races"][0]["goal_time"], "3:45:00")
        self.assertEqual(cfg["_meta"]["needs"], [])
        self.assertTrue((self.home / "data" / "city_marathon.generated.json").exists(), r.stdout)

        r = run_coach(self.home, "status", "--json")
        s = json.loads(r.stdout)
        self.assertTrue(s["configured"] and s["plan_present"])
        self.assertEqual(s["units"], "km")

        r = run_coach(self.home, "plan", "--from-data", "--days", "4", "--quality", "strides", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        plan = json.loads(r.stdout)
        self.assertEqual(plan["_meta"]["inputs"]["days_per_week"], 4)

        for cmd in (("week", "--no-write"), ("brief",), ("forecast",)):
            r = run_coach(self.home, *cmd)
            self.assertEqual(r.returncode, 0, cmd + (r.stderr,))
            self.assertIn("km", r.stdout)
            self.assertNotIn("/mi", r.stdout)
            self.assertNotIn("Traceback", r.stdout + r.stderr)

        r = run_coach(self.home, "review", str(LONG_ID), "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        doc = json.loads(r.stdout)
        self.assertEqual(doc["classification"], "LONG RUN")
        self.assertTrue(doc["stream"]["available"])
        self.assertEqual(doc["activity"]["unit"], "km")
        self.assertIn(doc["score"]["grade"], "ABCD")
        dims = [v["dim"] for v in doc["verdicts"]]
        for d in ("easy", "quality", "finish", "load", "sensor"):
            self.assertIn(d, dims)

        r = run_coach(self.home, "review", str(WORKOUT_ID))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("INTERVALS", r.stdout)

    def test_04_second_init_refuses_without_force(self):
        r = run_coach(self.home, "init", "--from-mcp", "--goal-time", "3:45:00")
        self.assertEqual(r.returncode, 2)
        self.assertIn("--force", r.stdout)


if __name__ == "__main__":
    unittest.main()
