#!/usr/bin/env python3
"""
CLI script to export self-contained reuse bundles for video records.
Allows re-rendering videos without any text-model call at all.

Usage:
  python scripts/export_reuse_bundle.py <video_id> [<video_id> ...]
  python scripts/export_reuse_bundle.py --all-below 350 --within-hours 48
"""

import argparse
import glob
import json
import os
import sys
from datetime import datetime, timedelta, timezone

# Ensure project root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from agent.script_writer import BANNED_GENERIC_QUERIES
from agent.store import parse_ts


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_record_from_file(path: str) -> tuple[dict, str]:
    with open(path, "r", encoding="utf-8") as f:
        record = json.load(f)
    return record, path


def find_all_records(base_dir: str = ".") -> list[tuple[dict, str]]:
    video_dir = os.path.join(base_dir, "data", "videos")
    records = []
    pattern = os.path.join(video_dir, "*", "*.json")
    for path in glob.glob(pattern):
        try:
            record, p = load_record_from_file(path)
            records.append((record, p))
        except Exception:
            continue
    return sorted(records, key=lambda r: r[0].get("uploaded_at", ""))


def find_record_by_id(video_id: str, base_dir: str = ".") -> tuple[dict, str] | None:
    if os.path.isfile(video_id):
        return load_record_from_file(video_id)

    video_dir = os.path.join(base_dir, "data", "videos")
    pattern = os.path.join(video_dir, "*", f"{video_id}.json")
    matches = glob.glob(pattern)
    if matches:
        return load_record_from_file(matches[0])

    for root, _, files in os.walk(video_dir):
        for file in files:
            if file == f"{video_id}.json":
                return load_record_from_file(os.path.join(root, file))

    return None


def get_latest_views(record: dict) -> int | None:
    latest_m = record.get("latest_measurement") or {}
    if latest_m.get("actual_views") is not None:
        return latest_m["actual_views"]

    history = record.get("measurement_history") or []
    for entry in reversed(history):
        if isinstance(entry, dict) and entry.get("actual_views") is not None:
            return entry["actual_views"]

    m = record.get("measurement") or {}
    if m.get("actual_views") is not None:
        return m["actual_views"]

    return None


def get_best_views_within_window(record: dict, hours: float) -> int | None:
    uploaded_at_str = record.get("uploaded_at")
    if not uploaded_at_str:
        return None
    try:
        uploaded_dt = parse_ts(uploaded_at_str)
    except Exception:
        return None

    cutoff_dt = uploaded_dt + timedelta(hours=hours)

    history = record.get("measurement_history")
    if history is None:
        m = record.get("measurement") or {}
        if m.get("measured_at") and m.get("actual_views") is not None:
            history = [m]
        else:
            history = []

    views_in_window = []

    def add_if_in_window(entry: dict) -> None:
        if not isinstance(entry, dict):
            return
        measured_at_str = entry.get("measured_at")
        actual_views = entry.get("actual_views")
        if not measured_at_str or actual_views is None:
            return
        try:
            measured_dt = parse_ts(measured_at_str)
        except Exception:
            return
        if uploaded_dt <= measured_dt <= cutoff_dt:
            views_in_window.append(actual_views)

    for entry in history:
        add_if_in_window(entry)

    # Some records have a non-empty lightweight history but no entry in the
    # requested window (for example a migration retained only a later reading).
    # Their frozen/latest measurement is still useful if it has a timestamp in
    # the window; the old ``history is None`` branch skipped that fallback and
    # made a usable low-performing video impossible to export.
    for fallback in (record.get("measurement") or {},
                     record.get("latest_measurement") or {}):
        add_if_in_window(fallback)

    if views_in_window:
        return max(views_in_window)
    return None



