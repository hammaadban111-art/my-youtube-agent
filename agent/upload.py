"""
Uploads the finished video to YouTube automatically using a stored
refresh token, so no browser login is needed once set up (see README
for the one-time OAuth step).
"""
import json
import os
import re
import shutil
from datetime import datetime, timezone

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError
from . import config, quota, resilience

import string

STOPWORDS = frozenset({
    "all", "and", "any", "are", "but", "for", "from", "how", "into", "its",
    "not", "off", "of", "out", "per", "that", "the", "this", "via", "was",
    "were", "what", "when", "where", "who", "why", "with", "you", "your",
})


def build_tags(script: dict, niche: str) -> list[str]:
    """Builds a deduped, lowercased list of up to 15 YouTube tags from script metadata
    and niche terms, enforcing YouTube's per-tag (<=100 chars) and total length
    (<=500 chars including separators) limits."""
    if not isinstance(script, dict):
        script = {}
    if not isinstance(niche, str):
        niche = ""

    raw_candidates = []

    # 1. Topic subject (if present)
    topic = script.get("topic_subject")
    if topic and isinstance(topic, str) and topic.strip():
        topic_str = topic.strip()
        match = re.search(r'\s*\(([^)]+)\)\s*$', topic_str)
        if match:
            main_part = topic_str[:match.start()]
            paren_part = match.group(1)
        else:
            main_part = topic_str
            paren_part = None

        cleaned_main = main_part.strip(string.punctuation + " ")
        if cleaned_main:
            raw_candidates.append(cleaned_main)

        if paren_part:
            cleaned_paren = paren_part.strip(string.punctuation + " ")
            if cleaned_paren:
                raw_candidates.append(cleaned_paren)

    # 2. Niche terms split into words
    if niche:
        for word in niche.split():
            cleaned = word.strip(string.punctuation + " ")
            if cleaned:
                raw_candidates.append(cleaned)

    # 3. Hashtags in description (without leading #)
    desc = script.get("description")
    if desc and isinstance(desc, str):
        for raw_ht in re.findall(r'#([^\s#]+)', desc):
            cleaned_ht = raw_ht.lstrip('#').rstrip('.,!?')
            cleaned_ht = cleaned_ht.strip(string.punctuation + " ")
            if cleaned_ht:
                raw_candidates.append(cleaned_ht)

    seen = set()
    tags = []
    current_budget = 0

    for raw in raw_candidates:
        tag = raw.strip().lower()
        if not tag or len(tag) < 3 or len(tag) > 100:
            continue
        if '(' in tag or ')' in tag:
            continue
        if tag in STOPWORDS:
            continue
        if tag in seen:
            continue

        cost = len(tag) if not tags else (len(tag) + 1)
        if current_budget + cost > 500:
            break

        seen.add(tag)
        tags.append(tag)
        current_budget += cost

        if len(tags) == 15:
            break

    return tags


# A rendered video represents ~6 minutes of CI time and a Gemini call that
# came out of a 20/day quota. If the upload itself fails, throwing it away
# means paying all of that again — it's parked here for the next run instead.
PENDING_DIR = os.path.join(os.path.dirname(__file__), "..", "workdir", "pending_upload")
# Quota exhaustion (403 quotaExceeded) does not recover within a run; the
# daily YouTube quota resets at midnight Pacific. Retrying it just burns time.
NON_RETRYABLE_STATUS = {400, 401, 404}

UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
# Deleting a video needs a WRITE scope over the channel, which youtube.upload
# is not - an upload-only token gets 403 "insufficient authentication scopes"
# on videos.delete. Kept separate from UPLOAD_SCOPE, the same way
# youtube_stats.py keeps ANALYTICS_SCOPE separate, so a token minted before
# this line existed still uploads fine instead of failing wholesale.
DELETE_SCOPE = "https://www.googleapis.com/auth/youtube.force-ssl"


def _credentials(scopes: list[str]) -> Credentials:
    return Credentials(
        token=None,
        refresh_token=config.YT_REFRESH_TOKEN,
        client_id=config.YT_CLIENT_ID,
        client_secret=config.YT_CLIENT_SECRET,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=scopes,
    )


def _get_service():
    return build("youtube", "v3", credentials=_credentials([UPLOAD_SCOPE]))


def granted_scopes() -> list[str]:
    """The scopes the stored refresh token was actually granted, read from the
    token endpoint's own response. Asking for a scope in Credentials(...) does
    not grant it - the grant was fixed when the token was minted - so this is
    the only way to find out before spending anything."""
    creds = _credentials([UPLOAD_SCOPE])
    creds.refresh(Request())
    return list(creds.granted_scopes or [])


def can_delete() -> tuple[bool, str]:
    """Whether the stored token can delete a video, and why not if it can't.
    Checked BEFORE a re-upload renders or uploads anything: finding out after
    the new video is live means 1,600 units spent on a replacement that cannot
    replace anything."""
    try:
        scopes = granted_scopes()
    except Exception as e:  # noqa: BLE001 - a broken refresh is a "no", with the reason
        return False, f"could not refresh the YouTube token: {type(e).__name__}: {e}"
    for scope in (DELETE_SCOPE, "https://www.googleapis.com/auth/youtube"):
        if scope in scopes:
            return True, f"token holds {scope}"
    return False, (
        "the stored refresh token has no delete scope "
        f"({DELETE_SCOPE} or .../auth/youtube). Re-run get_refresh_token.py to "
        "mint one that does, then update the YT_REFRESH_TOKEN secret."
    )


def delete_video(video_id: str) -> None:
    """Deletes one video from the channel. Costs 50 quota units, booked on the
    same ledger as everything else."""
    youtube = build("youtube", "v3", credentials=_credentials([DELETE_SCOPE]))
    try:
        youtube.videos().delete(id=video_id).execute()
    except HttpError:
        # The request reached the API, so the units are spent whatever the
        # verdict was. Booking them only on success would let a run of 403s
        # spend real quota the ledger never sees.
        quota.record_units(quota.UNITS_PER_DELETE)
        raise
    quota.record_units(quota.UNITS_PER_DELETE)


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
        try:
            video_id = request.execute()["id"]
        except HttpError:
            # 1,600 units per videos.insert, and an insert that reached the API
            # has spent them whether or not a video came back. Booked per
            # ATTEMPT, unkeyed, because three failed attempts really do cost
            # three times - only the successful one is keyed by video id below,
            # where idempotency matters.
            quota.record_units(quota.UNITS_PER_UPLOAD)
            raise
        quota.record_upload(video_id)
        return video_id

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
