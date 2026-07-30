"""
Pulls several free, royalty-free stock video clips per segment from Pexels,
matched to that segment's visual_keywords, for a faster multi-cut edit
rather than one static clip held for the whole segment.
"""
import json
import re
import requests
from . import config

PEXELS_SEARCH_URL = "https://api.pexels.com/videos/search"
# Caps clips fetched per segment: a segment can have more shots than this
# (fast dialogue = more cuts), in which case clips repeat across shots
# rather than triggering another search - keeps Pexels search-API calls
# bounded regardless of how choppy the cut plan gets.
MAX_CLIPS_PER_SEGMENT = 3


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
    per clip."""
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
    return [_download_video(v, p) for v, p in zip(picked, out_paths)]


def fetch_all(segments: list[dict]) -> list[dict]:
    for i, seg in enumerate(segments):
        shots = seg.get("shots") or [{"start": 0, "duration": seg["duration"]}]
        num_clips = min(len(shots), MAX_CLIPS_PER_SEGMENT)
        out_paths = [f"{config.WORKDIR}/clip_{i}_{c}.mp4" for c in range(num_clips)]
        seg["clip_paths"] = fetch_segment_clips(seg["visual_keywords"], out_paths)
    return segments


if __name__ == "__main__":
    with open(f"{config.WORKDIR}/segments.json") as f:
        segments = json.load(f)
    segments = fetch_all(segments)
    with open(f"{config.WORKDIR}/segments.json", "w") as f:
        json.dump(segments, f, indent=2)
    print(f"Fetched clips for {len(segments)} segments.")
