"""Tests for headless credential loading: .env file vs STRAVA_* env vars.

The overlay exists so cloud sessions and CI can run strava_sync with no .env
on disk. These tests never touch the network — they only exercise _load_env.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import strava_api


def _clean_environ():
    """Environment with every STRAVA_* key removed."""
    return {k: v for k, v in os.environ.items()
            if k not in strava_api.STRAVA_ENV_KEYS}


class TestLoadEnv(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env_path = Path(self.tmp.name) / ".env"
        patcher = mock.patch.object(strava_api, "ENV_PATH", self.env_path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_env_file_only(self):
        self.env_path.write_text(
            "# comment\nSTRAVA_CLIENT_ID=123\nSTRAVA_CLIENT_SECRET=abc\n")
        with mock.patch.dict(os.environ, _clean_environ(), clear=True):
            env = strava_api._load_env()
        self.assertEqual(env["STRAVA_CLIENT_ID"], "123")
        self.assertEqual(env["STRAVA_CLIENT_SECRET"], "abc")

    def test_environ_only(self):
        with mock.patch.dict(os.environ, {**_clean_environ(),
                                          "STRAVA_CLIENT_ID": "999",
                                          "STRAVA_ACCESS_TOKEN": "tok"},
                             clear=True):
            env = strava_api._load_env()
        self.assertEqual(env["STRAVA_CLIENT_ID"], "999")
        self.assertEqual(env["STRAVA_ACCESS_TOKEN"], "tok")

    def test_environ_overlays_env_file(self):
        self.env_path.write_text("STRAVA_CLIENT_ID=file_value\nEXTRA=kept\n")
        with mock.patch.dict(os.environ, {**_clean_environ(),
                                          "STRAVA_CLIENT_ID": "env_value"},
                             clear=True):
            env = strava_api._load_env()
        self.assertEqual(env["STRAVA_CLIENT_ID"], "env_value")
        self.assertEqual(env["EXTRA"], "kept")  # file-only keys survive

    def test_neither_source_raises(self):
        with mock.patch.dict(os.environ, _clean_environ(), clear=True):
            with self.assertRaises(FileNotFoundError):
                strava_api._load_env()


if __name__ == "__main__":
    unittest.main()
