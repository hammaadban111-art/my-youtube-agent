"""
Reads view counts and comments back off YouTube for the follow-up check.

Needs the youtube.readonly scope in addition to youtube.upload — an
upload-only token returns 403 "insufficient authentication scopes" here.
Re-run get_refresh_token.py if you see that error.

Quota cost is negligible: videos.list and commentThreads.list are 1 unit each
against a 10,000 units/day allowance, so one reading of one video costs 2.

Note the old "an upload costs 1,600 units" figure is no longer true and was
removed: Google split videos.insert into its OWN bucket of 100 calls/day, which
no longer draws on the 10,000. The two are now independent budgets, so
measuring more often cannot starve uploading, and this file's cost should be
judged against the full 10,000 rather than against what is left after an
upload. Verified 2026-08-01 against
https://developers.google.com/youtube/v3/guides/quota_and_compliance_audits

fetch_retention() goes to the YouTube Analytics API, which is a SEPARATE API
with its own quota again (1 unit per reports.query request).
"""
import json
import os
from datetime import date, datetime, timedelta, timezone

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from . import config, quota

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]
# Audience retention lives on a different API (YouTube Analytics, not Data)
# and needs its own scope. Kept separate from SCOPES so a token that predates
# it still works for everything else instead of failing wholesale.
ANALYTICS_SCOPE = "https://www.googleapis.com/auth/yt-analytics.readonly"
# commentThreads.list under OAuth needs force-ssl; youtube.readonly is NOT
# enough. google-auth sends the requested scope list on every refresh, so the
# access token is narrowed to exactly what was asked for — the stored refresh
# token has held force-ssl since the 2026-08-18 re-mint, but every comment read
# asked for SCOPES only and got `403 insufficientPermissions`, on every video,
# on every run. Verified live 2026-09-22: the same call with this scope
# returned the video's comments. Its own service, so a token without it fails
# only the comment read, which is classified and recorded, never fatal.
COMMENTS_SCOPE = "https://www.googleapis.com/auth/youtube.force-ssl"


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


def _comments_service():
    return build("youtube", "v3", credentials=_credentials([COMMENTS_SCOPE]))


def _execute_data_api(request, units: int = 1):
    """Execute one Data API request and book its quota even if it errors.

    Callers used to remember this themselves.  That left direct readers (most
    importantly the weekly orphan check) showing free API calls in the ledger,
    while followup had to know implementation details such as one video read
    plus one comment read.  Booking at the API boundary keeps the ledger
    conservative and complete.
    """
    try:
        return request.execute()
    finally:
        quota.record_units(units)


def fetch_stats(video_id: str) -> dict:
    youtube = _get_service()
    items = _execute_data_api(
        youtube.videos().list(part="statistics,snippet", id=video_id)
    ).get("items", [])
    if not items:
        raise RuntimeError(f"Video {video_id} not found (deleted, or wrong account?)")
    stats = items[0].get("statistics", {})
    return {
        "actual_views": int(stats.get("viewCount", 0)),
        "likes": int(stats.get("likeCount", 0)),
        "comment_count": int(stats.get("commentCount", 0)),
    }


def fetch_channel_stats() -> dict:
    """Channel-level totals for the dashboard summary and the growth series.

    1 unit of quota — channels().list is 1 unit same as videos().list, see the
    module docstring. viewCount and videoCount arrive in the SAME `statistics`
    part as subscriberCount, so reading them costs nothing extra; they were
    simply being thrown away.

    Why this matters: until 2026-09-09 the subscriber count was fetched live for
    the dashboard and then discarded, so the channel's single headline growth
    number had no history at all. 66 subscribers was knowable; whether that was
    up or down from last week was not, and no experiment could be judged."""
    youtube = _get_service()
    items = _execute_data_api(
        youtube.channels().list(part="statistics", mine=True)
    ).get("items", [])
    if not items:
        raise RuntimeError("channel not found for the authenticated account")
    stats = items[0].get("statistics", {})

    def _int(key):
        # hiddenSubscriberCount channels omit subscriberCount entirely. That is
        # "not published", not "zero", and writing 0 would be a fabricated
        # reading that the growth series could never distinguish from a real
        # collapse to zero.
        raw = stats.get(key)
        if raw is None:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    return {
        "subscriber_count": _int("subscriberCount"),
        "total_views": _int("viewCount"),
        "total_videos": _int("videoCount"),
        "hidden_subscriber_count": bool(stats.get("hiddenSubscriberCount")),
    }


