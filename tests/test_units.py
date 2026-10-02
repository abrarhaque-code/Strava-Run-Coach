"""units.py: miles/km display layer over an unchanged internal canonical."""

import tempfile
import unittest
from pathlib import Path

import units
from tests.helpers import temp_config


class TestMiles(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._ctx = temp_config(Path(self._tmp.name), {"athlete": {"units": "mi"}})
        self._ctx.__enter__()

    def tearDown(self):
        self._ctx.__exit__(None, None, None)
        self._tmp.cleanup()

    def test_labels(self):
        self.assertEqual(units.unit(), "mi")
        self.assertEqual(units.pace_label(), "/mi")
        self.assertEqual(units.volume_label(), "mpw")

    def test_fmt_dist(self):
        self.assertEqual(units.fmt_dist(1609.34), "1.0 mi")
        self.assertEqual(units.fmt_dist(mi=13.1, label=False), "13.1")
        self.assertEqual(units.fmt_dist(), "N/A")

    def test_fmt_pace_rounds_and_rolls(self):
        self.assertEqual(units.fmt_pace(8.583), "8:35/mi")
        self.assertEqual(units.fmt_pace(9.9999, label=False), "10:00")
        self.assertEqual(units.fmt_pace(0), "N/A")
        self.assertEqual(units.fmt_pace(None), "N/A")
        # seconds per metre input: 5:00/km == 8:03/mi
        self.assertEqual(units.fmt_pace(sec_per_m=0.3, label=False), "8:03")

    def test_fmt_pace_range_orders_faster_first(self):
        self.assertEqual(units.fmt_pace_range(10.5, 10.0), "10:00-10:30/mi")

    def test_parse_dist(self):
        self.assertAlmostEqual(units.parse_dist("25"), 25.0)
        self.assertAlmostEqual(units.parse_dist("25mi"), 25.0)
        self.assertAlmostEqual(units.parse_dist("40km"), 40 / 1.609344, places=6)
        self.assertAlmostEqual(units.parse_dist("40 k"), 40 / 1.609344, places=6)
        with self.assertRaises(ValueError):
            units.parse_dist("lots")

    def test_parse_pace(self):
        self.assertAlmostEqual(units.parse_pace("10:00"), 10.0)
        self.assertAlmostEqual(units.parse_pace("10:00/mi"), 10.0)
        self.assertAlmostEqual(units.parse_pace("6:13/km"), (6 + 13 / 60) * 1.609344, places=6)

    def test_round_dist_in_user_units(self):
        self.assertAlmostEqual(units.round_dist(5.26), 5.5)
        self.assertAlmostEqual(units.round_dist(5.24), 5.0)

    def test_to_mi(self):
        self.assertAlmostEqual(units.to_mi(10, "km"), 10 / 1.609344, places=6)
        self.assertAlmostEqual(units.to_mi(10, "k"), 10 / 1.609344, places=6)
        self.assertAlmostEqual(units.to_mi(10, "mi"), 10.0)


class TestKilometres(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._ctx = temp_config(Path(self._tmp.name), {"athlete": {"units": "km"}})
        self._ctx.__enter__()

    def tearDown(self):
        self._ctx.__exit__(None, None, None)
        self._tmp.cleanup()

    def test_labels(self):
        self.assertEqual(units.unit(), "km")
        self.assertEqual(units.pace_label(), "/km")
        self.assertEqual(units.volume_label(), "km/wk")

    def test_fmt_dist_converts(self):
        self.assertEqual(units.fmt_dist(10000), "10.0 km")
        self.assertEqual(units.fmt_dist(mi=13.1), "21.1 km")

    def test_fmt_pace_converts(self):
        # 8:03/mi is 5:00/km
        self.assertEqual(units.fmt_pace(8.0467), "5:00/km")
        self.assertEqual(units.fmt_pace(10.0, label=False), "6:13")

    def test_parse_bare_uses_km(self):
        self.assertAlmostEqual(units.parse_dist("40"), 40 / 1.609344, places=6)
        self.assertAlmostEqual(units.parse_pace("6:13"), (6 + 13 / 60) * 1.609344, places=6)
        # explicit suffix still wins
        self.assertAlmostEqual(units.parse_dist("25mi"), 25.0)

    def test_round_dist_rounds_in_km(self):
        # 5.26 mi = 8.47 km -> 8.5 km -> back to miles
        self.assertAlmostEqual(units.mi_to_user(units.round_dist(5.26)), 8.5)


if __name__ == "__main__":
    unittest.main()
