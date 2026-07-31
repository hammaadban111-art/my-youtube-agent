"""
Pulls several free, royalty-free stock video clips per segment from Pexels,
matched to that segment's visual_keywords, for a faster multi-cut edit
rather than one static clip held for the whole segment.
"""
import hashlib
import json
import os
import re
import shutil
import requests
from . import config, resilience

# Tried in order when the segment's own keywords return nothing usable. These
# are deliberately bland and always-populated on Pexels, so a niche query
# ("lead masks evidence locker") degrades to generic mood footage rather than
# taking down the whole run.
FALLBACK_QUERIES = ["dark atmospheric background", "fog", "abstract dark"]

PEXELS_SEARCH_URL = "https://api.pexels.com/videos/search"
# Caps clips fetched per segment: a segment can have more shots than this
# (fast dialogue = more cuts), in which case clips repeat across shots
# rather than triggering another search - keeps Pexels search-API calls
# bounded regardless of how choppy the cut plan gets.
MAX_CLIPS_PER_SEGMENT = 3

# The script prompt deliberately steers every segment toward generic,
# commonly-filmed B-roll categories (fog over hills, stormy ocean, candle in
# dark room - see script_writer's visual_keywords rule) rather than literal
# narrative props, specifically because stock libraries don't have the
# specific thing. That means the SAME handful of queries recur constantly
# across videos, not just within the fixed FALLBACK_QUERIES list - so the
# cache keys on any query, not a hardcoded category list. Lives outside
# workdir/ (wiped every run) so it survives across pipeline runs; in CI a
# workflow-level actions/cache step restores/saves this directory so it
# persists across ephemeral runners too.
CACHE_DIR = os.path.join(os.path.dirname(__file__), "..", ".pexels_cache")


def _cache_key(query: str) -> str:
    """Filesystem-safe directory name for a query. Keeps a readable slug
    prefix (for anyone poking around the cache dir) plus a hash suffix so two
    queries that slugify identically (e.g. differ only in punctuation stripped
    by the slug) never collide."""
    normalized = re.sub(r"\s+", " ", query.strip().lower())
    slug = re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")[:60] or "query"
    digest = hashlib.md5(normalized.encode()).hexdigest()[:8]
    return f"{slug}-{digest}"


def _cached_clips(query: str, n: int) -> list[str] | None:
    """Paths to n already-downloaded clips for this query, or None if the
    cache doesn't have enough of them yet (0 is a valid "not cached" case,
    same as any partial count - a fresh fetch repopulates the full set)."""
    clip_dir = os.path.join(CACHE_DIR, _cache_key(query))
    if not os.path.isdir(clip_dir):
        return None
    files = sorted(f for f in os.listdir(clip_dir) if f.endswith(".mp4"))
    if len(files) < n:
        return None
    return [os.path.join(clip_dir, f) for f in files[:n]]


def _store_in_cache(query: str, paths: list[str]) -> None:
    clip_dir = os.path.join(CACHE_DIR, _cache_key(query))
    os.makedirs(clip_dir, exist_ok=True)
    for i, path in enumerate(paths):
        dest = os.path.join(clip_dir, f"clip_{i}.mp4")
        if not os.path.exists(dest):
            shutil.copyfile(path, dest)


def _relevance_score(query: str, video: dict) -> int:
    """Pexels returns no description/tags for most clips — the only text
    metadata available is the uploader's descriptive URL slug (e.g.
    ".../video/arrested-woman-explaining-6125278/"). Score by how many query
    words appear in it, as a weak but real signal beyond raw search order."""
    slug_words = set(re.split(r"[^a-z0-9]+", video.get("url", "").lower()))
    query_words = set(re.split(r"[^a-z0-9]+", query.lower()))
    return len(slug_words & query_words)


def _search_videos(query: str, per_page: int) -> list[dict]:
    headers = {"Authorization": config.PEXELS_API_KEY}
    params = {"query": query, "orientation": "portrait", "per_page": per_page}
    r = requests.get(PEXELS_SEARCH_URL, headers=headers, params=params, timeout=30)
    r.raise_for_status()
    videos = r.json().get("videos", [])
    if not videos:
        raise RuntimeError(f"No Pexels results for '{query}'")
    return sorted(videos, key=lambda v: _relevance_score(query, v), reverse=True)


