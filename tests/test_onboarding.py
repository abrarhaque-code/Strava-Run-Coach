"""onboarding: config.json from the Strava profile, zones and the cache, with
every derivation noted and every gap listed."""

import contextlib
import io
import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

import config
import mcp_adapter
import onboarding as ob
from enrichment import enrich
from tests.helpers import make_activity
from tests.test_zones_calibration import ZONES

MI = 1609.34
TODAY = date(2026, 10, 1)
PROFILE = {
    "first_name": "Sam", "last_name": "Runner", "measurement_preference": "Metric",
    "current_focus": {"focus_type": "TrainForEvent", "open_text": "PR at the City Marathon",
                      "expires_at_local": "2026-11-02T00:00:00"},
}


def _run(days_ago, miles=5.0, pace=10.0, hr=140.0, max_hr=165.0, **over):
    d = (TODAY - timedelta(days=days_ago)).isoformat() + "T07:00:00"
    kw = dict(start_date_local=d, start_date=d + "Z", distance=miles * MI,
              moving_time=int(miles * pace * 60), average_heartrate=hr, max_heartrate=max_hr)
    kw.update(over)
    return enrich(make_activity(**kw))


ACTS = ([_run(d, hr=140, max_hr=165) for d in (2, 5, 9, 12, 16, 19, 23, 26)]
        + [_run(7, miles=4, pace=8.0, hr=168, max_hr=178), _run(14, miles=4, pace=8.1, hr=167, max_hr=176)])


class TestDerivations(unittest.TestCase):
    def test_race_from_profile_text(self):
        r = ob.race_from_profile(PROFILE, TODAY)
        self.assertEqual((r["name"], r["date"], r["distance_mi"]), ("City Marathon", "2026-11-01", 26.2))
        half = dict(PROFILE, current_focus={"focus_type": "TrainForEvent", "open_text": "Training for the Spring Half",
                                            "expires_at_local": "2027-05-17"})
        r2 = ob.race_from_profile(half, TODAY)
        self.assertEqual((r2["name"], r2["distance_mi"]), ("Spring Half", 13.1))
        self.assertEqual(ob.distance_from_text("Run the Brooklyn 10K"), 6.21)
        self.assertEqual(ob.distance_from_text("42.2 km in Berlin"), 26.22)
        self.assertIsNone(ob.distance_from_text("get faster"))
        self.assertIsNone(ob.race_from_profile(dict(PROFILE, current_focus={"focus_type": "GetFaster"}), TODAY))
        self.assertIsNone(ob.race_from_profile(None, TODAY))

    def test_observed_max_hr_rules(self):
        self.assertEqual(ob.observed_max_hr(ACTS)[0], 178)                 # 178 and 176 agree
        spike = ACTS[:8] + [_run(30, max_hr=190)]
        self.assertEqual(ob.observed_max_hr(spike)[0], 165)                 # 190 stood alone
        self.assertIn("spike", ob.observed_max_hr(spike)[1])
        self.assertIsNone(ob.observed_max_hr(ACTS[:2])[0])
        self.assertIn("unverified", ob.observed_max_hr([])[1])

    def test_easy_pace_and_volume(self):
        band = ob.easy_pace_from_data(ACTS, easy_cap=148, today=TODAY)
        self.assertEqual(band, (10.25, 9.75))
        self.assertIsNone(ob.easy_pace_from_data(ACTS[:2], easy_cap=148, today=TODAY))
        self.assertAlmostEqual(ob.trailing_mpw(ACTS, TODAY), (8 * 5 + 2 * 4) / 4, places=1)

    def test_profile_fields(self):
        self.assertEqual(ob.profile_name(PROFILE), "Sam Runner")
        self.assertEqual(ob.profile_units(PROFILE), "km")
        self.assertEqual(ob.profile_units({"measurement_preference": "Imperial"}), "mi")
        self.assertIsNone(ob.profile_units({}))
        self.assertEqual(ob.race_name_from_text("Sub-4 at the Chicago Marathon"), "Chicago Marathon")


