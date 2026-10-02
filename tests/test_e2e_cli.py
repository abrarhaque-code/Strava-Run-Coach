"""End to end, as a user would run it: a fresh home, `init --sample`, then the
commands a skill drives, each in its own process with STRAVA_COACH_HOME set
and every STRAVA_* variable stripped (no network, ever)."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run_coach(home: Path, *args, timeout: int = 180) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith("STRAVA_")}
    env["STRAVA_COACH_HOME"] = str(home)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run([sys.executable, str(ROOT / "coach.py"), *args], cwd=str(ROOT), env=env,
                          capture_output=True, text=True, timeout=timeout)


class TestFreshCloneDemo(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.home = Path(cls._tmp.name)
        cls.before = sorted(p.name for p in ROOT.iterdir())
        r = run_coach(cls.home, "init", "--sample")
        assert r.returncode == 0, r.stdout + r.stderr

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_01_sample_bootstrap_writes_into_the_home_only(self):
        self.assertTrue((self.home / "config.json").exists())
        self.assertTrue((self.home / "data" / "city_marathon.generated.json").exists())
        self.assertTrue(any((self.home / "data" / "strava_cache" / "activities").glob("*.json")))
        self.assertEqual(sorted(p.name for p in ROOT.iterdir()), self.before)

    def test_02_full_report(self):
        r = run_coach(self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        for needle in ("DAILY BRIEF", "FITNESS TRACKER", "RACE FORECAST", "POST-RUN REVIEW", "WEEKLY CHECK-IN"):
            self.assertIn(needle, r.stdout)
        self.assertNotIn("Traceback", r.stdout + r.stderr)

    def test_03_status_json(self):
        r = run_coach(self.home, "status", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        s = json.loads(r.stdout)
        self.assertTrue(s["configured"])
        self.assertTrue(s["plan_present"])
        self.assertGreater(s["cache_count"], 10)
        self.assertEqual(s["race"]["name"], "City Marathon")

    def test_04_review_json_is_one_document(self):
        r = run_coach(self.home, "review", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        doc = json.loads(r.stdout)
        self.assertIn(doc["score"]["grade"], "ABCD")
        self.assertEqual([v["dim"] for v in doc["verdicts"]][:1], ["plan"])

    def test_05_week_and_brief(self):
        r = run_coach(self.home, "week", "--no-write")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("7-DAY LAYOUT", r.stdout)
        self.assertIn("4. CHECKPOINT", r.stdout)
        r = run_coach(self.home, "brief")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("TODAY", r.stdout)

    def test_06_plan_flags_and_notes(self):
        r = run_coach(self.home, "plan", "--from-data", "--days", "4", "--long-day", "sun", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        plan = json.loads(r.stdout)
        week = plan["weeks"][0]
        self.assertEqual(len(week["days"]), 7)
        self.assertEqual([d["dow"] for d in week["days"] if d["role"] == "long"], ["sun"])
        self.assertEqual(sum(1 for d in week["days"] if d["role"] not in ("rest", "crosstrain")), 4)
        r = run_coach(self.home, "note", "no speedwork until the calf settles", "--until", "2099-01-01")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = run_coach(self.home, "brief")
        self.assertIn("Standing notes:", r.stdout)
        self.assertIn("no speedwork until the calf settles", r.stdout)

    def test_07_metric_units_everywhere(self):
        cfg_path = self.home / "config.json"
        cfg = json.loads(cfg_path.read_text())
        cfg["athlete"]["units"] = "km"
        cfg_path.write_text(json.dumps(cfg))
        try:
            for cmd in (("brief",), ("week", "--no-write"), ("forecast",), ("metrics",), ("review",)):
                r = run_coach(self.home, *cmd)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertIn("km", r.stdout, cmd)
                self.assertNotIn("/mi", r.stdout, cmd)
                self.assertNotIn("mpw", r.stdout, cmd)
        finally:
            cfg["athlete"]["units"] = "mi"
            cfg_path.write_text(json.dumps(cfg))

    def test_08_unknown_command_exits_1(self):
        r = run_coach(self.home, "bogus")
        self.assertEqual(r.returncode, 1)


if __name__ == "__main__":
    unittest.main()
