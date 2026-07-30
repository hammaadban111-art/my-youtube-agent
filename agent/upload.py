"""
Uploads the finished video to YouTube automatically using a stored
refresh token, so no browser login is needed once set up (see README
for the one-time OAuth step).
"""
import json
import os
import shutil
from datetime import datetime, timezone

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError
from . import config, resilience

# A rendered video represents ~6 minutes of CI time and a Gemini call that
# came out of a 20/day quota. If the upload itself fails, throwing it away
# means paying all of that again — it's parked here for the next run instead.
PENDING_DIR = os.path.join(os.path.dirname(__file__), "..", "workdir", "pending_upload")
# Quota exhaustion (403 quotaExceeded) does not recover within a run; the
# daily YouTube quota resets at midnight Pacific. Retrying it just burns time.
NON_RETRYABLE_STATUS = {400, 401, 404}


def _get_service():
    creds = Credentials(
        token=None,
        refresh_token=config.YT_REFRESH_TOKEN,
        client_id=config.YT_CLIENT_ID,
        client_secret=config.YT_CLIENT_SECRET,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=["https://www.googleapis.com/auth/youtube.upload"],
    )
    return build("youtube", "v3", credentials=creds)


def _is_retryable(error: Exception) -> bool:
    if isinstance(error, HttpError):
        return getattr(error.resp, "status", 500) not in NON_RETRYABLE_STATUS
    return True


def park_for_next_run(video_path: str, title: str, description: str, reason: str) -> str:
    """Saves a rendered-but-unuploaded video plus its metadata so a later run
    can publish it instead of the work being lost."""
    os.makedirs(PENDING_DIR, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    parked_video = os.path.join(PENDING_DIR, f"{stamp}.mp4")
    shutil.copyfile(video_path, parked_video)
    with open(os.path.join(PENDING_DIR, f"{stamp}.json"), "w") as f:
        json.dump({"title": title, "description": description,
                   "video_file": os.path.basename(parked_video),
                   "parked_at": stamp, "reason": reason[:300]}, f, indent=2)
    return parked_video


def upload_video(video_path: str, title: str, description: str, tags: list[str] = None):
    """Uploads with backoff. Raises only after the video has been safely
    parked, so a failed upload costs a slot rather than the whole render."""
    body = {
        "snippet": {
            "title": title[:100],
            "description": description,
            "tags": tags or [],
            "categoryId": "22",
        },
        "status": {"privacyStatus": config.PRIVACY_STATUS, "selfDeclaredMadeForKids": False},
    }

    def _do_upload():
        youtube = _get_service()
        # MediaFileUpload is rebuilt per attempt: a partially-consumed upload
        # object cannot be safely replayed after a failure.
        media = MediaFileUpload(video_path, chunksize=-1, resumable=True,
                                mimetype="video/mp4")
        request = youtube.videos().insert(part="snippet,status", body=body,
                                          media_body=media)
        return request.execute()["id"]

    try:
        return resilience.retry(
            _do_upload, label="youtube upload", attempts=3, base_delay=10.0,
        )
    except Exception as e:  # noqa: BLE001 - park before re-raising
        reason = f"{type(e).__name__}: {e}"
        retryable = _is_retryable(e)
        parked = park_for_next_run(video_path, title, description, reason)
        resilience.record_degradation(
            "youtube-upload",
            f"upload failed after retries ({'transient' if retryable else 'permanent'}): {reason}",
            f"video parked at {os.path.basename(parked)} for a later run",
        )
        raise


if __name__ == "__main__":
    with open(f"{config.WORKDIR}/script.json") as f:
        script = json.load(f)
    video_id = upload_video(
        f"{config.WORKDIR}/final_video.mp4",
        script["title"],
        script["description"],
    )
    print(f"Uploaded: https://youtube.com/watch?v={video_id}")
