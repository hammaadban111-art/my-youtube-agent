"""
Central config. Change NICHE / channel details here.
All secrets come from environment variables (set as GitHub Actions secrets
in production, or a local .env file when testing on your Mac).
"""
import os

# ---- Content settings ----
NICHE = os.getenv("NICHE", "unsolved mysteries and bizarre history")  # change to your topic
VIDEO_LENGTH_SECONDS = int(os.getenv("VIDEO_LENGTH_SECONDS", "35"))
VOICE = os.getenv("TTS_VOICE", "en-US-GuyNeural")  # any edge-tts voice name
NUM_SCRIPT_SEGMENTS = 5  # roughly one stock clip per segment

# ---- API keys (all free-tier) ----
# No text-model key here any more. Scripts are written ahead of time by a
# Claude Cowork task and committed as content/weekly_story_packet.json
# (agent/packet.py); the pipeline reads that file instead of calling a model,
# so a scheduled run needs no generation credentials at all.
PEXELS_API_KEY = os.getenv("PEXELS_API_KEY")        # pexels.com/api
YT_CLIENT_ID = os.getenv("YT_CLIENT_ID")
YT_CLIENT_SECRET = os.getenv("YT_CLIENT_SECRET")
YT_REFRESH_TOKEN = os.getenv("YT_REFRESH_TOKEN")

# "private" (default, safe) or "public" — controlled via env var / GitHub
# Variable so we never have to hand-edit code to flip live uploads on.
PRIVACY_STATUS = os.getenv("PRIVACY_STATUS", "private")

# ---- Paths ----
WORKDIR = os.path.join(os.path.dirname(__file__), "..", "workdir")
os.makedirs(WORKDIR, exist_ok=True)
