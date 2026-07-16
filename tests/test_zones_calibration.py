"""Tests for Strava-MCP get_athlete_zones -> config calibration.

The payload shape is the live MCP response: heart_rate_zones as bpm bands,
run_zones as m/s speed bands (5 or 6 zones; index 2 = tempo, 3 = threshold
either way). The mapper must stay conservative — max_hr is never derived.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import wizard

# Live-sampled get_athlete_zones payload (values are Strava-derived, not the
# athlete's calibrated numbers — the mapper's caveat note covers that).
ZONES = {
    "heart_rate_zones": [
        {"min": 0, "max": 129}, {"min": 130, "max": 160},
        {"min": 161, "max": 176}, {"min": 177, "max": 192}, {"min": 193},
    ],
    "heart_rate_zone_source": "MaxHeartRate",
    "run_zones": [
        {"min": 0, "max": 2.673}, {"min": 2.673, "max": 3.106},
        {"min": 3.106, "max": 3.46}, {"min": 3.46, "max": 3.695},
        {"min": 3.695, "max": 3.931}, {"min": 3.931},
    ],
    "run_zone_source": "PerformancePredictions",
}


class TestZonesToConfigPatch(unittest.TestCase):
    def test_hr_boundaries(self):
        patch, _ = wizard.zones_to_config_patch(ZONES)
        ath = patch["athlete"]
        self.assertEqual(ath["recovery_hr_cap"], 129)   # HR Z1 top
        self.assertEqual(ath["easy_hr_cap"], 160)       # HR Z2 top
        self.assertEqual(ath["threshold_hr"], 177)      # HR Z4 floor
        self.assertNotIn("max_hr", ath)                 # never derived

    def test_pace_bands_from_speed_zones(self):
        patch, _ = wizard.zones_to_config_patch(ZONES)
        tempo = patch["pace_zones"]["tempo"]
        thr = patch["pace_zones"]["threshold"]
        # floor is the SLOWER boundary (bigger min/mi), ceiling the faster
        self.assertAlmostEqual(tempo["floor"], 8.64, places=2)
        self.assertAlmostEqual(tempo["ceiling"], 7.75, places=2)
        self.assertEqual(tempo["hr_range"], [161, 176])
        self.assertAlmostEqual(thr["floor"], 7.75, places=2)
        self.assertAlmostEqual(thr["ceiling"], 7.26, places=2)
        self.assertEqual(thr["hr_range"], [177, 192])
        # threshold pace anchored on the run-Z4 midpoint
        self.assertAlmostEqual(patch["athlete"]["threshold_pace_min_per_mi"],
                               7.5, places=1)

    def test_source_caveat_in_notes(self):
        _, notes = wizard.zones_to_config_patch(ZONES)
        self.assertTrue(any("MaxHeartRate" in n for n in notes))

    def test_missing_zones_yield_empty_patch(self):
        patch, _ = wizard.zones_to_config_patch({})
        self.assertEqual(patch, {})

    def test_apply_writes_config_preserving_other_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            zones_path = tmp / "zones.json"
            zones_path.write_text(json.dumps(ZONES))
            cfg_path = tmp / "config.json"
            with mock.patch.object(wizard, "CONFIG_PATH", cfg_path):
                rc = wizard.apply_mcp_zones(str(zones_path))
            self.assertEqual(rc, 0)
            cfg = json.loads(cfg_path.read_text())
            # Zone-derived fields landed...
            self.assertEqual(cfg["athlete"]["threshold_hr"], 177)
            # ...and untouched example fields survive (deep merge, not replace)
            self.assertIn("races", cfg)
            self.assertIn("max_hr", cfg["athlete"])


if __name__ == "__main__":
    unittest.main()
