"""Tests for tiered measurement checks based on video age."""
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


def test_fast_tier_due(monkeypatch):
    """A 10h-old video measured 3h ago IS due (fast tier)."""
    now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    rec = _make_record(
        "v1",
        uploaded_at=now - timedelta(hours=10),
        history_times=[now - timedelta(hours=3)],
    )
    monkeypatch.setattr(store, "all_records", lambda: [rec])

    due = store.pending_measurement(now)
    assert len(due) == 1
    assert due[0]["video_id"] == "v1"


def test_fast_tier_not_due(monkeypatch):
    """A 10h-old video measured 1h ago is NOT due."""
    now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    rec = _make_record(
        "v2",
        uploaded_at=now - timedelta(hours=10),
        history_times=[now - timedelta(hours=1)],
    )
    monkeypatch.setattr(store, "all_records", lambda: [rec])

    due = store.pending_measurement(now)
    assert len(due) == 0


def test_slow_tier_not_due(monkeypatch):
    """A 5-day-old video measured 3h ago is NOT due (slow tier)."""
    now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    rec = _make_record(
        "v3",
        uploaded_at=now - timedelta(days=5),
        history_times=[now - timedelta(hours=3)],
    )
    monkeypatch.setattr(store, "all_records", lambda: [rec])

    due = store.pending_measurement(now)
    assert len(due) == 0


def test_slow_tier_due(monkeypatch):
    """A 5-day-old video measured 25h ago IS due."""
    now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    rec = _make_record(
        "v4",
        uploaded_at=now - timedelta(days=5),
        history_times=[now - timedelta(hours=25)],
    )
    monkeypatch.setattr(store, "all_records", lambda: [rec])

    due = store.pending_measurement(now)
    assert len(due) == 1
    assert due[0]["video_id"] == "v4"


def test_no_readings_unbounded_age(monkeypatch):
    """A video with no readings at all and older than MEASURE_AFTER_HOURS is due,
    even if it is many days old (the no-upper-bound rule)."""
    now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    rec = _make_record(
        "v5",
        uploaded_at=now - timedelta(days=10),
        history_times=[],
    )
    monkeypatch.setattr(store, "all_records", lambda: [rec])

    due = store.pending_measurement(now)
    assert len(due) == 1
    assert due[0]["video_id"] == "v5"


def test_max_measurements_reached(monkeypatch):
    """A video that has already hit MAX_MEASUREMENTS readings is never due."""
    now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    times = [now - timedelta(hours=50 - i) for i in range(store.MAX_MEASUREMENTS)]
    rec = _make_record(
        "v6",
        uploaded_at=now - timedelta(days=5),
        history_times=times,
    )
    monkeypatch.setattr(store, "all_records", lambda: [rec])

    due = store.pending_measurement(now)
    assert len(due) == 0


def test_recheck_interval_hours_boundary():
    """recheck_interval_hours() returns 3 just under the 48h boundary and 24 just over it."""
    now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)

    rec_under = _make_record("v_under", uploaded_at=now - timedelta(hours=47, minutes=59))
    assert store.recheck_interval_hours(rec_under, now) == store.FAST_RECHECK_INTERVAL_HOURS

    rec_over = _make_record("v_over", uploaded_at=now - timedelta(hours=48, minutes=1))
    assert store.recheck_interval_hours(rec_over, now) == store.RECHECK_INTERVAL_HOURS


def test_eight_day_old_with_readings_not_due(monkeypatch):
    """An 8-day-old video with prior readings, last measured 25h ago, is NOT due."""
    now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    rec = _make_record(
        "v8d",
        uploaded_at=now - timedelta(days=8),
        history_times=[now - timedelta(hours=25)],
    )
    monkeypatch.setattr(store, "all_records", lambda: [rec])

    due = store.pending_measurement(now)
    assert len(due) == 0


def test_six_day_old_with_readings_is_due(monkeypatch):
    """A 6-day-old video with prior readings, last measured 25h ago, IS due."""
    now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    rec = _make_record(
        "v6d",
        uploaded_at=now - timedelta(days=6),
        history_times=[now - timedelta(hours=25)],
    )
    monkeypatch.setattr(store, "all_records", lambda: [rec])

    due = store.pending_measurement(now)
    assert len(due) == 1
    assert due[0]["video_id"] == "v6d"


def test_thirty_day_old_no_readings_is_due(monkeypatch):
    """A 30-day-old video with NO readings at all IS still due (the exception)."""
    now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    rec = _make_record(
        "v30d_none",
        uploaded_at=now - timedelta(days=30),
        history_times=[],
    )
    monkeypatch.setattr(store, "all_records", lambda: [rec])

    due = store.pending_measurement(now)
    assert len(due) == 1
    assert due[0]["video_id"] == "v30d_none"


def test_follow_up_window_boundary(monkeypatch):
    """A video right at the boundary: 7 days minus 1h old and due by interval IS due;
    7 days plus 1h old is NOT."""
    now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    rec_under = _make_record(
        "v_7d_minus_1h",
        uploaded_at=now - (timedelta(days=7) - timedelta(hours=1)),
        history_times=[now - timedelta(hours=25)],
    )
    rec_over = _make_record(
        "v_7d_plus_1h",
        uploaded_at=now - (timedelta(days=7) + timedelta(hours=1)),
        history_times=[now - timedelta(hours=25)],
    )
    monkeypatch.setattr(store, "all_records", lambda: [rec_under, rec_over])

    due = store.pending_measurement(now)
    due_ids = [r["video_id"] for r in due]
    assert "v_7d_minus_1h" in due_ids
    assert "v_7d_plus_1h" not in due_ids