def create_reuse_bundle(record: dict, source_record_path: str, base_dir: str = ".") -> dict:
    video_id = record.get("video_id", "")
    title = record.get("title", "")
    topic_subject = record.get("topic_subject", "")
    uploaded_at = record.get("uploaded_at", "")
    niche = record.get("niche", "")

    try:
        rel_source = os.path.relpath(source_record_path, start=base_dir).replace("\\", "/")
    except ValueError:
        rel_source = source_record_path.replace("\\", "/")

    views_at_export = get_latest_views(record)

    script_data = record.get("script", {})
    raw_segments = script_data.get("segments", [])

    bundle_segments = []
    has_legacy_generic = False
    banned_queries_lower = {q.lower() for q in BANNED_GENERIC_QUERIES}

    for seg in raw_segments:
        narration = seg.get("narration", "")
        visual_kw = seg.get("visual_keywords") or seg.get("visual_query") or ""

        if visual_kw.strip().lower() in banned_queries_lower:
            has_legacy_generic = True

        bundle_segments.append({
            "narration": narration,
            "visual_keywords": visual_kw,
            # Rung 2 of agent/visuals.py's footage ladder. Dropped here, every
            # re-render lost it and fell straight from the anchored query to
            # the generic "fog" / "abstract dark" mood footage.
            "visual_fallback": seg.get("visual_fallback") or "",
        })

    segment_count = len(bundle_segments)
    word_count = script_data.get("word_count")
    if word_count is None:
        word_count = sum(len(s["narration"].split()) for s in bundle_segments)

    voice = script_data.get("voice")
    tts_rate = script_data.get("tts_rate")
    target_length_seconds = script_data.get("target_length_seconds")

    grounding = record.get("grounding", {})

    bundle = {
        "schema_version": 1,
        "video_id": video_id,
        "title": title,
        "topic_subject": topic_subject,
        "uploaded_at": uploaded_at,
        "niche": niche,
        "source_record": rel_source,
        "exported_at": iso(_utcnow()),
        "views_at_export": views_at_export,
        "script": {
            "segments": bundle_segments,
            "voice": voice,
            "tts_rate": tts_rate,
            "target_length_seconds": target_length_seconds,
            "segment_count": segment_count,
            "word_count": word_count
        },
        "grounding": grounding,
        "description": None,
        "reuse": {
            "llm_calls_needed": 0,
            # Kept under its old name too: bundles written before
            # 2026-09-08 carry it, and the dashboard reads whichever it finds.
            "gemini_calls_needed": 0,
            "tts_cost": "free (edge-tts)",
            "pexels_downloads_needed": segment_count * 3,
            "missing": [
                "youtube description",
                "pexels clip ids"
            ],
            "visual_queries_are_legacy_generic": has_legacy_generic
        }
    }
    return bundle


def export_bundle_for_record(record: dict, path: str, base_dir: str = ".") -> str:
    bundle = create_reuse_bundle(record, path, base_dir=base_dir)
    video_id = bundle["video_id"]
    out_dir = os.path.join(base_dir, "data", "reuse")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"{video_id}.json")

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(bundle, f, indent=2)

    views_str = str(bundle["views_at_export"]) if bundle["views_at_export"] is not None else "unknown"
    print(f"Bundle exported: {video_id}")
    print(f"  Title: {bundle['title']}")
    print(f"  Views at export: {views_str}")
    print(f"  Segments: {bundle['script']['segment_count']}")
    print(f"  Pexels downloads needed: {bundle['reuse']['pexels_downloads_needed']}")
    if bundle["reuse"]["visual_queries_are_legacy_generic"]:
        print(f"  [WARNING] Legacy generic visual queries detected in script!")

    return out_path


def main():
    parser = argparse.ArgumentParser(description="Export reuse bundle for video records.")
    parser.add_argument("video_ids", nargs="*", help="Video IDs or file paths to export")
    parser.add_argument("--all-below", type=int, default=None, help="Export all videos with best views within H hours below N")
    parser.add_argument("--within-hours", type=float, default=48.0, help="Hours window for --all-below (default: 48)")

    args = parser.parse_args()

    if not args.video_ids and args.all_below is None:
        parser.error("Must specify video_ids or --all-below N")

    records_to_export = []

    if args.video_ids:
        for vid in args.video_ids:
            found = find_record_by_id(vid)
            if found:
                records_to_export.append(found)
            else:
                print(f"Error: record for video_id '{vid}' not found.", file=sys.stderr)

    if args.all_below is not None:
        all_recs = find_all_records()
        for rec, path in all_recs:
            best_v = get_best_views_within_window(rec, args.within_hours)
            if best_v is not None and best_v < args.all_below:
                if not any(r[0].get("video_id") == rec.get("video_id") for r in records_to_export):
                    records_to_export.append((rec, path))

    for rec, path in records_to_export:
        export_bundle_for_record(rec, path)


if __name__ == "__main__":
    main()
