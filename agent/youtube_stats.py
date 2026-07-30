"""
Reads view counts and comments back off YouTube for the follow-up check.

Needs the youtube.readonly scope in addition to youtube.upload — an
upload-only token returns 403 "insufficient authentication scopes" here.
Re-run get_refresh_token.py if you see that error.

Quota cost is negligible: videos.list and commentThreads.list are 1 unit
each against a 10,000/day allowance (an upload alone costs 1,600).
"""
from datetime import date, timedelta

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from . import config

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]
# Audience retention lives on a different API (YouTube Analytics, not Data)
# and needs its own scope. Kept separate from SCOPES so a token that predates
# it still works for everything else instead of failing wholesale.
ANALYTICS_SCOPE = "https://www.googleapis.com/auth/yt-analytics.readonly"


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
    return build("youtube", "v3", credentials=_credentials(SCOPES))


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


def _drop_off_analysis(curve: list[dict]) -> dict:
    """Finds where viewers actually leave: the steepest single fall between
    consecutive retention samples, plus the point retention first passes
    below half. Both are expressed as a fraction of video length (0.0-1.0)
    so they're comparable across videos of different durations."""
    if len(curve) < 2:
        return {}
    biggest, drop_at = 0.0, None
    for prev, nxt in zip(curve, curve[1:]):
        delta = prev["watch_ratio"] - nxt["watch_ratio"]
        if delta > biggest:
            biggest, drop_at = delta, nxt["position"]
    below_half = next((p["position"] for p in curve if p["watch_ratio"] < 0.5), None)
    return {
        "biggest_drop_at": drop_at,
        "biggest_drop_size": round(biggest, 4),
        "fell_below_half_at": below_half,
        "retention_at_end": round(curve[-1]["watch_ratio"], 4),
    }


def fetch_retention(video_id: str, uploaded_at_date: str = None) -> dict:
    """Per-position audience retention from the YouTube Analytics API.

    Returns {"available": bool, ...}. When unavailable it records the REASON
    rather than a zero or a guess — retention that silently reads as 0 would
    poison the prediction model far worse than a missing value. Two things
    must both be true for this to work, and neither is true by default:
      1. The YouTube Analytics API must be enabled on the Google Cloud project.
      2. The refresh token must carry yt-analytics.readonly (re-run
         get_refresh_token.py; a token minted before that scope was added
         will fail with invalid_scope).
    """
    try:
        analytics = build(
            "youtubeAnalytics", "v2",
            credentials=_credentials(SCOPES + [ANALYTICS_SCOPE]),
        )
    except Exception as e:  # noqa: BLE001 - reported, never fatal
        return {"available": False, "reason": f"{type(e).__name__}: {e}"[:300]}

    start = uploaded_at_date or (date.today() - timedelta(days=90)).isoformat()
    try:
        response = analytics.reports().query(
            ids="channel==MINE",
            startDate=start,
            endDate=(date.today() + timedelta(days=1)).isoformat(),
            metrics="audienceWatchRatio,relativeRetentionPerformance",
            dimensions="elapsedVideoTimeRatio",
            filters=f"video=={video_id}",
        ).execute()
    except Exception as e:  # noqa: BLE001 - reported, never fatal
        return {"available": False, "reason": f"{type(e).__name__}: {e}"[:300]}

    rows = response.get("rows") or []
    if not rows:
        return {"available": False,
                "reason": "Analytics returned no retention rows yet (too few "
                          "views, or data not processed — usually needs ~24-48h)."}

    curve = [{"position": round(float(r[0]), 4),
              "watch_ratio": round(float(r[1]), 4)} for r in rows]
    curve.sort(key=lambda p: p["position"])
    return {"available": True, "source": "youtube_analytics",
            "granularity": "elapsedVideoTimeRatio",
            "points": len(curve), "curve": curve, **_drop_off_analysis(curve)}


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
