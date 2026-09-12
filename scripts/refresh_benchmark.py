"""
Manual maintenance script to regenerate data/benchmark.json on demand.

data/benchmark.json holds a snapshot of how recent Shorts perform on comparable
niche channels. agent/benchmark.py reads it as a prior for view predictions while
our own channel history is thin.

This script fetches the most recent uploads from a pinned set of niche channels,
filters for Shorts (duration <= 90 seconds), computes view percentiles across
the sample, and updates data/benchmark.json.

Usage:
    python3 scripts/refresh_benchmark.py --dry-run
    python3 scripts/refresh_benchmark.py
"""
import argparse
from datetime import datetime, timezone
import json
import os
import statistics
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

# Pinned YouTube channel IDs to sample from.
#
# WHY PINNED RATHER THAN RE-SEARCHED:
# YouTube API's `search.list` costs quota from a separate 100/day bucket and
# returns different channel results from run to run. Re-searching each time
# would cause our prediction prior to jump around for reasons unrelated to actual
# niche dynamics.
#
# Maintainer: Add YouTube Channel IDs (e.g. "UC...") here if not already present
# in data/benchmark.json.
CHANNEL_IDS: list[str] = [
    # Add target channel IDs here
]

BENCHMARK_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data", "benchmark.json"))
SMALL_CHANNEL_CUTOFF = 1000000
MIN_POOL_SIZE = 100


class BenchmarkRefreshError(RuntimeError):
    """The benchmark cannot be refreshed safely, so leave the prior intact."""


def _load_existing_benchmark() -> dict:
    if not os.path.exists(BENCHMARK_PATH):
        return {}
    try:
        with open(BENCHMARK_PATH) as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        raise BenchmarkRefreshError(
            f"cannot read existing benchmark without risking a destructive rewrite: "
            f"{type(exc).__name__}: {exc}") from exc
    if not isinstance(data, dict):
        raise BenchmarkRefreshError("existing benchmark is not a JSON object")
    schema = data.get("schema_version", 1)
    if isinstance(schema, bool) or not isinstance(schema, int) or schema < 1:
        raise BenchmarkRefreshError("existing benchmark has an invalid schema_version")
    return data


def get_target_channel_ids(existing: dict | None = None) -> list[str]:
    """Combines CHANNEL_IDS constant with channel IDs found in benchmark.json."""
    ids = list(CHANNEL_IDS)
    data = _load_existing_benchmark() if existing is None else existing
    sources = data.get("source_channels", []) if isinstance(data, dict) else []
    if isinstance(sources, list):
        for ch in sources:
            if not isinstance(ch, dict):
                continue
            cid = ch.get("id") or ch.get("channel_id")
            if isinstance(cid, str) and cid and cid not in ids:
                ids.append(cid)
    return ids


def compute_percentiles(values: list[int]) -> dict[str, int]:
    """Computes p1, p5, p10, p25, p50 over a list of view counts."""
    if not values:
        return {"p1": 0, "p5": 0, "p10": 0, "p25": 0, "p50": 0}
    if len(values) == 1:
        v = values[0]
        return {"p1": v, "p5": v, "p10": v, "p25": v, "p50": v}

    sorted_vals = sorted(values)
    q = statistics.quantiles(sorted_vals, n=100, method="inclusive")
    return {
        "p1": int(round(q[0])),
        "p5": int(round(q[4])),
        "p10": int(round(q[9])),
        "p25": int(round(q[24])),
        "p50": int(round(q[49])),
    }


def fetch_channel_shorts(youtube, channel_id: str) -> tuple[dict | None, int]:
    """Fetches subscriber count and up to 50 recent Shorts view counts for a channel.

    Returns (channel_info_dict, api_units_consumed).
    If fetching fails for a channel, logs the error and returns (None, units).
    """
    units = 0
    try:
        # 1. channels.list: get subs, uploads playlist ID, and title (1 unit)
        units += 1
        ch_resp = youtube.channels().list(
            part="snippet,statistics,contentDetails",
            id=channel_id
        ).execute()

        items = ch_resp.get("items", [])
        if not items:
            print(f"[refresh_benchmark] Channel {channel_id} not found.")
            return None, units

        ch_item = items[0]
        title = ch_item.get("snippet", {}).get("title", channel_id)
        stats = ch_item.get("statistics", {})
        subs = int(stats.get("subscriberCount", 0))

        uploads_id = ch_item.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")
        if not uploads_id:
            print(f"[refresh_benchmark] Channel {channel_id} ({title}) missing uploads playlist.")
            return None, units

        # 2. playlistItems.list: get up to 50 recent uploads (1 unit)
        units += 1
        pl_resp = youtube.playlistItems().list(
            part="contentDetails",
            playlistId=uploads_id,
            maxResults=50
        ).execute()

        pl_items = pl_resp.get("items", [])
        video_ids = [
            item["contentDetails"]["videoId"]
            for item in pl_items
            if "contentDetails" in item and "videoId" in item["contentDetails"]
        ]

        if not video_ids:
            return {
                "id": channel_id,
                "title": title,
                "subs": subs,
                "shorts_views": [],
            }, units

        # 3. videos.list: details for those video IDs (1 unit per 50 IDs batch)
        shorts_views = []
        for i in range(0, len(video_ids), 50):
            chunk = video_ids[i:i + 50]
            units += 1
            v_resp = youtube.videos().list(
                part="statistics,contentDetails,snippet",
                id=",".join(chunk)
            ).execute()

            for v_item in v_resp.get("items", []):
                duration_iso = v_item.get("contentDetails", {}).get("duration", "")
                if not duration_iso:
                    continue
                try:
                    dur_seconds = isodate.parse_duration(duration_iso).total_seconds()
                except Exception:
                    continue

                if dur_seconds <= 90:
                    views = int(v_item.get("statistics", {}).get("viewCount", 0))
                    shorts_views.append(views)

        return {
            "id": channel_id,
            "title": title,
            "subs": subs,
            "shorts_views": shorts_views,
        }, units

    except Exception as e:  # noqa: BLE001 - one bad channel must not abort the run
        print(f"[refresh_benchmark] Error fetching channel {channel_id}: {type(e).__name__}: {e}")
        return None, units


