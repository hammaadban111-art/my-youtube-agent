"""
Re-mints the YouTube OAuth refresh token and pushes it straight to the
GitHub secret — never prints the token itself to the terminal.

Why this exists (2026-08-18): refresh tokens minted while the Google Cloud
consent screen was in "Testing" carry a 7-day expiry baked in FOREVER, even
after the screen is later switched to "In production" (2026-08-15). Only a
token minted AFTER the switch is exempt. That is the real cause behind the
'invalid_grant: Token has been expired or revoked' failures on every upload
run since 2026-08-15T16:48Z.

Usage:
    venv/bin/python scripts/remint_yt_token.py

Reads YT_CLIENT_ID / YT_CLIENT_SECRET from .env (already valid, unchanged).
Opens your browser for a Google login + consent screen. On success, writes
the new refresh token straight into the GitHub secret YT_REFRESH_TOKEN via
`gh secret set` and updates the local .env copy. The token value never
touches stdout/stderr.
"""
import os
import subprocess
import sys

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
    "https://www.googleapis.com/auth/youtube.force-ssl",
]

REPO = "hammaadban111-art/my-youtube-agent"


def load_env(path=".env"):
    env = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def main():
    env = load_env()
    client_config = {
        "installed": {
            "client_id": env["YT_CLIENT_ID"],
            "client_secret": env["YT_CLIENT_SECRET"],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }
    flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
    print("Opening your browser — log in with the account that owns the "
          "YouTube channel and click Allow.")
    creds = flow.run_local_server(port=0)
    new_token = creds.refresh_token
    if not new_token:
        print("No refresh_token came back. Google only issues one on first "
              "consent for a given client — if you've authorized this app "
              "before, revoke access at myaccount.google.com/permissions "
              "for this app and re-run.", file=sys.stderr)
        sys.exit(1)

    # gh reads the secret value from stdin when --body is omitted.  Passing a
    # refresh token as a command-line argument exposes it to process listings
    # (and to some shell history/debugging tools) while the command is live.
    result = subprocess.run(
        ["gh", "secret", "set", "YT_REFRESH_TOKEN", "--repo", REPO],
        input=new_token + "\n", capture_output=True, text=True,
    )
    if result.returncode != 0:
        print("gh secret set failed:", result.stderr, file=sys.stderr)
        sys.exit(1)

    # Keep the local .env in sync so local test runs use the live token too.
    with open(".env") as f:
        lines = f.readlines()
    with open(".env", "w") as f:
        for line in lines:
            if line.startswith("YT_REFRESH_TOKEN="):
                f.write(f"YT_REFRESH_TOKEN={new_token}\n")
            else:
                f.write(line)

    print("Done. New refresh token minted after the app went 'In production' "
          "— it will not expire in 7 days. GitHub secret YT_REFRESH_TOKEN "
          "updated, local .env updated. Token value was never printed.")


if __name__ == "__main__":
    main()
