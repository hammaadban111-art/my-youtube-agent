import json
import os
import sys
import pytest
from datetime import datetime, timezone, timedelta

from agent.script_writer import BANNED_GENERIC_QUERIES
from scripts.export_reuse_bundle import (
    create_reuse_bundle,
    export_bundle_for_record,
    find_all_records,
    get_best_views_within_window,
    main as cli_main,
)


@pytest.fixture
def fake_record():
    return {
        "schema_version": 1,
        "video_id": "test_vid_123",
        "title": "Secrets of the Deep Ocean",
        "topic_subject": "Deep Ocean",
        "uploaded_at": "2026-08-01T12:00:00Z",
        "niche": "unsolved mysteries and bizarre history",
        "latest_measurement": {
            "measured_at": "2026-08-02T12:00:00Z",
            "actual_views": 150
        },
        "measurement_history": [
            {"measured_at": "2026-08-01T18:00:00Z", "actual_views": 100},
            {"measured_at": "2026-08-02T12:00:00Z", "actual_views": 150}
        ],
        "script": {
            "segments": [
                {
                    "narration": "First line of the narration.",
                    "visual_query": "1960s ocean exploration vessel"
                },
                {
                    "narration": "Second line of the narration.",
                    "visual_query": "underwater submarine lights"
                }
            ],
            "voice": "en-US-GuyNeural",
            "tts_rate": "+8%",
            "target_length_seconds": 35,
            "segment_count": 2,
            "word_count": 10
        },
        "grounding": {
            "status": "checked",
            "source": "wikipedia"
        }
    }


def test_bundle_structure_and_keys(fake_record):
    bundle = create_reuse_bundle(fake_record, "data/videos/2026-08/test_vid_123.json")
    
    required_top_keys = [
        "schema_version", "video_id", "title", "topic_subject", "uploaded_at",
        "niche", "source_record", "exported_at", "views_at_export", "script",
        "grounding", "description", "reuse"
    ]
    for key in required_top_keys:
        assert key in bundle, f"Missing required top-level key: {key}"

    assert bundle["schema_version"] == 1
    assert bundle["video_id"] == "test_vid_123"
    assert bundle["title"] == "Secrets of the Deep Ocean"
    assert bundle["source_record"] == "data/videos/2026-08/test_vid_123.json"
    assert bundle["views_at_export"] == 150

    # Script section check
    script = bundle["script"]
    for s_key in ["segments", "voice", "tts_rate", "target_length_seconds", "segment_count", "word_count"]:
        assert s_key in script, f"Missing script key: {s_key}"

    assert script["segment_count"] == 2
    assert len(script["segments"]) == 2
    assert script["segments"][0]["visual_keywords"] == "1960s ocean exploration vessel"


def test_gemini_calls_needed_is_zero(fake_record):
    bundle = create_reuse_bundle(fake_record, "data/videos/2026-08/test_vid_123.json")
    assert bundle["reuse"]["gemini_calls_needed"] == 0


def test_pexels_downloads_needed(fake_record):
    bundle = create_reuse_bundle(fake_record, "data/videos/2026-08/test_vid_123.json")
    # 2 segments * 3 = 6
    assert bundle["reuse"]["pexels_downloads_needed"] == 6


def test_description_is_none_and_in_missing(fake_record):
    bundle = create_reuse_bundle(fake_record, "data/videos/2026-08/test_vid_123.json")
    assert bundle["description"] is None
    assert "youtube description" in bundle["reuse"]["missing"]
    assert "pexels clip ids" in bundle["reuse"]["missing"]


def test_visual_queries_legacy_generic(fake_record):
    # Test specific query -> False
    bundle_specific = create_reuse_bundle(fake_record, "data/videos/2026-08/test_vid_123.json")
    assert bundle_specific["reuse"]["visual_queries_are_legacy_generic"] is False

    # Test banned generic query -> True
    banned_sample = list(BANNED_GENERIC_QUERIES)[0]
    fake_record["script"]["segments"][0]["visual_query"] = banned_sample
    bundle_generic = create_reuse_bundle(fake_record, "data/videos/2026-08/test_vid_123.json")
    assert bundle_generic["reuse"]["visual_queries_are_legacy_generic"] is True


def test_all_below_selector(tmp_path, monkeypatch, fake_record):
    monkeypatch.chdir(tmp_path)
    os.makedirs("data/videos/2026-08", exist_ok=True)

    # Low view record (best view within 48h = 200)
    low_rec = dict(fake_record)
    low_rec["video_id"] = "low_views_vid"
    low_rec["uploaded_at"] = "2026-08-01T00:00:00Z"
    low_rec["measurement_history"] = [
        {"measured_at": "2026-08-01T12:00:00Z", "actual_views": 150},
        {"measured_at": "2026-08-02T12:00:00Z", "actual_views": 200}
    ]
    with open("data/videos/2026-08/low_views_vid.json", "w") as f:
        json.dump(low_rec, f)

    # High view record (best view within 48h = 500)
    high_rec = dict(fake_record)
    high_rec["video_id"] = "high_views_vid"
    high_rec["uploaded_at"] = "2026-08-01T00:00:00Z"
    high_rec["measurement_history"] = [
        {"measured_at": "2026-08-01T12:00:00Z", "actual_views": 400},
        {"measured_at": "2026-08-02T12:00:00Z", "actual_views": 500}
    ]
    with open("data/videos/2026-08/high_views_vid.json", "w") as f:
        json.dump(high_rec, f)

    monkeypatch.setattr(sys, "argv", ["export_reuse_bundle.py", "--all-below", "350", "--within-hours", "48"])
    cli_main()

    reuse_dir = tmp_path / "data" / "reuse"
    assert (reuse_dir / "low_views_vid.json").exists()
    assert not (reuse_dir / "high_views_vid.json").exists()


def test_missing_measurement_history_does_not_crash():
    record_no_history = {
        "video_id": "no_history_vid",
        "uploaded_at": "2026-08-01T00:00:00Z",
        "title": "No History Video",
        "script": {"segments": []}
    }
    # Check that selector helper returns None without crashing
    best_v = get_best_views_within_window(record_no_history, 48)
    assert best_v is None

    # Check bundle creation works without crashing
    bundle = create_reuse_bundle(record_no_history, "data/videos/2026-08/no_history_vid.json")
    assert bundle["video_id"] == "no_history_vid"
    assert bundle["views_at_export"] is None
