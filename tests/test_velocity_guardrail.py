"""Guards the upload-velocity ceiling added in Phase 1.

Real context: on 2026-07-30 this channel published 10 videos inside 24h
(several from manual dispatches, two 42 minutes apart) and its distribution
collapsed from ~1,000 views per video to 0-16. Nothing in the pipeline could
notice, because each run only knew about itself.
"""
from datetime import datetime, timedelta, timezone

import pytest

from agent import resilience, store, velocity


def _records(times: list[datetime]) -> list[dict]:
    return [{"uploaded_at": store.iso(t), "video_id": f"v{i}",
             "measurement": {}} for i, t in enumerate(times)]


@pytest.fixture(autouse=True)
def _clear_degradations():
    resilience.reset()
    yield
    resilience.reset()


def test_normal_schedule_is_allowed(monkeypatch):
    """2x/day, with a late run on top, must never trip the ceiling or the gap
    floor - that is the intended cadence."""
    now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    times = [now - timedelta(hours=h) for h in (5, 9, 24, 29, 48)]
    monkeypatch.setattr(store, "all_records", lambda: _records(times))

    report = velocity.check(now)
    assert report["uploads_last_24h"] <= velocity.MAX_UPLOADS_24H


def test_burst_is_blocked(monkeypatch):
    """The shape of the real 2026-07-30 day: 10 uploads inside 24h."""
    now = datetime(2026, 9, 1, 23, 0, tzinfo=timezone.utc)
    times = [now - timedelta(hours=h) for h in range(1, 11)]
    monkeypatch.setattr(store, "all_records", lambda: _records(times))

    with pytest.raises(velocity.VelocityBlocked) as excinfo:
        velocity.check(now)
    assert "10 uploads in the last 24h" in str(excinfo.value)


def test_override_allows_a_deliberate_burst(monkeypatch):
    now = datetime(2026, 9, 1, 23, 0, tzinfo=timezone.utc)
    times = [now - timedelta(hours=h) for h in range(1, 11)]
    monkeypatch.setattr(store, "all_records", lambda: _records(times))
    monkeypatch.setenv(velocity.OVERRIDE_ENV, "1")

    report = velocity.check(now)
    assert report["override"] is True


def test_empty_override_does_not_disable_the_guardrail(monkeypatch):
    """A stray empty env var must not silently switch the brake off."""
    now = datetime(2026, 9, 1, 23, 0, tzinfo=timezone.utc)
    times = [now - timedelta(hours=h) for h in range(1, 11)]
    monkeypatch.setattr(store, "all_records", lambda: _records(times))
    monkeypatch.setenv(velocity.OVERRIDE_ENV, "")

    with pytest.raises(velocity.VelocityBlocked):
        velocity.check(now)


def test_elevated_48h_warns_but_does_not_block(monkeypatch):
    """Right after a burst, 48h cannot tell 'still bursting' from 'back on
    schedule'. It must flag, never halt the normal cadence."""
    now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    # 2 in the last 24h (on-schedule), but 10 across 48h (yesterday's burst).
    times = ([now - timedelta(hours=h) for h in (5, 19)]
             + [now - timedelta(hours=h) for h in range(26, 34)])
    monkeypatch.setattr(store, "all_records", lambda: _records(times))

    report = velocity.check(now)  # must not raise
    assert report["elevated_48h"] is True
    assert any(d["stage"] == "upload-velocity" for d in resilience.degradations())


def test_recent_upload_count_ignores_future_uploads(monkeypatch):
    """recent_upload_count is bounded at BOTH ends. Without the upper bound a
    historical replay counts uploads that had not happened yet - and runner
    clock skew could read as a burst."""
    now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    times = [now - timedelta(hours=2),
             now + timedelta(hours=5),      # future
             now + timedelta(hours=30)]     # future
    monkeypatch.setattr(store, "all_records", lambda: _records(times))

    assert store.recent_upload_count(24, now) == 1


def test_cluster_inside_the_gap_is_deferred(monkeypatch):
    """The real 2026-09-23 shape: the next run starts 13 minutes after an
    upload. Well under the 24h ceiling, still refused."""
    now = datetime(2026, 9, 23, 5, 54, tzinfo=timezone.utc)
    times = [datetime(2026, 9, 23, 5, 41, tzinfo=timezone.utc),
             datetime(2026, 9, 22, 19, 46, tzinfo=timezone.utc)]
    monkeypatch.setattr(store, "all_records", lambda: _records(times))

    with pytest.raises(velocity.VelocityBlocked) as excinfo:
        velocity.check(now)
    assert "13 minutes ago" in str(excinfo.value)


def test_gap_just_past_the_floor_is_allowed(monkeypatch):
    now = datetime(2026, 9, 23, 11, 7, tzinfo=timezone.utc)
    times = [now - timedelta(hours=velocity.MIN_GAP_HOURS, minutes=1)]
    monkeypatch.setattr(store, "all_records", lambda: _records(times))

    report = velocity.check(now)
    assert report["hours_since_last"] > velocity.MIN_GAP_HOURS


def test_override_also_lifts_the_gap(monkeypatch):
    now = datetime(2026, 9, 23, 6, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(store, "all_records",
                        lambda: _records([now - timedelta(minutes=10)]))
    monkeypatch.setenv(velocity.OVERRIDE_ENV, "1")

    assert velocity.check(now)["override"] is True


def test_gap_ignores_uploads_after_now(monkeypatch):
    """A replay at an earlier time must not be blocked by a later upload."""
    now = datetime(2026, 9, 23, 6, 0, tzinfo=timezone.utc)
    times = [now + timedelta(minutes=5), now - timedelta(hours=10)]
    monkeypatch.setattr(store, "all_records", lambda: _records(times))

    assert round(velocity.check(now)["hours_since_last"]) == 10


def test_no_uploads_yet_is_allowed(monkeypatch):
    monkeypatch.setattr(store, "all_records", lambda: [])
    assert velocity.check(datetime(2026, 9, 23, tzinfo=timezone.utc))[
        "hours_since_last"] is None
