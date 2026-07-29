"""
Reads view counts and comments back off YouTube for the follow-up check.

Needs the youtube.readonly scope in addition to youtube.upload — an
upload-only token returns 403 "insufficient authentication scopes" here.
Re-run get_refresh_token.py if you see that error.

Quota cost is negligible: videos.list and commentThreads.list are 1 unit
each against a 10,000/day allowance (an upload alone costs 1,600).
"""
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from . import config

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]


def _get_service():
    creds = Credentials(
        token=None,
        refresh_token=config.YT_REFRESH_TOKEN,
        client_id=config.YT_CLIENT_ID,
        client_secret=config.YT_CLIENT_SECRET,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=SCOPES,
    )
    return build("youtube", "v3", credentials=creds)


def fetch_stats(video_id: str) -> dict:
    youtube = _get_service()
    items = youtube.videos().list(part="statistics,snippet", id=video_id).execute().get("items", [])
    if not items:
        raise RuntimeError(f"Video {video_id} not found (deleted, or wrong account?)")
    stats = items[0].get("statistics", {})
    return {
        "actual_views": int(stats.get("viewCount", 0)),
        "likes": int(stats.get("likeCount", 0)),
        "comment_count": int(stats.get("commentCount", 0)),
    }


def fetch_comments(video_id: str, limit: int = 5) -> list[dict]:
    """Top-level comments, most-liked first. Comments being disabled or empty
    is normal and returns [] rather than raising."""
    youtube = _get_service()
    try:
        response = youtube.commentThreads().list(
            part="snippet", videoId=video_id, maxResults=min(limit * 4, 100),
            order="relevance", textFormat="plainText",
        ).execute()
    except Exception:  # noqa: BLE001 - comments disabled / none yet
        return []

    comments = []
    for item in response.get("items", []):
        top = item["snippet"]["topLevelComment"]["snippet"]
        comments.append({
            "author": top.get("authorDisplayName", ""),
            "text": (top.get("textDisplay", "") or "").strip(),
            "likes": int(top.get("likeCount", 0)),
            "published_at": top.get("publishedAt", ""),
        })
    comments.sort(key=lambda c: c["likes"], reverse=True)
    return comments[:limit]
