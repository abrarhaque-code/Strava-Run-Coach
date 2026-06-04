"""Strava API client with automatic token refresh.

Reads credentials from .env. On each call, checks if access_token is expired
or close to it. If so, calls /oauth/token with refresh_token to get a fresh
access_token and writes new tokens back to .env.

Usage:
    api = StravaAPI()
    activities = api.list_activities(after=some_timestamp)
    activity = api.get_activity(activity_id)
    streams = api.get_activity_streams(activity_id)

Stdlib only — uses urllib for HTTP.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


BASE_URL = "https://www.strava.com/api/v3"
TOKEN_URL = "https://www.strava.com/oauth/token"
ENV_PATH = Path(__file__).parent / ".env"

# Refresh access token if it expires within this many seconds.
REFRESH_THRESHOLD_SEC = 300  # 5 min buffer


class StravaAPIError(Exception):
    """Raised when the Strava API returns an error response."""
    pass


class RateLimitError(StravaAPIError):
    """Raised when Strava returns 429 (we hit the limit)."""
    pass


class RateLimitWarning(StravaAPIError):
    """Raised PRE-flight when we're about to hit the limit and --no-wait is set."""
    pass


class AuthError(StravaAPIError):
    """Raised when authentication fails (bad token, missing scope, etc)."""
    pass


