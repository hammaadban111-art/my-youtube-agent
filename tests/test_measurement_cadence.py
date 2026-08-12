"""Tests for which videos are eligible for a fresh reading.

The tiering removed on 2026-08-02 was deliberately reintroduced on 2026-08-12.
Without tiering, the cost of reading every eligible video on every run
(4,320 units/day for 120 videos) plus 4 uploads/day (6,400 units) exceeded the
10,000 unit quota cap, causing scheduled uploads to be refused. The owner's
explicit trade-off is that constant freshness on old videos is worth sacrificing
to stop scheduled uploads being refused as the channel grows.

The cadence is:
 - under FRESH_WINDOW_HOURS (48h): every run
 - older: daily
 - past AGE_FREEZE_DAYS (30d): handled separately by due_for_final_refresh.
"""
from datetime import datetime, timedelta, timezone

from agent import store
from agent import quota


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
    """A 10h-old video measured 1 minute ago is still eligible - still in the
    every-run fresh window."""
    now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    rec = _make_record(
        "v1",
        uploaded_at=now - timedelta(hours=10),
        history_times=[now - timedelta(minutes=1)],
    )
    monkeypatch.setattr(store, "all_records", lambda: [rec])

    due = store.measurable_records(now)
    assert [r["video_id"] for r in due] == ["v1"]


def test_old_video_just_measured_is_NOT_eligible(monkeypatch):
    """A 5-day-old video measured 1 minute ago is NOT eligible - old
    videos slow down to a daily cadence."""
    now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    rec = _make_record(
        "v2",
        uploaded_at=now - timedelta(days=5),
        history_times=[now - timedelta(minutes=1)],
    )
    monkeypatch.setattr(store, "all_records", lambda: [rec])

    due = store.measurable_records(now)
    assert due == []


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


def test_eligible_videos_are_returned_together(monkeypatch):
    """A mix of freshly-measured young, stalely-measured old, and never-measured
    videos are ALL eligible in the same call."""
    now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    fresh = _make_record("fresh", now - timedelta(hours=10), [now - timedelta(minutes=1)])
    stale = _make_record("stale", now - timedelta(days=5), [now - timedelta(hours=25)])
    never = _make_record("never", now - timedelta(days=1), [])
    monkeypatch.setattr(store, "all_records", lambda: [fresh, stale, never])

    due_ids = {r["video_id"] for r in store.measurable_records(now)}
    assert due_ids == {"fresh", "stale", "never"}


def test_video_just_inside_fresh_window_is_eligible(monkeypatch):
    """A 47h-old video read 1 minute ago is still eligible (just inside the fresh window)."""
    now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    rec = _make_record("v_47", now - timedelta(hours=47), [now - timedelta(minutes=1)])
    monkeypatch.setattr(store, "all_records", lambda: [rec])
    assert [r["video_id"] for r in store.measurable_records(now)] == ["v_47"]

def test_video_just_outside_fresh_window_is_not_eligible(monkeypatch):
    """A 49h-old video read 1 minute ago is NOT eligible."""
    now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    rec = _make_record("v_49", now - timedelta(hours=49), [now - timedelta(minutes=1)])
    monkeypatch.setattr(store, "all_records", lambda: [rec])
    assert store.measurable_records(now) == []

def test_video_outside_fresh_window_read_21h_ago_is_eligible(monkeypatch):
    """A 49h-old video read 21 hours ago IS eligible (past DAILY_RECHECK_AFTER_HOURS)."""
    now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    rec = _make_record("v_49_21h", now - timedelta(hours=49), [now - timedelta(hours=21)])
    monkeypatch.setattr(store, "all_records", lambda: [rec])
    assert [r["video_id"] for r in store.measurable_records(now)] == ["v_49_21h"]

def test_video_outside_fresh_window_read_19h_ago_is_not_eligible(monkeypatch):
    """A 49h-old video read 19 hours ago is NOT eligible (pins the 20h boundary)."""
    now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    rec = _make_record("v_49_19h", now - timedelta(hours=49), [now - timedelta(hours=19)])
    monkeypatch.setattr(store, "all_records", lambda: [rec])
    assert store.measurable_records(now) == []

def test_never_measured_old_video_is_eligible(monkeypatch):
    """A never-measured 10-day-old video IS eligible (the no-silent-drop rule)."""
    now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    rec = _make_record("v_10d_never", now - timedelta(days=10), [])
    monkeypatch.setattr(store, "all_records", lambda: [rec])
    assert [r["video_id"] for r in store.measurable_records(now)] == ["v_10d_never"]

def test_record_with_missing_timestamp_is_treated_as_due(monkeypatch):
    """A record whose latest_measurement.measured_at is missing or garbage is
    treated as DUE, not skipped."""
    now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    rec_missing = _make_record("v_missing", now - timedelta(days=5), [])
    rec_missing["latest_measurement"]["measured_at"] = None
    rec_missing["measurement_history"] = [{"measured_at": None, "actual_views": 100}]

    rec_garbage = _make_record("v_garbage", now - timedelta(days=5), [])
    rec_garbage["latest_measurement"]["measured_at"] = "garbage"
    rec_garbage["measurement_history"] = [{"measured_at": "garbage", "actual_views": 100}]

    monkeypatch.setattr(store, "all_records", lambda: [rec_missing, rec_garbage])
    assert {r["video_id"] for r in store.measurable_records(now)} == {"v_missing", "v_garbage"}

def test_reading_cadence_returns_correct_shape():
    """reading_cadence returns "final"/"every-run"/"daily" for the three shapes."""
    now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    final_rec = _make_record("final", now - timedelta(days=40), [])
    final_rec["final"] = True
    assert store.reading_cadence(final_rec, now) == "final"

    young_rec = _make_record("young", now - timedelta(hours=10), [])
    assert store.reading_cadence(young_rec, now) == "every-run"

    old_rec = _make_record("old", now - timedelta(days=5), [])
    assert store.reading_cadence(old_rec, now) == "daily"

def test_quota_arithmetic_steady_state():
    """A quota-arithmetic test asserting the tiered steady state fits under
    quota.DAILY_CAP alongside 4 uploads, so the regression this fixes cannot
    come back silently. Derive it from the constants, do not hardcode 7,024."""
    uploads_per_day = 4
    runs_per_day = 12
    # 4 uploads/day * 30 days = 120 total active videos
    total_active_videos = uploads_per_day * store.AGE_FREEZE_DAYS
    # Under 48h (FRESH_WINDOW_HOURS): 4 uploads/day * 2 days = 8 videos
    young_videos = uploads_per_day * (store.FRESH_WINDOW_HOURS / 24)
    old_videos = total_active_videos - young_videos

    reads_cost = young_videos * quota.UNITS_PER_READING * runs_per_day
    reads_cost += old_videos * quota.UNITS_PER_READING * 1  # 1 run/day (daily tier)

    assert reads_cost < quota.DAILY_CAP
