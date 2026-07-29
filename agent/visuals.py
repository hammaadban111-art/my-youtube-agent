"""
Pulls one free, royalty-free stock video clip per segment from Pexels,
matched to that segment's visual_keywords.
"""
import json
import requests
from . import config

PEXELS_SEARCH_URL = "https://api.pexels.com/videos/search"


def fetch_clip(query: str, out_path: str, min_duration: int = 4):
    headers = {"Authorization": config.PEXELS_API_KEY}
    params = {"query": query, "orientation": "portrait", "per_page": 5}
    r = requests.get(PEXELS_SEARCH_URL, headers=headers, params=params, timeout=30)
    r.raise_for_status()
    videos = r.json().get("videos", [])
    if not videos:
        raise RuntimeError(f"No Pexels results for '{query}'")

    # Pick a reasonably small portrait file to keep downloads fast
    video = videos[0]
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