@dataclass
class RateLimitState:
    """Parsed Strava rate limit headers.

    Strava returns counters as comma-separated pairs: "15min,daily".
    Limits typically: read 100/15min and 1000/daily; overall 200/15min and 2000/daily.
    """
    usage_15min: int = 0
    limit_15min: int = 100
    usage_daily: int = 0
    limit_daily: int = 1000
    read_usage_15min: int = 0
    read_limit_15min: int = 100
    read_usage_daily: int = 0
    read_limit_daily: int = 1000
    last_seen: float = 0.0  # unix timestamp of last update

    def update_from_headers(self, headers) -> None:
        """Update from response headers (case-insensitive lookup)."""
        def get(name: str) -> Optional[str]:
            for k in (name, name.lower(), name.upper()):
                v = headers.get(k)
                if v is not None:
                    return v
            return None

        def parse_pair(val: Optional[str]) -> tuple:
            if not val:
                return (0, 0)
            parts = val.split(",")
            try:
                return (int(parts[0].strip()), int(parts[1].strip()) if len(parts) > 1 else 0)
            except ValueError:
                return (0, 0)

        usage = get("X-RateLimit-Usage")
        limit = get("X-RateLimit-Limit")
        read_usage = get("X-ReadRateLimit-Usage")
        read_limit = get("X-ReadRateLimit-Limit")

        if usage:
            self.usage_15min, self.usage_daily = parse_pair(usage)
        if limit:
            self.limit_15min, self.limit_daily = parse_pair(limit)
        if read_usage:
            self.read_usage_15min, self.read_usage_daily = parse_pair(read_usage)
        if read_limit:
            self.read_limit_15min, self.read_limit_daily = parse_pair(read_limit)
        self.last_seen = time.time()

    def at_threshold(self, threshold: float = 0.9) -> bool:
        """True if we're at or above the threshold (default 90%) on any limit."""
        # Use whichever signal we have. Read limits are stricter for our usage.
        checks = []
        if self.read_limit_15min:
            checks.append(self.read_usage_15min / self.read_limit_15min)
        if self.limit_15min:
            checks.append(self.usage_15min / self.limit_15min)
        if self.read_limit_daily:
            checks.append(self.read_usage_daily / self.read_limit_daily)
        if self.limit_daily:
            checks.append(self.usage_daily / self.limit_daily)
        return any(c >= threshold for c in checks) if checks else False

    def seconds_until_15min_window_resets(self) -> int:
        """Strava's 15-min windows reset on the quarter hour (00, 15, 30, 45)."""
        now = time.time()
        # Quarter-hour boundary in UTC
        utc_minutes = (int(now) // 60) % 60
        next_quarter = ((utc_minutes // 15) + 1) * 15
        seconds_into_min = int(now) % 60
        return (next_quarter - utc_minutes) * 60 - seconds_into_min

    def __str__(self) -> str:
        return (f"15min: {self.read_usage_15min}/{self.read_limit_15min} read, "
                f"{self.usage_15min}/{self.limit_15min} overall | "
                f"daily: {self.read_usage_daily}/{self.read_limit_daily} read, "
                f"{self.usage_daily}/{self.limit_daily} overall")


def _load_env() -> dict:
    """Read .env into a dict. No external deps."""
    if not ENV_PATH.exists():
        raise FileNotFoundError(f"Missing {ENV_PATH}. Copy .env.example and fill in.")
    env = {}
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        env[key.strip()] = val.strip()
    return env


def _save_env(env: dict) -> None:
    """Write dict back to .env, preserving comments where possible."""
    # Preserve original comments by reading raw lines and substituting values.
    if ENV_PATH.exists():
        original = ENV_PATH.read_text().splitlines()
    else:
        original = []

    out_lines = []
    seen_keys = set()
    for line in original:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            out_lines.append(line)
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in env:
            out_lines.append(f"{key}={env[key]}")
            seen_keys.add(key)
        else:
            out_lines.append(line)

    # Append any new keys not in the original file.
    for key, val in env.items():
        if key not in seen_keys:
            out_lines.append(f"{key}={val}")

    ENV_PATH.write_text("\n".join(out_lines) + "\n")


class StravaAPI:
    """Client for the Strava v3 REST API."""

    def __init__(self, wait_on_rate_limit: bool = True):
        self.env = _load_env()
        self._validate_env()
        self.rate_limits = RateLimitState()
        self.wait_on_rate_limit = wait_on_rate_limit

    def _validate_env(self):
        required = ["STRAVA_CLIENT_ID", "STRAVA_CLIENT_SECRET",
                    "STRAVA_ACCESS_TOKEN", "STRAVA_REFRESH_TOKEN"]
        missing = [k for k in required if not self.env.get(k)]
        if missing:
            raise AuthError(f"Missing required env vars: {missing}")

    def _expires_at(self) -> int:
        """Unix timestamp when current access token expires."""
        try:
            return int(self.env.get("STRAVA_TOKEN_EXPIRES_AT", "0"))
        except ValueError:
            return 0

    def _is_token_expired(self) -> bool:
        """True if access token is expired or within 5 min of expiring."""
        return time.time() >= self._expires_at() - REFRESH_THRESHOLD_SEC

    def _refresh_access_token(self) -> None:
        """Use refresh_token to get a new access_token. Updates .env."""
        data = urllib.parse.urlencode({
            "client_id": self.env["STRAVA_CLIENT_ID"],
            "client_secret": self.env["STRAVA_CLIENT_SECRET"],
            "grant_type": "refresh_token",
            "refresh_token": self.env["STRAVA_REFRESH_TOKEN"],
        }).encode("utf-8")

        req = urllib.request.Request(
            TOKEN_URL,
            data=data,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            raise AuthError(f"Token refresh failed ({e.code}): {body}")

        # Strava returns: access_token, refresh_token, expires_at, expires_in, token_type
        self.env["STRAVA_ACCESS_TOKEN"] = payload["access_token"]
        self.env["STRAVA_REFRESH_TOKEN"] = payload["refresh_token"]
        self.env["STRAVA_TOKEN_EXPIRES_AT"] = str(payload["expires_at"])
        _save_env(self.env)

        print(f"  [strava_api] Refreshed access token (expires "
              f"{time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(payload['expires_at']))} UTC)")

    def _ensure_fresh_token(self) -> None:
        """Refresh access token if expired or close to expiring."""
        if self._is_token_expired():
            self._refresh_access_token()

    def _check_rate_limit_preflight(self) -> None:
        """Refuse or wait if we're near the rate limit before making a call."""
        if not self.rate_limits.last_seen:
            return  # haven't seen any responses yet, can't check
        if not self.rate_limits.at_threshold(0.9):
            return

        wait_sec = self.rate_limits.seconds_until_15min_window_resets() + 5
        msg = (f"Rate limit at threshold ({self.rate_limits}). "
               f"Window resets in {wait_sec}s.")

        if not self.wait_on_rate_limit:
            raise RateLimitWarning(msg)

        if wait_sec > 900:
            # Daily limit hit, don't sleep all night
            raise RateLimitWarning(f"{msg} Wait too long ({wait_sec}s); aborting.")

        print(f"  [strava_api] {msg} Sleeping {wait_sec}s.", file=sys.stderr)
        time.sleep(wait_sec)
        # Reset 15-min counters after waiting
        self.rate_limits.read_usage_15min = 0
        self.rate_limits.usage_15min = 0

    def _request(self, path: str, params: Optional[dict] = None) -> dict:
        """Make an authenticated GET request to the Strava API."""
        self._ensure_fresh_token()
        self._check_rate_limit_preflight()

        url = f"{BASE_URL}{path}"
        if params:
            # Filter out None values
            params = {k: v for k, v in params.items() if v is not None}
            url += "?" + urllib.parse.urlencode(params)

        req = urllib.request.Request(url, method="GET")
        req.add_header("Authorization", f"Bearer {self.env['STRAVA_ACCESS_TOKEN']}")

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                self.rate_limits.update_from_headers(resp.headers)
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            if e.code == 401:
                raise AuthError(f"401 Unauthorized: {body}")
            if e.code == 403:
                raise AuthError(f"403 Forbidden (likely scope issue): {body}")
            if e.code == 404:
                raise StravaAPIError(f"404 Not Found: {path}")
            if e.code == 429:
                raise RateLimitError(f"429 Rate Limited: {body}")
            raise StravaAPIError(f"HTTP {e.code}: {body}")

    # ----- Public API methods -----

    def get_athlete(self) -> dict:
        """Get the authenticated athlete's profile."""
        return self._request("/athlete")

    def get_athlete_zones(self) -> dict:
        """Get HR/power zones (requires profile:read_all scope — may fail with 'read')."""
        return self._request("/athlete/zones")

    def get_athlete_stats(self, athlete_id: int) -> dict:
        """Get totals: recent runs, YTD runs, all-time runs."""
        return self._request(f"/athletes/{athlete_id}/stats")

    def list_activities(self, after: Optional[int] = None,
                        before: Optional[int] = None,
                        page: int = 1,
                        per_page: int = 200) -> list:
        """List authenticated athlete's activities.

        Parameters
        ----------
        after : int, optional
            Unix timestamp. Only return activities after this time.
        before : int, optional
            Unix timestamp. Only return activities before this time.
        page : int
            Page number (1-indexed).
        per_page : int
            Activities per page (max 200).
        """
        return self._request("/athlete/activities", {
            "after": after,
            "before": before,
            "page": page,
            "per_page": per_page,
        })

    def list_all_activities(self, after: Optional[int] = None,
                            before: Optional[int] = None) -> list:
        """List all activities, paginating until exhausted. Returns combined list."""
        all_activities = []
        page = 1
        while True:
            batch = self.list_activities(after=after, before=before,
                                          page=page, per_page=200)
            if not batch:
                break
            all_activities.extend(batch)
            if len(batch) < 200:
                break  # last page
            page += 1
        return all_activities

    def get_activity(self, activity_id: int, include_all_efforts: bool = False) -> dict:
        """Get full detail for a single activity."""
        return self._request(f"/activities/{activity_id}", {
            "include_all_efforts": str(include_all_efforts).lower()
        })

    def get_activity_streams(self, activity_id: int,
                              keys: Optional[list] = None) -> dict:
        """Get per-second time-series streams for an activity.

        Available keys: time, distance, latlng, altitude, velocity_smooth,
        heartrate, cadence, watts, temp, moving, grade_smooth.

        Returns dict mapping stream type -> {data: [...], original_size, resolution}.
        """
        if keys is None:
            keys = ["time", "distance", "heartrate", "velocity_smooth",
                    "cadence", "altitude"]
        return self._request(f"/activities/{activity_id}/streams", {
            "keys": ",".join(keys),
            "key_by_type": "true",
        })

    def get_activity_laps(self, activity_id: int) -> list:
        """Get auto-detected laps for an activity (1 mile or 1 km splits)."""
        return self._request(f"/activities/{activity_id}/laps")


def quick_test():
    """Probe the API to see what we can access with the current scope."""
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    api = StravaAPI()

    OK = "[OK]"
    FAIL = "[FAIL]"

    print("=" * 60)
    print("STRAVA API CONNECTIVITY TEST")
    print("=" * 60)

    print("\n[1] GET /athlete (basic profile)")
    try:
        athlete = api.get_athlete()
        print(f"  {OK} {athlete.get('firstname')} {athlete.get('lastname')} "
              f"(id={athlete.get('id')})")
        print(f"    City: {athlete.get('city')}, Created: {athlete.get('created_at')}")
    except Exception as e:
        print(f"  {FAIL} {e}")

    print("\n[2] GET /athlete/activities (last 5)")
    acts = []
    try:
        acts = api.list_activities(per_page=5)
        print(f"  {OK} Got {len(acts)} activities")
        for a in acts[:5]:
            print(f"    {a['start_date_local'][:10]} | {a['type']:12s} | "
                  f"{a.get('name', '')[:40]:40s} | "
                  f"{a.get('distance', 0)/1000:.1f}km")
    except Exception as e:
        print(f"  {FAIL} {e}")

    if acts:
        latest_id = acts[0]["id"]
        print(f"\n[3] GET /activities/{latest_id} (detail)")
        try:
            detail = api.get_activity(latest_id)
            print(f"  {OK} Has HR: {detail.get('has_heartrate')}, "
                  f"Laps: {len(detail.get('laps', []) or [])}, "
                  f"Splits metric: {len(detail.get('splits_metric', []) or [])}")
        except Exception as e:
            print(f"  {FAIL} {e}")

        print(f"\n[4] GET /activities/{latest_id}/streams")
        try:
            streams = api.get_activity_streams(latest_id)
            for stream_type, data in streams.items():
                size = data.get("original_size", 0) if isinstance(data, dict) else 0
                print(f"  {OK} {stream_type}: {size} points")
        except Exception as e:
            print(f"  {FAIL} {e}")

        print(f"\n[5] GET /activities/{latest_id}/laps")
        try:
            laps = api.get_activity_laps(latest_id)
            print(f"  {OK} {len(laps)} laps")
            for lap in laps[:3]:
                print(f"    Lap {lap.get('lap_index')}: "
                      f"{lap.get('distance', 0)/1609.34:.2f}mi in "
                      f"{lap.get('moving_time', 0)/60:.1f}min, "
                      f"avg HR {lap.get('average_heartrate', 'N/A')}")
        except Exception as e:
            print(f"  {FAIL} {e}")

    print("\n[6] GET /athlete/zones")
    try:
        zones = api.get_athlete_zones()
        print(f"  {OK} {zones}")
    except Exception as e:
        print(f"  {FAIL} (likely scope): {e}")

    print()


if __name__ == "__main__":
    quick_test()
