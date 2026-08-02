"""Videos are followed for as long as they exist, not for a week.

Shorts keep earning views for months. Two separate cutoffs used to stop
follow-up long before that - a 7-day window and a per-video reading cap - and
between them a video's totals froze while the real video kept climbing. Both
are gone. These tests pin that, and pin the two cadences that must NOT have
changed with it.
"""
from datetime import datetime, timedelta, timezone

from agent import store

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


def _record(video_id: str, age: timedelta, readings: list[timedelta]) -> dict:
    """A video uploaded `age` ago, measured at each of `readings` ago."""
    history = [{"measured_at": store.iso(NOW - r), "actual_views": 100}
               for r in sorted(readings, reverse=True)]
    return {
        "video_id": video_id,
        "uploaded_at": store.iso(NOW - age),
        "measurement": history[0] if history else {},
        "latest_measurement": history[-1] if history else {},
        "measurement_history": history,
    }


def _due(monkeypatch, *records) -> list[str]:
    monkeypatch.setattr(store, "all_records", lambda: list(records))
    return [r["video_id"] for r in store.measurable_records(NOW)]


# --- the fix ----------------------------------------------------------------

def test_video_older_than_a_week_is_still_picked_up(monkeypatch):
    """The exact case that was broken: past day 7, nothing was ever due."""
    old = _record("8 days", timedelta(days=8), [timedelta(hours=25)])
    assert _due(monkeypatch, old) == ["8 days"]


def test_tracking_does_not_stop_at_any_age(monkeypatch):
    """Not just day 8 - a video is still due a year and five years out."""
    for label, age in [("1 month", timedelta(days=30)),
                       ("6 months", timedelta(days=182)),
                       ("1 year", timedelta(days=365)),
                       ("5 years", timedelta(days=1826))]:
        rec = _record(label, age, [timedelta(hours=25)])
        assert _due(monkeypatch, rec) == [label], f"{label} stopped being tracked"


def test_a_long_reading_history_does_not_end_tracking(monkeypatch):
    """The second cutoff: a per-video cap of 24 readings ended follow-up a few
    days after the 7-day window did. A video with hundreds of readings behind
    it must still be due."""
    veteran = _record("veteran", timedelta(days=400),
                      [timedelta(hours=h) for h in range(25, 500)])
    assert len(veteran["measurement_history"]) > 400
    assert _due(monkeypatch, veteran) == ["veteran"]


# --- the 2026-08-02 change: every eligible video, every run -----------------
#
# The recheck-interval tiering these tests used to pin (3h under 48h old,
# daily after) is gone by request, so every dashboard card reflects the
# latest known numbers every time the site rebuilds - not numbers staggered
# by how long since each video's own last scheduled check. These tests now
# pin the OPPOSITE of what they used to: a video just measured minutes ago
# is still eligible again immediately, regardless of age.

def test_recently_measured_young_video_is_eligible_again_immediately(monkeypatch):
    """A 10h-old video read 1 minute ago is still eligible - there is no
    interval left to wait out."""
    just_read = _record("10h, read 1m ago", timedelta(hours=10), [timedelta(minutes=1)])
    assert _due(monkeypatch, just_read) == ["10h, read 1m ago"]


def test_recently_measured_old_video_is_eligible_again_immediately(monkeypatch):
    """A 3-day-old video read 1 minute ago is ALSO still eligible - old videos
    no longer slow down to a daily cadence."""
    just_read = _record("3d, read 1m ago", timedelta(days=3), [timedelta(minutes=1)])
    assert _due(monkeypatch, just_read) == ["3d, read 1m ago"]


def test_old_videos_are_read_every_run_not_daily(monkeypatch):
    """The rate this changed to: an old video is read on EVERY run now, not
    once a day - stated as a rate since it's the number the (now much lower)
    quota ceiling in store.py's comment is derived from."""
    rec = _record("old", timedelta(days=90), [timedelta(hours=24)])
    monkeypatch.setattr(store, "all_records", lambda: [rec])

    reads = 0
    for step in range(8):  # one full day of 3-hourly runs
        at = NOW + timedelta(hours=3 * step)
        if store.measurable_records(at):
            store.record_measurement(rec, {"measured_at": store.iso(at),
                                           "actual_views": 1})
            reads += 1
    assert reads == 8, f"an old video should be read every run now, was read {reads}x"


def test_first_reading_still_has_no_age_limit(monkeypatch):
    """Unchanged and still important: a video that missed its first reading
    because a run failed is picked up however old it is."""
    never = _record("never measured", timedelta(days=45), [])
    assert _due(monkeypatch, never) == ["never measured"]


def test_a_video_below_the_first_reading_age_is_not_due(monkeypatch):
    """The one remaining floor: nothing is measured before the 5h mark."""
    fresh = _record("2h old", timedelta(hours=2), [])
    assert _due(monkeypatch, fresh) == []