class TestPlanConfig(unittest.TestCase):
    def test_profile_zones_and_history_fill_the_config(self):
        cfg, needs, notes = ob.plan_config(PROFILE, ZONES, ACTS, {"goal_time": "3:45:00"}, TODAY)
        config._validate(cfg)
        ath = cfg["athlete"]
        self.assertEqual((ath["name"], ath["units"], ath["max_hr"]), ("Sam Runner", "km", 178))
        self.assertEqual(ath["easy_hr_cap"], 160)                          # zones Z2 top
        self.assertEqual(ath["threshold_hr"], 177)                         # zones Z4 floor
        race = cfg["races"][0]
        self.assertEqual(cfg["active_race"], race["id"])
        self.assertEqual((race["name"], race["date"], race["distance_mi"], race["goal_time"]),
                         ("City Marathon", "2026-11-01", 26.2, "3:45:00"))
        self.assertAlmostEqual(race["goal_pace_min_per_mi"], 8.588, places=2)
        self.assertEqual(cfg["race_history"], [])
        self.assertEqual(cfg["pace_zones"]["easy"], {"floor": 10.25, "ceiling": 9.75})
        self.assertAlmostEqual(cfg["pace_zones"]["race_pace"]["ceiling"], 8.59, places=2)
        self.assertEqual(cfg["scenario"]["entries"], [10, 15, 20])
        self.assertEqual(needs, [])
        self.assertEqual(cfg["_meta"]["needs"], [])
        self.assertTrue(any("Strava training focus" in n for n in notes))
        self.assertTrue(any("highest recorded" in n for n in notes))

    def test_missing_goal_is_estimated_and_listed(self):
        cfg, needs, notes = ob.plan_config(PROFILE, ZONES, ACTS, {}, TODAY)
        self.assertEqual(needs, ["goal_time"])
        self.assertRegex(cfg["races"][0]["goal_time"], r"^\d:\d\d:\d\d$")
        self.assertTrue(any("ESTIMATED" in n for n in notes))

    def test_flags_override_the_profile(self):
        cfg, needs, _ = ob.plan_config(PROFILE, None, [], {"race_name": "Autumn Half", "race_date": "2026-11-15",
                                                            "distance": "21.1km", "goal_time": "1:50:00",
                                                            "units": "mi", "name": "Pat"}, TODAY)
        race = cfg["races"][0]
        self.assertEqual((race["name"], race["date"]), ("Autumn Half", "2026-11-15"))
        self.assertAlmostEqual(race["distance_mi"], 13.11, places=2)
        self.assertEqual((cfg["athlete"]["name"], cfg["athlete"]["units"]), ("Pat", "mi"))
        self.assertEqual(needs, [])

    def test_nothing_known_still_validates_and_asks(self):
        cfg, needs, notes = ob.plan_config(None, None, [], {}, TODAY)
        config._validate(cfg)
        self.assertIn("race", needs)
        self.assertTrue(any("unverified" in n for n in notes))
        self.assertEqual(cfg["athlete"]["max_hr"], 189)                    # example default kept
        self.assertEqual(cfg["race_history"], [])


class TestApplyAndCli(unittest.TestCase):
    def test_apply_refuses_then_backs_up(self):
        cfg, _, _ = ob.plan_config(PROFILE, ZONES, ACTS, {"goal_time": "3:45:00"}, TODAY)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            with mock.patch.object(config, "CONFIG_PATH", path):
                ob.apply(cfg)
                self.assertTrue(path.exists())
                with self.assertRaises(FileExistsError):
                    ob.apply(cfg)
                cfg["athlete"]["name"] = "Changed"
                _, backup = ob.apply(cfg, force=True)
                self.assertTrue(backup.exists())
                self.assertEqual(json.loads(path.read_text())["athlete"]["name"], "Changed")

    def test_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            inbox = Path(tmp) / "mcp"
            inbox.mkdir()
            # a persisted tool result, as Claude Code saves large MCP replies
            (inbox / "profile.json").write_text(json.dumps([{"type": "text", "text": json.dumps(PROFILE)}]))
            (inbox / "zones.json").write_text(json.dumps(ZONES))
            cache = Path(tmp) / "cache"
            cache.mkdir()
            for a in ACTS:
                (cache / f"{a['id']}.json").write_text(json.dumps(a))
            cfg_path = Path(tmp) / "config.json"
            with mock.patch.object(config, "CONFIG_PATH", cfg_path), \
                    mock.patch.object(mcp_adapter, "CACHE_DIR", cache):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    rc = ob.main(["--from-mcp", str(inbox), "--goal-time", "3:45:00", "--dry-run"])
            text = out.getvalue()
        self.assertEqual(rc, 0)
        self.assertFalse(cfg_path.exists())
        self.assertIn("Dry run: nothing written", text)
        self.assertIn("Sam Runner", text)
        self.assertIn("City Marathon on 2026-11-01", text)
        self.assertIn("profile yes, zones yes; 10 cached activities", text)

    def test_existing_config_without_force_is_rc_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "config.json"
            cfg_path.write_text("{}")
            with mock.patch.object(config, "CONFIG_PATH", cfg_path), \
                    mock.patch.object(mcp_adapter, "CACHE_DIR", Path(tmp) / "none"):
                with contextlib.redirect_stdout(io.StringIO()):
                    rc = ob.main(["--from-mcp", str(Path(tmp) / "mcp"), "--no-race"])
            self.assertEqual(rc, 2)
            self.assertEqual(cfg_path.read_text(), "{}")          # untouched


if __name__ == "__main__":
    unittest.main()
