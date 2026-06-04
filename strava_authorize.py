"""One-time OAuth re-authorization for Strava.

The default 'read' scope only gives basic profile data. To pull activities,
we need 'activity:read_all'. This script walks through the OAuth flow.

Usage:
    python3 strava_authorize.py

It will:
1. Print an authorization URL — open it in your browser
2. After approving, Strava redirects to http://localhost/ (which fails to load)
   — that's fine. Copy the FULL URL from the address bar.
3. Paste it here.
4. Script exchanges the code for new tokens and updates .env.
"""

import json
import re
import sys
import urllib.parse
import urllib.request

from strava_api import _load_env, _save_env, TOKEN_URL


SCOPES = ["activity:read_all", "profile:read_all", "read"]
REDIRECT_URI = "http://localhost"


def build_authorize_url(client_id: str) -> str:
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "approval_prompt": "force",
        "scope": ",".join(SCOPES),
    }
    return "https://www.strava.com/oauth/authorize?" + urllib.parse.urlencode(params)


def extract_code(redirect_url: str) -> str:
    """Extract the 'code' query param from a redirect URL."""
    parsed = urllib.parse.urlparse(redirect_url.strip())
    qs = urllib.parse.parse_qs(parsed.query)
    if "code" not in qs:
        # Maybe they pasted just the code itself
        if re.match(r"^[a-f0-9]{40,}$", redirect_url.strip()):
            return redirect_url.strip()
        raise ValueError(f"No 'code' param in URL: {redirect_url}")
    return qs["code"][0]


def exchange_code_for_tokens(client_id: str, client_secret: str, code: str) -> dict:
    data = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "grant_type": "authorization_code",
    }).encode("utf-8")

    req = urllib.request.Request(TOKEN_URL, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    env = _load_env()
    client_id = env.get("STRAVA_CLIENT_ID")
    client_secret = env.get("STRAVA_CLIENT_SECRET")
    if not client_id or not client_secret:
        print("ERROR: STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET must be set in .env")
        sys.exit(1)

    auth_url = build_authorize_url(client_id)
    print()
    print("=" * 70)
    print("STRAVA OAUTH RE-AUTHORIZATION")
    print("=" * 70)
    print()
    print("Step 1: Open this URL in your browser:")
    print()
    print(f"  {auth_url}")
    print()
    print("Step 2: Click 'Authorize'.")
    print()
    print("Step 3: You'll be redirected to a URL that fails to load.")
    print("        That's fine. The URL bar will look like:")
    print("        http://localhost/?state=&code=XXXXXX&scope=read,activity:read_all,...")
    print()
    print("Step 4: Copy the ENTIRE URL from the address bar and paste below:")
    print()

    redirect_url = input("Paste URL: ").strip()
    if not redirect_url:
        print("No URL provided. Exiting.")
        sys.exit(1)

    code = extract_code(redirect_url)
    print(f"\n  Extracted code: {code[:10]}...")

    print("  Exchanging code for tokens...")
    tokens = exchange_code_for_tokens(client_id, client_secret, code)

    env["STRAVA_ACCESS_TOKEN"] = tokens["access_token"]
    env["STRAVA_REFRESH_TOKEN"] = tokens["refresh_token"]
    env["STRAVA_TOKEN_EXPIRES_AT"] = str(tokens["expires_at"])
    _save_env(env)

    print()
    print("[OK] Tokens saved to .env")
    print(f"  Access token expires at: {tokens['expires_at']}")
    print(f"  Granted scopes: {tokens.get('athlete', {}).get('id', 'see Strava response')}")
    print()
    print("Now run: python3 strava_api.py  (to verify activities are accessible)")


if __name__ == "__main__":
    main()