def _download_video(video: dict, out_path: str) -> str:
    files = sorted(video["video_files"], key=lambda f: f.get("width", 9999))
    candidate = next((f for f in files if f.get("height", 0) >= 720), files[-1])
    with requests.get(candidate["link"], stream=True, timeout=60) as resp:
        resp.raise_for_status()
        with open(out_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1 << 16):
                f.write(chunk)
    return out_path


def fetch_segment_clips(query: str, out_paths: list[str]) -> list[str]:
    """One Pexels search for the whole segment, then the top len(out_paths)
    distinct videos are each downloaded once - this is one search call no
    matter how many clips the segment needs, rather than a separate search
    per clip.

    Checks the on-disk cache first: a hit skips both the search call and the
    download entirely, since this exact (or near-identical) generic query has
    already been fetched by a previous run."""
    cached = _cached_clips(query, len(out_paths))
    if cached is not None:
        for src, dst in zip(cached, out_paths):
            shutil.copyfile(src, dst)
        return out_paths

    videos = _search_videos(query, per_page=max(15, len(out_paths) * 3))
    picked, seen_ids = [], set()
    for v in videos:
        if v["id"] in seen_ids:
            continue
        seen_ids.add(v["id"])
        picked.append(v)
        if len(picked) == len(out_paths):
            break
    # Fewer distinct Pexels results than clips needed (rare, niche query) -
    # cycle back through the best matches rather than erroring out.
    while len(picked) < len(out_paths):
        picked.append(picked[len(picked) % len(picked)])
    result = [_download_video(v, p) for v, p in zip(picked, out_paths)]
    _store_in_cache(query, result)
    return result


def fetch_all(segments: list[dict]) -> list[dict]:
    """Fetches clips for every segment, degrading rather than dying.

    Pexels allows 200 requests/hour; a rate-limited or empty response used to
    abort the run. Now each segment tries, in order: its own keywords (with
    backoff), then generic fallback queries, then reuse of a clip already
    downloaded earlier in this same run. Only a first segment that fails with
    nothing yet cached can still raise, since there is genuinely no footage
    to build a video from at that point.
    """
    already_fetched: list[str] = []

    for i, seg in enumerate(segments):
        shots = seg.get("shots") or [{"start": 0, "duration": seg["duration"]}]
        num_clips = min(len(shots), MAX_CLIPS_PER_SEGMENT)
        out_paths = [f"{config.WORKDIR}/clip_{i}_{c}.mp4" for c in range(num_clips)]
        keywords = seg["visual_keywords"]

        try:
            seg["clip_paths"] = resilience.retry(
                lambda: fetch_segment_clips(keywords, out_paths),
                label=f"pexels segment {i} ({keywords!r})",
                attempts=3, base_delay=4.0,
            )
            already_fetched.extend(seg["clip_paths"])
            continue
        except Exception as primary:  # noqa: BLE001 - handled by fallbacks below
            last_error = primary

        for fallback_query in FALLBACK_QUERIES:
            try:
                seg["clip_paths"] = fetch_segment_clips(fallback_query, out_paths)
                resilience.record_degradation(
                    "pexels",
                    f"segment {i} query {keywords!r} failed: "
                    f"{type(last_error).__name__}: {last_error}",
                    f"used generic query {fallback_query!r}",
                )
                already_fetched.extend(seg["clip_paths"])
                break
            except Exception as e:  # noqa: BLE001 - try the next fallback query
                last_error = e
        else:
            if not already_fetched:
                raise RuntimeError(
                    f"Pexels failed for the first segment with no cached clips "
                    f"to fall back on: {last_error}"
                ) from last_error
            # Copy rather than alias so downstream editing of one segment's
            # clip can never mutate another segment's source file.
            reused = []
            for n, out_path in enumerate(out_paths):
                shutil.copyfile(already_fetched[n % len(already_fetched)], out_path)
                reused.append(out_path)
            seg["clip_paths"] = reused
            resilience.record_degradation(
                "pexels",
                f"segment {i} exhausted all queries: "
                f"{type(last_error).__name__}: {last_error}",
                f"reused {len(reused)} clip(s) already fetched this run",
            )

    return segments


if __name__ == "__main__":
    with open(f"{config.WORKDIR}/segments.json") as f:
        segments = json.load(f)
    segments = fetch_all(segments)
    with open(f"{config.WORKDIR}/segments.json", "w") as f:
        json.dump(segments, f, indent=2)
    print(f"Fetched clips for {len(segments)} segments.")
