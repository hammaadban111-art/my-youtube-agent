"""
Pulls one free, royalty-free stock video clip per segment from Pexels,
matched to that segment's visual_keywords.
"""
import json
import re
import requests
from . import config

PEXELS_SEARCH_URL = "https://api.pexels.com/videos/search"


def _relevance_score(query: str, video: dict) -> int:
    """Pexels returns no description/tags for most clips — the only text
    metadata available is the uploader's descriptive URL slug (e.g.
    ".../video/arrested-woman-explaining-6125278/"). Score by how many query
    words appear in it, as a weak but real signal beyond raw search order."""
    slug_words = set(re.split(r"[^a-z0-9]+", video.get("url", "").lower()))
    query_words = set(re.split(r"[^a-z0-9]+", query.lower()))
    return len(slug_words & query_words)


def fetch_clip(query: str, out_path: str, min_duration: int = 4):
    headers = {"Authorization": config.PEXELS_API_KEY}
    params = {"query": query, "orientation": "portrait", "per_page": 15}
    r = requests.get(PEXELS_SEARCH_URL, headers=headers, params=params, timeout=30)
    r.raise_for_status()
    videos = r.json().get("videos", [])
    if not videos:
        raise RuntimeError(f"No Pexels results for '{query}'")

    # Rank by relevance score (falls back to Pexels' own order on ties, so
    # if nothing scores above 0 we still land on videos[0] as before).
    video = max(videos, key=lambda v: _relevance_score(query, v))
    files = sorted(video["video_files"], key=lambda f: f.get("width", 9999))
    candidate = next((f for f in files if f.get("height", 0) >= 720), files[-1])

    with requests.get(candidate["link"], stream=True, timeout=60) as resp:
        resp.raise_for_status()
        with open(out_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1 << 16):
                f.write(chunk)
    return out_path


def fetch_all(segments: list[dict]) -> list[dict]:
    for i, seg in enumerate(segments):
        out_path = f"{config.WORKDIR}/clip_{i}.mp4"
        fetch_clip(seg["visual_keywords"], out_path)
        seg["clip_path"] = out_path
    return segments


if __name__ == "__main__":
    with open(f"{config.WORKDIR}/segments.json") as f:
        segments = json.load(f)
    segments = fetch_all(segments)
    with open(f"{config.WORKDIR}/segments.json", "w") as f:
        json.dump(segments, f, indent=2)
    print(f"Fetched {len(segments)} clips.")
