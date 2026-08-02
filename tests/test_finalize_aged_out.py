"""Tests for followup.finalize_aged_out() - the glue between store.py's
30-day freeze selector and quota.py's headroom check. Network calls
(youtube_stats) are mocked; the store/quota file I/O is real (against a
temp dir), so this exercises the actual wiring, not just isolated units.
"""
from datetime import datetime, timedelta, timezone

from agent import followup, quota, store

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


def _aged_out_record(video_id: str) -> dict:
    age = timedelta(days=31)
    return {
        "video_id": video_id,
        "title": "Some aged-out video",
        "uploaded_at": store.iso(NOW - age),
        "prediction": {},
        "measurement": {"measured_at": store.iso(NOW - age), "actual_views": 1000},
        "latest_measurement": {"measured_at": store.iso(NOW - age), "actual_views": 1000},
        "measurement_history": [{"measured_at": store.iso(NOW - age), "actual_views": 1000}],
        "final": False,
        "finalized_at": None,
    }


def _setup(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "DATA_DIR", str(tmp_path / "videos"))
    monkeypatch.setattr(quota, "LEDGER_PATH", str(tmp_path / "quota_ledger.json"))
    monkeypatch.setattr(store, "_utcnow", lambda: NOW)


def test_final_refresh_happens_when_headroom_available(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(followup.youtube_stats, "fetch_stats",
                         lambda vid: {"actual_views": 5000, "likes": 10, "comment_count": 2})
    monkeypatch.setattr(followup.youtube_stats, "fetch_comments", lambda vid, limit=5: [])
    monkeypatch.setattr(followup.youtube_stats, "fetch_retention",
                         lambda vid, uploaded_at_date=None: {"available": False, "reason": "test"})

    rec = _aged_out_record("has-headroom")
    refreshed, frozen_stale = followup.finalize_aged_out([rec])

    assert refreshed == 1
    assert frozen_stale == 0
    assert rec["final"] is True
    assert rec["latest_measurement"]["actual_views"] == 5000  # actually got the fresh reading
    assert quota.units_used_today() == 2  # stats + comments, tracked for real


def test_freezes_without_reading_when_quota_tight(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)

    def boom(*a, **k):
        raise AssertionError("should not fetch when quota headroom is exhausted")
    monkeypatch.setattr(followup.youtube_stats, "fetch_stats", boom)
    monkeypatch.setattr(followup.youtube_stats, "fetch_comments", boom)

    quota.record_units(9500)  # well past the 70% reserve threshold

    rec = _aged_out_record("no-headroom")
    refreshed, frozen_stale = followup.finalize_aged_out([rec])

    assert refreshed == 0
    assert frozen_stale == 1
    assert rec["final"] is True
    # Frozen at whatever was already there - unchanged, no fetch attempted.
    assert rec["latest_measurement"]["actual_views"] == 1000


def test_finalized_video_never_appears_again(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(followup.youtube_stats, "fetch_stats",
                         lambda vid: {"actual_views": 5000, "likes": 10, "comment_count": 2})
    monkeypatch.setattr(followup.youtube_stats, "fetch_comments", lambda vid, limit=5: [])
    monkeypatch.setattr(followup.youtube_stats, "fetch_retention",
                         lambda vid, uploaded_at_date=None: {"available": False, "reason": "test"})

    rec = _aged_out_record("gone-for-good")
    followup.finalize_aged_out([rec])

    later = NOW + timedelta(days=10)
    assert store.due_for_final_refresh(later) == []
    assert store.measurable_records(later) == []
