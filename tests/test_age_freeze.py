"""Tests for the 30-day freeze: videos past AGE_FREEZE_DAYS get exactly one
more decision (final refresh if there's quota headroom, else freeze at
last-known numbers) and then drop out of both measurable_records() and
due_for_final_refresh() for good.
"""
from datetime import datetime, timedelta, timezone

from agent import store

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


def _record(video_id: str, age: timedelta, final: bool = False) -> dict:
    return {
        "video_id": video_id,
        "uploaded_at": store.iso(NOW - age),
        "measurement": {"measured_at": store.iso(NOW - age), "actual_views": 100},
        "latest_measurement": {"measured_at": store.iso(NOW - age), "actual_views": 100},
        "measurement_history": [{"measured_at": store.iso(NOW - age), "actual_views": 100}],
        "final": final,
        "finalized_at": None,
    }


def test_video_under_freeze_age_is_measurable(monkeypatch):
    rec = _record("young", timedelta(days=10))
    monkeypatch.setattr(store, "all_records", lambda: [rec])
    assert [r["video_id"] for r in store.measurable_records(NOW)] == ["young"]
    assert store.due_for_final_refresh(NOW) == []


def test_video_at_freeze_age_is_due_for_final_not_measurable(monkeypatch):
    rec = _record("aged-out", timedelta(days=30, hours=1))
    monkeypatch.setattr(store, "all_records", lambda: [rec])
    assert store.measurable_records(NOW) == []
    assert [r["video_id"] for r in store.due_for_final_refresh(NOW)] == ["aged-out"]


def test_already_final_video_appears_nowhere(monkeypatch):
    rec = _record("frozen", timedelta(days=90), final=True)
    monkeypatch.setattr(store, "all_records", lambda: [rec])
    assert store.measurable_records(NOW) == []
    assert store.due_for_final_refresh(NOW) == []


def test_freeze_record_sets_final_fields_and_saves(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "DATA_DIR", str(tmp_path))
    rec = _record("to-freeze", timedelta(days=31))

    store.freeze_record(rec, NOW)

    assert rec["final"] is True
    assert rec["finalized_at"] == store.iso(NOW)

    # Actually saved to disk, not just mutated in memory.
    reloaded = store.all_records()
    assert len(reloaded) == 1
    assert reloaded[0]["video_id"] == "to-freeze"
    assert reloaded[0]["final"] is True


def test_frozen_video_is_excluded_after_a_real_save_roundtrip(monkeypatch, tmp_path):
    """The full loop: freeze a video, then confirm a fresh read of disk
    (not the in-memory dict) still excludes it from both selectors."""
    monkeypatch.setattr(store, "DATA_DIR", str(tmp_path))
    rec = _record("roundtrip", timedelta(days=31))
    store.freeze_record(rec, NOW)

    later = NOW + timedelta(days=5)
    assert store.measurable_records(later) == []
    assert store.due_for_final_refresh(later) == []
