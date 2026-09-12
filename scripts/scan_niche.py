"""
Recurring niche scan runner.

Fetches recent Shorts across a representative set of niche search queries to sample
current video performance, subscriber counts, and engagement metrics across the niche.
"""
import argparse
from datetime import datetime, timedelta, timezone
import json
import os
import sys


def _load_env() -> None:
    """Loads environment variables from local .env file if present."""
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip("'\""))


_load_env()

# Ensure repository root is in sys.path so 'agent' package imports work cleanly
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import isodate  # noqa: E402
from googleapiclient.discovery import build  # noqa: E402
from agent import youtube_stats  # noqa: E402

# These queries are chosen to span content TYPES within the niche so the
# sample is not dominated by one phrasing, and channel size is deliberately
# NOT part of the selection.
QUERIES: list[str] = [
    "unsolved disappearance shorts",
    "unsolved murder case shorts",
    "unexplained phenomenon shorts",
    "bizarre history shorts",
    "strange historical event shorts",
    "creepy mystery shorts",
    "cold case shorts",
    "weird science mystery shorts",
]

NICHE_SCAN_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "data", "niche_scan.json")
)
MIN_KEPT_SHORTS = 200
# Search results can surface a viral Short from years ago. A weekly benchmark
# that calls those "current competitors" trains the next packet on stale
# distribution conditions, so every scan is explicitly recent.
RECENCY_DAYS = 90


def _published_since(value: object, cutoff: datetime) -> bool:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc) >= cutoff


