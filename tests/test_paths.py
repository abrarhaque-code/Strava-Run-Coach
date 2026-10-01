"""The state-home layer: STRAVA_COACH_HOME, plugin data dir, code dir."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import config

_ROOT = Path(__file__).resolve().parent.parent

_PROBE = r"""
import json, config, metrics, fitness_tracker, strava_sync, marathon_plan, scenario
import mcp_adapter, post_run_review, daily_brief, dashboard, plan_generator, race_predictor, wizard
print(json.dumps({
  "home": str(config.HOME), "csv": str(config.CSV_PATH), "cfg": str(config.CONFIG_PATH),
  "metrics_cache": str(metrics.CACHE_DIR), "ft_csv": str(fitness_tracker.CSV_PATH),
  "sync_lock": str(strava_sync.LOCK_FILE), "state": str(marathon_plan.STATE_PATH),
  "scenario_cache": str(scenario.CACHE_DIR), "mcp_cache": str(mcp_adapter.CACHE_DIR),
  "streams": str(post_run_review.STREAMS_DIR), "brief": str(daily_brief.BRIEF_PATH),
  "dash": str(dashboard.OUT_DIR), "plan_out": str(plan_generator.OUT_PATH),
  "rp": race_predictor.ACTIVITIES_DIR, "wiz_cfg": str(wizard.CONFIG_PATH),
  "example": str(config.EXAMPLE_PATH),
}))
"""


class TestPluginDataHome(unittest.TestCase):
    def test_under_plugins_tree(self):
        code = Path("/u/.claude/plugins/cache/mk/strava-run-coach/1.1.0")
        self.assertEqual(config._plugin_data_home(code),
                         Path("/u/.claude/plugins/data/strava-run-coach"))

    def test_plain_clone_is_none(self):
        self.assertIsNone(config._plugin_data_home(Path("/home/me/src/Strava-Run-Coach")))
        self.assertIsNone(config._plugin_data_home(Path("/home/me/.claude/skills/x")))


class TestHomeEnv(unittest.TestCase):
    def test_every_state_path_moves_with_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, STRAVA_COACH_HOME=tmp)
            out = subprocess.run([sys.executable, "-c", _PROBE], cwd=_ROOT, env=env,
                                 capture_output=True, text=True, check=True).stdout
            paths = json.loads(out)
            example = paths.pop("example")
            self.assertTrue(example.startswith(str(_ROOT)), example)  # shipped file stays put
            for key, val in paths.items():
                self.assertTrue(val.startswith(tmp), f"{key} -> {val} not under {tmp}")

    def test_no_env_means_code_dir(self):
        env = {k: v for k, v in os.environ.items() if k != "STRAVA_COACH_HOME"}
        out = subprocess.run([sys.executable, "-c", "import config; print(config.HOME)"],
                             cwd=_ROOT, env=env, capture_output=True, text=True, check=True).stdout
        self.assertEqual(Path(out.strip()).resolve(), _ROOT)


class TestPlanPath(unittest.TestCase):
    def test_convention_is_generated_json_per_race(self):
        self.assertEqual(config.plan_path({"id": "nyc"}),
                         config.DATA_DIR / "nyc.generated.json")
        self.assertEqual(config.plan_path({"id": "x", "plan": "generated_half"}),
                         config.DATA_DIR / "x.generated.json")

    def test_explicit_json_is_honored(self):
        rel = config.plan_path({"id": "x", "plan": "data/custom.json"})
        self.assertEqual(rel, config.HOME / "data" / "custom.json")
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "mine.json"
            self.assertFalse(config.has_structured_plan({"id": "x", "plan": str(f)}))
            f.write_text("{}", encoding="utf-8")
            self.assertTrue(config.has_structured_plan({"id": "x", "plan": str(f)}))

    def test_missing_race_is_false(self):
        self.assertFalse(config.has_structured_plan({}))
        self.assertFalse(config.has_structured_plan(None))


if __name__ == "__main__":
    unittest.main()
