"""Tests for which videos are eligible for a fresh reading.

The tiered recheck cadence this file used to test (every 3h under 48h old,
daily after) was removed 2026-08-02: every eligible video is now refreshed
on every run, so every dashboard card reflects the latest known numbers
every time the site rebuilds, rather than numbers staggered by whichever
recheck interval each video happened to be on. The only remaining gate is
the 5h first-measurement floor.
"""
from datetime import datetime, timedelta, timezone

from agent import store


def _make_record(
    video_id: str,
    uploaded_at: datetime,
    history_times: list[datetime] = None,
) -> dict:
    history = []
    if history_times:
        for i, t in enumerate(history_times):
            history.append({
                "measured_at": store.iso(t),
                "actual_views": (i + 1) * 100,
            })

    first = history[0] if history else {}
    latest = history[-1] if history else {}

    return {
        "video_id": video_id,
        "uploaded_at": store.iso(uploaded_at),
        "measurement": {
            "measured_at": first.get("measured_at"),
            "actual_views": first.get("actual_views"),
        },
        "latest_measurement": {
            "measured_at": latest.get("measured_at"),
            "actual_views": latest.get("actual_views"),
        },
        "measurement_history": history,
    }


def test_just_measured_video_is_still_eligible(monkeypatch):
    """A 10h-old video measured 1 minute ago is still eligible - there is no
    recheck interval to wait out anymore."""
    now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    rec = _make_record(
        "v1",
        uploaded_at=now - timedelta(hours=10),
        history_times=[now - timedelta(minutes=1)],
    )
    monkeypatch.setattr(store, "all_records", lambda: [rec])

    due = store.measurable_records(now)
    assert [r["video_id"] for r in due] == ["v1"]


def test_old_video_just_measured_is_still_eligible(monkeypatch):
    """A 5-day-old video measured 1 minute ago is still eligible too - old
    videos no longer slow down to a daily cadence."""
    now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    rec = _make_record(
        "v2",
        uploaded_at=now - timedelta(days=5),
        history_times=[now - timedelta(minutes=1)],
    )
    monkeypatch.setattr(store, "all_records", lambda: [rec])

    due = store.measurable_records(now)
    assert [r["video_id"] for r in due] == ["v2"]


def test_unmeasured_video_past_first_reading_age_is_eligible(monkeypatch):
    """A video with no readings at all, past MEASURE_AFTER_HOURS, is eligible -
    however many days old it eventually turns out to be (the no-upper-bound
    rule, unchanged by removing the recheck tiering)."""
    now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    rec = _make_record("v3", uploaded_at=now - timedelta(days=10), history_times=[])
    monkeypatch.setattr(store, "all_records", lambda: [rec])

    due = store.measurable_records(now)
    assert [r["video_id"] for r in due] == ["v3"]


def test_video_below_first_reading_age_is_not_eligible(monkeypatch):
    """The one remaining floor: nothing is measured before MEASURE_AFTER_HOURS."""
    now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    rec = _make_record("v4", uploaded_at=now - timedelta(hours=2), history_times=[])
    monkeypatch.setattr(store, "all_records", lambda: [rec])

    due = store.measurable_records(now)
    assert due == []


def test_every_eligible_video_is_returned_together(monkeypatch):
    """A mix of freshly-measured, staleley-measured, and never-measured videos
    are ALL eligible in the same call - there is no longer a subset that
    "isn't due yet"."""
    now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    fresh = _make_record("fresh", now - timedelta(hours=10), [now - timedelta(minutes=1)])
    stale = _make_record("stale", now - timedelta(days=5), [now - timedelta(hours=25)])
    never = _make_record("never", now - timedelta(days=1), [])
    monkeypatch.setattr(store, "all_records", lambda: [fresh, stale, never])

    due_ids = {r["video_id"] for r in store.measurable_records(now)}
    assert due_ids == {"fresh", "stale", "never"}
