"""status: what is cached, what the MCP still needs to fetch, and the next step."""

import contextlib
import io
import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

import mcp_adapter
import status
from enrichment import enrich
from tests.helpers import make_activity, temp_activity_data, temp_plan

MI = 1609.34
TODAY = date(2026, 10, 1)


def _run(aid, days_ago, miles=5.0, hr=None, name="Run"):
    d = (TODAY - timedelta(days=days_ago)).isoformat() + "T07:00:00"
    return enrich(make_activity(id=aid, name=name, start_date_local=d, start_date=d + "Z",
                                distance=miles * MI, moving_time=int(miles * 600), average_heartrate=hr))


class TestSnapshot(unittest.TestCase):
    def test_empty_home_points_at_the_mcp_pull(self):
        with tempfile.TemporaryDirectory() as tmp:
            with temp_plan(Path(tmp)), temp_activity_data(Path(tmp)) as paths:
                s = status.snapshot(today=TODAY, cache_dir=paths["cache_dir"], inbox=Path(tmp) / "mcp",
                                    streams_dir=paths["streams_dir"])
        self.assertEqual(s["cache_count"], 0)
        self.assertFalse(s["plan_present"])
        self.assertEqual(s["inbox"]["list_pages"], 0)
        self.assertTrue(s["next"][0].startswith("No activities yet"))
        self.assertTrue(any("init --from-mcp" in n or "plan --from-data" in n for n in s["next"]))

    def test_with_data_lists_what_to_fetch(self):
        acts = [_run(1, 3), _run(2, 5, miles=12, hr=150.0), _run(3, 20, hr=140.0),
                _run(4, 40, hr=141.0), _run(5, 55, hr=139.0)]
        with tempfile.TemporaryDirectory() as tmp:
            inbox = Path(tmp) / "mcp"
            (inbox / "list").mkdir(parents=True)
            (inbox / "list" / "page1.json").write_text("[]", encoding="utf-8")
            (inbox / "profile.json").write_text("{}", encoding="utf-8")
            with temp_plan(Path(tmp)), temp_activity_data(Path(tmp), cache_activities=acts) as paths:
                s = status.snapshot(today=TODAY, cache_dir=paths["cache_dir"], inbox=inbox,
                                    streams_dir=paths["streams_dir"])
                text = status.render(s)
        self.assertEqual((s["cache_count"], s["run_count"]), (5, 5))
        self.assertEqual(s["history_days"], 55)
        self.assertFalse(s["history_sufficient"])
        self.assertEqual(s["fetch"]["perf_needed"], [1])
        self.assertEqual(s["fetch"]["streams_needed"], [2])
        self.assertEqual(s["newest_activity"], "2026-09-28")
        self.assertEqual(s["newest_run_id"], 1)
        self.assertEqual(s["inbox"], {"list_pages": 1, "perf": 0, "streams": 0, "profile": True, "zones": False})
        joined = " ".join(s["next"])
        self.assertIn("Only 55 days of history", joined)
        self.assertIn("1 recent run(s) lack heart rate", joined)
        self.assertIn("1 long run(s) or workout(s) lack streams", joined)
        self.assertIn("5 cached (5 runs), 55 days of history", text)
        self.assertIn("Still to fetch: 1 performance, 1 streams", text)
        self.assertIn("NEXT", text)

    def test_cli_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            with temp_plan(Path(tmp)), temp_activity_data(Path(tmp)) as paths, \
                    mock.patch.object(mcp_adapter, "CACHE_DIR", paths["cache_dir"]), \
                    mock.patch.object(mcp_adapter, "MCP_DIR", Path(tmp) / "mcp"), \
                    mock.patch.object(mcp_adapter, "STREAMS_DIR", paths["streams_dir"]):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    rc = status.main(["--json"])
        self.assertEqual(rc, 0)
        doc = json.loads(out.getvalue())
        self.assertIn("next", doc)
        self.assertEqual(doc["cache_count"], 0)


if __name__ == "__main__":
    unittest.main()