def fetch_recent_uploads(limit: int = 100) -> list[dict]:
    """Recent channel uploads for reconciliation, newest first.

    This deliberately returns only identity/title/timestamp, not a fabricated
    agent attribution.  Weekly health uses it to surface channel videos that
    have no local record; a human then decides whether each is an orphaned
    agent upload or a deliberate manual upload.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit must be a positive integer")
    youtube = _get_service()
    channel = _execute_data_api(
        youtube.channels().list(part="contentDetails", mine=True)
    )
    items = channel.get("items") or []
    uploads = ((items[0].get("contentDetails") or {}).get("relatedPlaylists") or {}
               ).get("uploads") if items else None
    if not uploads:
        raise RuntimeError("upload playlist not found for the authenticated channel")

    result, token = [], None
    while len(result) < limit:
        page = _execute_data_api(
            youtube.playlistItems().list(
                part="snippet,contentDetails", playlistId=uploads,
                maxResults=min(50, limit - len(result)), pageToken=token,
            )
        )
        for item in page.get("items") or []:
            snippet = item.get("snippet") or {}
            details = item.get("contentDetails") or {}
            video_id = details.get("videoId") or (snippet.get("resourceId") or {}).get("videoId")
            if video_id:
                result.append({
                    "video_id": video_id,
                    "title": str(snippet.get("title") or ""),
                    "published_at": details.get("videoPublishedAt") or snippet.get("publishedAt") or "",
                })
        token = page.get("nextPageToken")
        if not token:
            break
    return result[:limit]


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


# commentThreads.list returns 403 for two completely different situations, and
# the reason string is the only thing that separates them. "commentsDisabled" is
# a real, meaningful answer about the video; anything else at 403 is our
# problem (scope, key, suspended account) and must NOT be recorded as "this
# video has no comments".
_COMMENTS_DISABLED_MARKERS = ("commentsdisabled", "has disabled comments")


def _api_error_reasons(exc: Exception) -> list[str]:
    """The machine-readable `reason` codes out of a googleapiclient HttpError.

    Parsed from the JSON body rather than read out of str(exc), because the
    string form of an HttpError opens with the full request URL. Live proof
    from run 34689042038: 54 videos in one sweep produced

        HttpError: <HttpError 403 when requesting
        https://youtube.googleapis.com/youtube/v3/commentThreads?part=snippet&videoId=

    and the 300-character cap fell before the reason did, so every one of them
    was filed as a failed read when most were simply videos with comments
    turned off. Truncating the URL is fine; truncating the answer is not."""
    body = getattr(exc, "content", None)
    if isinstance(body, bytes):
        try:
            body = body.decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 - a body we cannot decode has no reason
            return []
    if not isinstance(body, str) or not body.strip():
        return []
    try:
        payload = json.loads(body)
    except (ValueError, TypeError):
        return []
    errors = ((payload.get("error") or {}).get("errors") or [])
    reasons = [str(e.get("reason") or "") for e in errors if isinstance(e, dict)]
    return [r for r in reasons if r]


def _comment_failure_reason(exc: Exception) -> tuple[bool, str]:
    """(comments_are_disabled, human reason) for a failed commentThreads call.

    The reason code leads the message so it survives the length cap, and the
    disabled test reads the structured code first and falls back to the text
    only when there is no parseable body."""
    reasons = _api_error_reasons(exc)
    status = getattr(getattr(exc, "resp", None), "status", None)
    text = f"{type(exc).__name__}: {exc}"
    lowered = (" ".join(reasons) + " " + text).lower()
    disabled = any(m in lowered for m in _COMMENTS_DISABLED_MARKERS)
    head = ""
    if status is not None:
        head += f"HTTP {status} "
    if reasons:
        head += f"[{', '.join(reasons)}] "
    return disabled, (head + text)[:300]


def fetch_comments_result(video_id: str, limit: int = 5) -> dict:
    """Top-level comments plus whether the read actually succeeded.

    WHY THIS REPLACED A BARE `except: return []`. The old version turned EVERY
    failure — expired scope, quota exhaustion, a network blip, a suspended
    account — into an empty list, which was then persisted as though the video
    genuinely had no comments. The channel shows 27 comments across 114 videos,
    and there was no way to tell how much of that silence was real and how much
    was swallowed errors. A metric that cannot fail visibly is not a metric.

    Returns {"available": bool, "reason": str|None, "items": [...]}.
    `available` False means WE could not read; it never means "zero comments".
    Comments being disabled is a SUCCESSFUL read — it is a fact about the video,
    not a failure of ours — so it returns available=True with an empty list and
    a reason saying so."""
    youtube = _comments_service()
    try:
        response = _execute_data_api(youtube.commentThreads().list(
            part="snippet", videoId=video_id, maxResults=min(limit * 4, 100),
            order="relevance", textFormat="plainText",
        ))
    except Exception as e:  # noqa: BLE001 - classified, never silently empty
        disabled, reason = _comment_failure_reason(e)
        if disabled:
            return {"available": True, "reason": "comments are disabled on this video",
                    "disabled": True, "items": []}
        return {"available": False, "reason": reason, "items": []}

    return {"available": True, "reason": None, "disabled": False,
            "items": _parse_comment_threads(response, limit)}


def fetch_comments(video_id: str, limit: int = 5) -> list[dict]:
    """Backwards-compatible list view of fetch_comments_result().

    Kept because callers and every existing record store a plain list under
    "comments". New code should prefer fetch_comments_result() so it can tell a
    failed read from an empty one."""
    return fetch_comments_result(video_id, limit)["items"]


def _parse_comment_threads(response: dict, limit: int) -> list[dict]:
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


# ------------------------------------------------- analytics capability probe

# Where the probe's answer is kept. Read before collecting any of these
# metrics; a metric absent from this file, or present with available=false, is
# never collected and never guessed at.
CAPABILITIES_PATH = os.path.join(
    os.path.dirname(__file__), "..", "data", "analytics_capabilities.json")

# Each entry is one real Analytics query. The point is to ASK the API rather
# than to trust documentation or a previous conclusion.
#
# Impressions and impressionClickThroughRate are here because PLAN.md records
# them as "Studio-only, metric does not exist" — tested live, correctly, but
# tested when the channel was ~2 days old and Analytics was returning no rows
# for ANY metric. That is a degenerate condition to conclude from, and
# impressions are the one number that would settle whether this channel's view
# ceiling is a distribution problem or a content problem. So it gets re-asked,
# and whatever the API says is what gets written down.
CAPABILITY_PROBES = {
    "impressions": {"metrics": "impressions"},
    "impressionClickThroughRate": {"metrics": "impressionClickThroughRate"},
    "estimatedMinutesWatched": {"metrics": "estimatedMinutesWatched"},
    "trafficSources": {"metrics": "views",
                       "dimensions": "insightTrafficSourceType"},
    "demographics": {"metrics": "viewerPercentage",
                     "dimensions": "ageGroup,gender"},
}


def probe_analytics_capabilities(write: bool = True, days: int = 30) -> dict:
    """Asks the Analytics API which of CAPABILITY_PROBES it will actually serve.

    Never assumes, never fabricates. Every probe is a real query; a failure is
    recorded with the API's own error text so a later reader can tell "this
    channel cannot have it" from "our token lost a scope". An empty result set
    is recorded as available=false with a reason saying the query worked but
    returned nothing, which is a different and recoverable state.

    Costs one Analytics unit per probe against a separate 100,000/day pool, so
    it is not booked against the 10,000 Data API budget (see agent/quota.py)."""
    now = date.today()
    result = {
        "probed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window_days": days,
        "metrics": {},
    }
    try:
        analytics = build(
            "youtubeAnalytics", "v2",
            credentials=_credentials(SCOPES + [ANALYTICS_SCOPE]),
        )
    except Exception as e:  # noqa: BLE001 - the probe reports, never raises
        reason = f"{type(e).__name__}: {e}"[:300]
        result["metrics"] = {
            name: {"available": False, "reason": reason, "rows": 0}
            for name in CAPABILITY_PROBES
        }
        if write:
            _write_capabilities(result)
        return result

    for name, query in CAPABILITY_PROBES.items():
        entry = {"available": False, "reason": None, "rows": 0}
        try:
            response = analytics.reports().query(
                ids="channel==MINE",
                startDate=(now - timedelta(days=days)).isoformat(),
                endDate=now.isoformat(),
                **query,
            ).execute()
            rows = response.get("rows") or []
            entry["rows"] = len(rows)
            if rows:
                entry["available"] = True
            else:
                entry["reason"] = ("the query is accepted but returned no rows "
                                   "for this window")
        except Exception as e:  # noqa: BLE001 - the answer we came for
            entry["reason"] = f"{type(e).__name__}: {e}"[:300]
        result["metrics"][name] = entry

    if write:
        _write_capabilities(result)
    return result


def _write_capabilities(result: dict) -> None:
    os.makedirs(os.path.dirname(CAPABILITIES_PATH), exist_ok=True)
    tmp = CAPABILITIES_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(result, f, indent=2)
        f.write("\n")
    os.replace(tmp, CAPABILITIES_PATH)


def analytics_capabilities() -> dict:
    """What the last probe found, or an empty result if it has never run.

    Deliberately does NOT probe on demand: a caller asking "can I collect
    impressions" during an upload must not spend an API call finding out."""
    try:
        with open(CAPABILITIES_PATH) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"probed_at": None, "metrics": {}}
    return data if isinstance(data, dict) else {"probed_at": None, "metrics": {}}


def capability_available(name: str) -> bool:
    """Whether `name` was proven available by the last probe. Unknown is False:
    a metric we have never successfully read is not one we collect."""
    entry = (analytics_capabilities().get("metrics") or {}).get(name) or {}
    return bool(entry.get("available"))


if __name__ == "__main__":  # pragma: no cover - operator entry point
    import sys

    if "--probe-capabilities" in sys.argv:
        found = probe_analytics_capabilities()
        print(f"Probed at {found['probed_at']} over {found['window_days']} days:")
        for metric, entry in found["metrics"].items():
            state = "AVAILABLE" if entry["available"] else "unavailable"
            print(f"  {metric:<28} {state:<12} rows={entry['rows']}"
                  + (f"  {entry['reason']}" if entry.get("reason") else ""))
        print(f"\nWritten to {CAPABILITIES_PATH}")
        sys.exit(0)
    print("usage: python -m agent.youtube_stats --probe-capabilities")
    sys.exit(2)
