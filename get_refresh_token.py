"""
Run this ONCE on your Mac to get a YouTube refresh token.
It opens a browser for you to log into the Google account that owns
your YouTube channel and grant upload permission. After this, the
refresh token lets the agent upload forever with no further logins.

Usage:
    python get_refresh_token.py <path-to-client_secret.json>

Get client_secret.json from Google Cloud Console:
  console.cloud.google.com -> new project -> enable "YouTube Data API v3"
  -> Credentials -> Create OAuth client ID -> Desktop app -> Download JSON
"""
import sys
from google_auth_oauthlib.flow import InstalledAppFlow

# upload: publish videos.  readonly: read back view counts and comments for
# the 5-hour follow-up check (an upload-only token gets 403 there).
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
    # Needed for audience-retention curves in the follow-up job. A token
    # minted before this line was added does NOT have it — re-run this script
    # to grant it, otherwise retention is recorded as unavailable.
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]

if __name__ == "__main__":
    client_secrets_path = sys.argv[1]
    flow = InstalledAppFlow.from_client_secrets_file(client_secrets_path, SCOPES)
    creds = flow.run_local_server(port=0)
    print("\n--- SAVE THESE AS GITHUB SECRETS ---")
    print(f"YT_CLIENT_ID={creds.client_id}")
    print(f"YT_CLIENT_SECRET={creds.client_secret}")
    print(f"YT_REFRESH_TOKEN={creds.refresh_token}")
