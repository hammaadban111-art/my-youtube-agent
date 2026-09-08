import json
from datetime import datetime, timedelta, timezone

import pytest

from agent import editorial


def _record(subject, views, hook, *, frozen=100, hours=5):
    return {
        "topic_subject": subject,
        "title": f"Title: {subject}",
        "measurement": {
            "actual_views": frozen,
            "hours_after_upload": hours,
        },
        "latest_measurement": {
            "actual_views": views,
            "retention": {
                "available": True,
                "curve": [
                    {"position": 0.10, "watch_ratio": hook},
                    {"position": 0.25, "watch_ratio": hook},
                ],
            },
        },
        "script": {"segments": [{"narration": f"Opening for {subject}"}]},
    }


def _valid_brief(*, generated_at="2026-09-08T00:00:00Z"):
    return {
        "schema_version": editorial.BRIEF_SCHEMA_VERSION,
        "generated_at": generated_at,
        "purpose": "Required measured feedback for the next weekly research session.",
        "methodology": {"latest_views": "Directional mature signal only."},
        "channel_snapshot": {
            "measured_records": 4,
            "usable_records": 4,
            "excluded_no_signal_records": 0,
            "comparable_early_records": 4,
            "hook_retention_records": 4,
            "hook_evidence_records": 4,
            "hook_evidence_minimum": 4,
            "hook_evidence_sufficient": True,
        },
        "editorial_rules": ["Use evidence only for fresh story angles."],
        "strong_mature_examples": [],
        "strong_early_examples": [],
        "strong_hook_examples": [],
        "hook_watch_examples": [],
        "early_performance_watchlist": [],
        "avoid_subjects": ["Already published"],
    }


def test_brief_keeps_throttle_records_out_of_rankings():
    evidence = [
        _record(f"Supporting subject {i}", 700 - i, 0.55, frozen=350 - i)
        for i in range(16)
    ]
    brief = editorial.build_brief(
        [
            _record("Strong subject", 800, 0.9, frozen=500),
            _record("Weak hook", 200, 0.2, frozen=450),
            *evidence,
            _record("High retention, low distribution", 80, 0.99, frozen=20),
            _record("Platform throttle", 10, 0.99, frozen=10),
        ],
        now=datetime(2026, 9, 8, tzinfo=timezone.utc),
        published_subjects=["Already published"],
    )

    assert brief["channel_snapshot"]["usable_records"] == 19
    assert brief["channel_snapshot"]["excluded_no_signal_records"] == 1
    assert brief["channel_snapshot"]["hook_evidence_sufficient"] is True
    hook_examples = brief["strong_hook_examples"] + brief["hook_watch_examples"]
    assert "Strong subject" in [example["subject"] for example in hook_examples]
    assert "High retention, low distribution" not in [
        example["subject"] for example in hook_examples]
    assert all(example["frozen_views"] > editorial.predict.NO_SIGNAL_VIEW_THRESHOLD
               for example in hook_examples)
    assert brief["avoid_subjects"] == ["Already published"]
    assert "Opening for Strong subject" in editorial.markdown(brief)


def test_brief_with_small_hook_cohort_does_not_make_a_hook_rule():
    brief = editorial.build_brief(
        [
            _record("One", 500, 0.99, frozen=500),
            _record("Two", 400, 0.90, frozen=400),
            _record("Three", 300, 0.80, frozen=300),
        ],
        now=datetime(2026, 9, 8, tzinfo=timezone.utc),
    )

    snapshot = brief["channel_snapshot"]
    assert snapshot["hook_evidence_records"] == 1
    assert snapshot["hook_evidence_sufficient"] is False
    assert brief["strong_hook_examples"] == []
    assert brief["hook_watch_examples"] == []
    assert "Not enough distributed early-performance records" in editorial.markdown(brief)


def test_brief_provenance_requires_recent_valid_json(tmp_path):
    now = datetime(2026, 9, 8, tzinfo=timezone.utc)
    path = tmp_path / "brief.json"
    path.write_text(json.dumps(_valid_brief()))

    provenance = editorial.brief_provenance(path, now=now)
    assert provenance["sha256"]
    assert provenance["usable_records"] == 4
    assert provenance["hook_evidence_sufficient"] is True

    with pytest.raises(editorial.EditorialBriefError, match="old"):
        editorial.brief_provenance(path, now=now + timedelta(days=9))


def test_brief_provenance_rejects_a_fresh_placeholder(tmp_path):
    path = tmp_path / "brief.json"
    path.write_text(json.dumps({
        "schema_version": editorial.BRIEF_SCHEMA_VERSION,
        "generated_at": "2026-09-08T00:00:00Z",
        "channel_snapshot": {},
    }))

    with pytest.raises(editorial.EditorialBriefError, match="malformed"):
        editorial.brief_provenance(
            path, now=datetime(2026, 9, 8, tzinfo=timezone.utc))