def refresh_benchmark(dry_run: bool = False) -> dict:
    existing = _load_existing_benchmark()
    channel_ids = get_target_channel_ids(existing)
    if not channel_ids:
        raise BenchmarkRefreshError(
            "no pinned source channel IDs are configured. Add CHANNEL_IDS or "
            "source_channels entries with an id; refusing to replace the existing prior.")
    api_units = 0
    channels_data = []

    try:
        youtube = build("youtube", "v3", credentials=youtube_stats._credentials(youtube_stats.SCOPES))
        for cid in channel_ids:
            ch_info, units = fetch_channel_shorts(youtube, cid)
            api_units += units
            if ch_info:
                channels_data.append(ch_info)
    except Exception as e:
        raise BenchmarkRefreshError(
            f"could not initialize or query the YouTube API: {type(e).__name__}: {e}") from e
    if not channels_data:
        raise BenchmarkRefreshError(
            "none of the configured benchmark channels returned usable data; "
            "refusing to overwrite the current benchmark.")

    all_shorts = []
    small_shorts = []
    small_channels_data = []

    for ch in channels_data:
        all_shorts.extend(ch["shorts_views"])
        if ch["subs"] <= SMALL_CHANNEL_CUTOFF:
            small_shorts.extend(ch["shorts_views"])
            small_channels_data.append(ch)

    percentiles_all = compute_percentiles(all_shorts)
    percentiles_small = compute_percentiles(small_shorts)

    small_channels_sorted = sorted(small_channels_data, key=lambda c: (c["subs"], c["title"]))
    source_channels = [
        {"id": ch["id"], "title": ch["title"], "subs": ch["subs"]}
        for ch in small_channels_sorted
    ]

    method_info = {
        "found_via": "youtube search.list, queries 'unsolved mysteries shorts' and 'bizarre history shorts', type=video, videoDuration=short, order=viewCount",
        "then": "channels.list for sizes, playlistItems.list + videos.list for each channel's 50 most recent uploads",
        "kept": "uploads of 90 seconds or less",
        "small_channel_cutoff_subs": SMALL_CHANNEL_CUTOFF,
    }
    if isinstance(existing.get("method"), dict):
        method_info = existing["method"]

    today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Start with the existing object so a legacy refresher can never erase the
    # schema-2 category/selection analysis it does not recompute.  Updating a
    # subset is honest; silently downgrading it is data loss.
    benchmark_data = dict(existing)
    benchmark_data.update({
        "schema_version": existing.get("schema_version", 1),
        "captured_at": today_utc,
        "what_this_is": (
            "Floor of the view distribution for recent Shorts (<=90s) from the smallest reachable "
            "channels in this niche. Used as a prior for our own view predictions while our own sample is thin."
        ),
        "method": method_info,
        "sample": {
            **(existing.get("sample") if isinstance(existing.get("sample"), dict) else {}),
            "pooled_shorts": len(all_shorts),
            "channels": len(channels_data),
            "small_channel_shorts": len(small_shorts),
            "small_channels": len(small_channels_data),
        },
        "percentiles_small_channels": percentiles_small,
        "percentiles_all": percentiles_all,
        "prior_views": percentiles_small["p5"],
        "prior_percentile": "p5 of small-channel subset",
        "source_channels": source_channels,
    })

    print("\n--- Benchmark Refresh Summary ---")
    print(f"Channels sampled: {len(channels_data)} ({len(small_channels_data)} small channels <= {SMALL_CHANNEL_CUTOFF:,} subs)")
    print(f"Pooled Shorts: {len(all_shorts)} ({len(small_shorts)} from small channels)")
    print(f"Computed prior (p5 small channels): {percentiles_small['p5']} views")
    print(f"Total API units consumed: {api_units}")
    print("---------------------------------\n")

    json_output = json.dumps(benchmark_data, indent=2)

    if dry_run:
        print("[DRY RUN] Generated data/benchmark.json WOULD be:")
        print(json_output)

    # The prior is p5 of the SMALL subset, so that subset—not the broad pool—
    # is the sample that has to meet the minimum.  Validating all_shorts let a
    # huge-channel pool make a 2-video small-channel prior look legitimate.
    if len(small_shorts) < MIN_POOL_SIZE:
        raise BenchmarkRefreshError(
            f"refusing to overwrite {BENCHMARK_PATH}: small-channel Shorts "
            f"count ({len(small_shorts)}) is fewer than the required "
            f"{MIN_POOL_SIZE}. The p5 prior would be unsupported.")

    if not dry_run:
        with open(BENCHMARK_PATH, "w") as f:
            f.write(json_output + "\n")
        print(f"[refresh_benchmark] Wrote updated benchmark to {BENCHMARK_PATH}")

    return benchmark_data


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Refresh data/benchmark.json from pinned YouTube channels.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch data and print summary/JSON, but do not write data/benchmark.json.",
    )
    args = parser.parse_args()
    try:
        refresh_benchmark(dry_run=args.dry_run)
    except BenchmarkRefreshError as exc:
        print(f"[refresh_benchmark] {exc}", file=sys.stderr)
        sys.exit(1)
