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


class TestRefreshAndStreams(unittest.TestCase):
    """No network: the token endpoint and the request layer are mocked."""

    def _api(self):
        api = strava_api.StravaAPI.__new__(strava_api.StravaAPI)
        api.env = {"STRAVA_CLIENT_ID": "1", "STRAVA_CLIENT_SECRET": "s",
                   "STRAVA_REFRESH_TOKEN": "r", "STRAVA_ACCESS_TOKEN": "a",
                   "STRAVA_TOKEN_EXPIRES_AT": "0"}
        return api

    def test_rotated_refresh_token_is_explained(self):
        import io
        import urllib.error
        body = b'{"message":"Bad Request","errors":[{"code":"invalid","field":"refresh_token"}]}'
        err = urllib.error.HTTPError("https://www.strava.com/oauth/token", 400, "Bad Request",
                                     {}, io.BytesIO(body))
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(strava_api.AuthError) as cm:
                self._api()._refresh_access_token()
        msg = str(cm.exception)
        self.assertIn("REFRESH TOKEN (400)", msg)
        self.assertIn("rotates it on every refresh", msg)
        self.assertIn("strava_authorize.py", msg)
        self.assertIn("coach.py analyze", msg)

    def test_other_refresh_errors_keep_the_plain_message(self):
        import io
        import urllib.error
        err = urllib.error.HTTPError("https://www.strava.com/oauth/token", 503, "Unavailable",
                                     {}, io.BytesIO(b"down"))
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(strava_api.AuthError) as cm:
                self._api()._refresh_access_token()
        self.assertIn("Token refresh failed (503)", str(cm.exception))
        self.assertNotIn("rotates", str(cm.exception))

    def test_default_stream_keys_include_moving_and_watts(self):
        api = self._api()
        with mock.patch.object(api, "_request", return_value={}) as req:
            api.get_activity_streams(42)
        path, params = req.call_args[0]
        self.assertEqual(path, "/activities/42/streams")
        keys = params["keys"].split(",")
        for k in ("time", "distance", "heartrate", "velocity_smooth", "cadence", "altitude",
                  "moving", "watts"):
            self.assertIn(k, keys)
