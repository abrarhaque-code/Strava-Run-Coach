"""Tests for analysis.load_activities CSV parsing.

Builds a tiny temp CSV using the real bulk-export header so column indices
match what the parser reads, then checks the parsed run and strength session.
"""

import csv
import os
import tempfile
import unittest
from pathlib import Path

import analysis

_ROOT = Path(__file__).resolve().parent.parent
_HEADER_PATH = _ROOT / "activities.csv.example"


def _header():
    with _HEADER_PATH.open("r", encoding="utf-8") as f:
        return next(csv.reader(f))


def _blank_row(n):
    return [""] * n


class TestAnalysisParsing(unittest.TestCase):
    def setUp(self):
        header = _header()
        n = len(header)

        # A 5.0 km run -> 3.106856 mi, moving 1800s -> 30 min.
        run = _blank_row(n)
        run[0] = "TEST_RUN_1"
        run[1] = "Jun 01, 2026, 7:30:00 AM"
        run[2] = "Morning Run"
        run[3] = "Run"
        run[6] = "5.000"          # km
        run[7] = "172.0"          # max HR
        run[8] = "30"             # relative effort
        run[15] = "1850.0"        # elapsed s
        run[16] = "1800.0"        # moving s
        run[20] = "25.0"          # elevation gain
        run[29] = "84.0"          # avg cadence
        run[31] = "150.0"         # avg HR

        lift = _blank_row(n)
        lift[0] = "TEST_LIFT_1"
        lift[1] = "Jun 02, 2026, 8:00:00 PM"
        lift[2] = "Upper Body"
        lift[3] = "Weight Training"
        lift[4] = "Logged with Hevy"
        lift[5] = "2400"          # elapsed seconds -> 40 min
        lift[6] = "0"

        fd, self.path = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        with open(self.path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(header)
            w.writerow(run)
            w.writerow(lift)

    def tearDown(self):
        try:
            os.remove(self.path)
        except OSError:
            pass

    def test_counts(self):
        runs, strength = analysis.load_activities(self.path)
        self.assertEqual(len(runs), 1)
        self.assertEqual(len(strength), 1)

    def test_run_fields(self):
        runs, _ = analysis.load_activities(self.path)
        r = runs[0]
        self.assertAlmostEqual(r.distance_mi, 5.0 * 0.621371, places=3)
        self.assertEqual(r.avg_hr, 150.0)
        self.assertEqual(r.max_hr, 172.0)
        self.assertAlmostEqual(r.moving_time_min, 30.0, places=3)
        # pace = 30 min / 3.1069 mi ~= 9.66 min/mi
        self.assertAlmostEqual(r.pace_min_per_mi, 30.0 / (5.0 * 0.621371),
                               places=2)

    def test_strength_fields(self):
        _, strength = analysis.load_activities(self.path)
        s = strength[0]
        self.assertEqual(s.name, "Upper Body")
        self.assertAlmostEqual(s.elapsed_min, 40.0, places=3)


if __name__ == "__main__":
    unittest.main()