def run_niche_scan(dry_run: bool = False) -> dict:
    youtube = build(
        "youtube",
        "v3",
        credentials=youtube_stats._credentials(youtube_stats.SCOPES),
    )

    search_calls = 0
    deduped_video_ids: set[str] = set()
    cutoff = datetime.now(timezone.utc) - timedelta(days=RECENCY_DAYS)
    published_after = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")

    # 1. For each query run search.list TWICE (relevance and viewCount)
    for q in QUERIES:
        for order_val in ["relevance", "viewCount"]:
            try:
                search_calls += 1
                resp = (
                    youtube.search()
                    .list(
                        part="snippet",
                        type="video",
                        videoDuration="short",
                        maxResults=50,
                        relevanceLanguage="en",
                        publishedAfter=published_after,
                        q=q,
                        order=order_val,
                    )
                    .execute()
                )
                for item in resp.get("items", []):
                    vid = item.get("id", {}).get("videoId")
                    if vid:
                        deduped_video_ids.add(vid)
            except Exception as e:  # noqa: BLE001 - tolerate failing query
                print(
                    f"[scan_niche] Error running search.list for query '{q}' "
                    f"(order={order_val}): {type(e).__name__}: {e}"
                )

    all_video_ids = sorted(list(deduped_video_ids))
    total_found_videos = len(all_video_ids)

    # 2. videos.list in batches of 50 for every deduped id. Keep duration <= 90s.
    api_units = 0
    raw_kept_videos: list[dict] = []

    for i in range(0, len(all_video_ids), 50):
        chunk = all_video_ids[i : i + 50]
        api_units += 1
        try:
            v_resp = (
                youtube.videos()
                .list(
                    part="statistics,contentDetails,snippet",
                    id=",".join(chunk),
                )
                .execute()
            )
            for v_item in v_resp.get("items", []):
                duration_iso = (
                    v_item.get("contentDetails", {}).get("duration", "")
                )
                if not duration_iso:
                    continue
                try:
                    dur_seconds = isodate.parse_duration(
                        duration_iso
                    ).total_seconds()
                except Exception:
                    continue

                if dur_seconds <= 90 and _published_since(
                        (v_item.get("snippet") or {}).get("publishedAt"), cutoff):
                    vid = v_item.get("id", "")
                    snippet = v_item.get("snippet", {})
                    stats = v_item.get("statistics", {})
                    raw_kept_videos.append(
                        {
                            "video_id": vid,
                            "title": snippet.get("title", ""),
                            "channel_id": snippet.get("channelId", ""),
                            "channel_title": snippet.get("channelTitle", ""),
                            # Never turn a failed channels.list request into a
                            # fake zero-subscriber channel: zero is a strong
                            # ranking signal, not an "unknown" placeholder.
                            "subs": None,
                            "views": int(stats.get("viewCount", 0)),
                            "likes": int(stats.get("likeCount", 0)),
                            "comments": int(stats.get("commentCount", 0)),
                            "duration_seconds": int(dur_seconds),
                            "published_at": snippet.get("publishedAt", ""),
                        }
                    )
        except Exception as e:  # noqa: BLE001 - tolerate failing batch
            print(
                f"[scan_niche] Error fetching videos.list batch: "
                f"{type(e).__name__}: {e}"
            )

    # 3. channels.list in batches of 50 for every distinct channel id
    distinct_channel_ids = sorted(
        list(
            {
                v["channel_id"]
                for v in raw_kept_videos
                if v.get("channel_id")
            }
        )
    )
    total_channels = len(distinct_channel_ids)
    channel_subs: dict[str, int] = {}

    for i in range(0, len(distinct_channel_ids), 50):
        chunk = distinct_channel_ids[i : i + 50]
        api_units += 1
        try:
            ch_resp = (
                youtube.channels()
                .list(
                    part="statistics",
                    id=",".join(chunk),
                )
                .execute()
            )
            for ch_item in ch_resp.get("items", []):
                cid = ch_item.get("id")
                stats = ch_item.get("statistics", {})
                subs = int(stats.get("subscriberCount", 0))
                if cid:
                    channel_subs[cid] = subs
        except Exception as e:  # noqa: BLE001 - tolerate failing batch
            print(
                f"[scan_niche] Error fetching channels.list batch: "
                f"{type(e).__name__}: {e}"
            )

    # Attach verified subscriber counts only. Unknown-channel rows are omitted
    # from the benchmark rather than made to look like tiny channels.
    for v in raw_kept_videos:
        v["subs"] = channel_subs.get(v["channel_id"])

    unresolved_channels = sorted(set(distinct_channel_ids) - set(channel_subs))
    kept_videos = [v for v in raw_kept_videos
                   if v.get("channel_id") and v.get("subs") is not None]

    # Sort videos by video_id so reruns produce a stable diff
    kept_videos.sort(key=lambda x: x["video_id"])

    scanned_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    scan_result = {
        "schema_version": 1,
        "scanned_at": scanned_at,
        "queries": QUERIES,
        "counts": {
            "videos": total_found_videos,
            "channels": total_channels,
            "kept_shorts": len(kept_videos),
            "unresolved_channels": len(unresolved_channels),
        },
        "quota": {
            "search_calls": search_calls,
            "units": api_units,
        },
        "recency_days": RECENCY_DAYS,
        "published_after": published_after,
        "unresolved_channel_ids": unresolved_channels,
        "videos": kept_videos,
    }

    print("\n--- Niche Scan Summary ---")
    print(f"Queries run: {len(QUERIES)}")
    print(f"Total deduped videos found: {total_found_videos}")
    print(f"Kept recent Shorts (<= 90s, verified channel stats): {len(kept_videos)}")
    print(f"Distinct channels: {total_channels}")
    print("Quota consumed:")
    print(f"  search.list calls: {search_calls} (of 100 calls/day bucket)")
    print(f"  API units: {api_units} (of 10,000 units/day bucket)")
    print("--------------------------\n")

    if len(kept_videos) < MIN_KEPT_SHORTS:
        raise RuntimeError(
            f"Refusing to overwrite {NICHE_SCAN_PATH}: only {len(kept_videos)} "
            f"recent Shorts with verified channel stats (minimum {MIN_KEPT_SHORTS}). "
            "A thin or partial scan is not a benchmark refresh.")

    if dry_run:
        print(
            f"[scan_niche] [DRY RUN] Complete. Would write {NICHE_SCAN_PATH} "
            f"with {len(kept_videos)} kept Shorts."
        )
        return scan_result

    with open(NICHE_SCAN_PATH, "w") as f:
        json.dump(scan_result, f, indent=2)
        f.write("\n")

    print(f"[scan_niche] Wrote niche scan data to {NICHE_SCAN_PATH}")
    return scan_result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Run periodic niche scan and save to data/niche_scan.json."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Perform API queries and print summary, but do not write output file.",
    )
    args = parser.parse_args()
    run_niche_scan(dry_run=args.dry_run)
