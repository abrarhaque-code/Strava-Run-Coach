# Security policy

## Scope

strava-run-coach is a local CLI: no server, no telemetry, no accounts. The
security surface is small and mostly about your own data staying local:

- Strava credentials live only in the gitignored `.env` (or `STRAVA_*`
  environment variables in headless runs). Nothing in the repo transmits
  them anywhere except `www.strava.com` for OAuth/token refresh.
- Your activity data lives in gitignored files (`activities.csv`,
  `data/strava_cache/`, `data/mcp_*.json`). The generated dashboard is a
  local file; nothing is uploaded.
- The checked-in `.mcp.json` contains only the official Strava MCP endpoint
  URL — no secrets. Authentication is OAuth handled by your Claude client.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting on this repository
(Security → Report a vulnerability). Please do not open a public issue for
anything exploitable.

## If you leaked a token

Revoke it at https://www.strava.com/settings/apps (deauthorize the app) and
rotate the client secret at https://www.strava.com/settings/api. Tokens in a
committed `.env` should be treated as burned even after a force-push.
