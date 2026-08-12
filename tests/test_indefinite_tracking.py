"""Videos are never SILENTLY dropped, at any age.

Two early cutoffs (a 7-day window, a per-video reading cap) used to freeze a
video's numbers while the real video kept climbing on YouTube - and said
nothing about it on screen. Both were removed entirely.

A THIRD, deliberate cutoff was added 2026-08-02 (store.AGE_FREEZE_DAYS): past
30 days a video gets exactly one more decision - a final refresh if there's
quota headroom that day, otherwise an immediate freeze at whatever numbers
are already on record - and then genuinely stops, forever. The difference
from the removed cutoffs is that this one is a real decision with a visible
label ("final as of N days" on the dashboard card), not numbers that quietly
stop moving with no explanation. These tests pin that distinction: still
picked up past a week, past a month, past a year - but past AGE_FREEZE_DAYS
that "still picked up" means due_for_final_refresh(), not measurable_records()
(which continuous, every-run refreshing is reserved for videos under the
freeze age - see tests/test_measurement_cadence.py and test_age_freeze.py).
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
        "final": False,
        "finalized_at": None,
    }


def _measurable(monkeypatch, *records) -> list[str]:
    monkeypatch.setattr(store, "all_records", lambda: list(records))
    return [r["video_id"] for r in store.measurable_records(NOW)]


def _due_final(monkeypatch, *records) -> list[str]:
    monkeypatch.setattr(store, "all_records", lambda: list(records))
    return [r["video_id"] for r in store.due_for_final_refresh(NOW)]


# --- the original fix: past a week is still picked up -----------------------

def test_video_older_than_a_week_is_still_measurable(monkeypatch):
    """The exact case that was broken originally: past day 7, nothing was
    ever due. Still under 30 days, so still on the every-run cadence."""
    old = _record("8 days", timedelta(days=8), [timedelta(hours=25)])
    assert _measurable(monkeypatch, old) == ["8 days"]


# --- 2026-08-02: past AGE_FREEZE_DAYS, "still picked up" means ONE final
# decision, not continued every-run refreshing ------------------------------

def test_video_past_freeze_age_moves_to_final_refresh_not_measurable(monkeypatch):
    """Not just day 8 - a video a year or five years out is still picked up
    somewhere, just via due_for_final_refresh() rather than
    measurable_records() once it's past AGE_FREEZE_DAYS."""
    for label, age in [("6 months", timedelta(days=182)),
                       ("1 year", timedelta(days=365)),
                       ("5 years", timedelta(days=1826))]:
        rec = _record(label, age, [timedelta(hours=25)])
        assert _measurable(monkeypatch, rec) == [], f"{label} should not be on the every-run cadence"
        assert _due_final(monkeypatch, rec) == [label], f"{label} dropped out silently"


def test_a_long_reading_history_does_not_end_tracking(monkeypatch):
    """The old per-video reading cap ended follow-up a few days after the
    7-day window did. A video with hundreds of readings behind it, now past
    the freeze age, must still get its final-refresh decision."""
    veteran = _record("veteran", timedelta(days=400),
                      [timedelta(hours=h) for h in range(25, 500)])
    assert len(veteran["measurement_history"]) > 400
    assert _measurable(monkeypatch, veteran) == []
    assert _due_final(monkeypatch, veteran) == ["veteran"]


def test_old_video_gets_exactly_one_final_decision_not_repeated(monkeypatch, tmp_path):
    """The rate this changed to for videos past the freeze age: ONE decision,
    not read every run and not read once a day - store.freeze_record() (not
    measurable_records' own selection) is what stops it reappearing here,
    which followup.finalize_aged_out() calls after handling it."""
    monkeypatch.setattr(store, "DATA_DIR", str(tmp_path))
    rec = _record("old", timedelta(days=90), [timedelta(hours=24)])
    monkeypatch.setattr(store, "all_records", lambda: [rec])

    assert store.due_for_final_refresh(NOW) == [rec]
    store.freeze_record(rec, NOW)  # what finalize_aged_out() does either way
    assert store.due_for_final_refresh(NOW) == []
    assert store.measurable_records(NOW) == []


# --- everything below is unchanged by the freeze -----------------------------

def test_recently_measured_young_video_is_eligible_again_immediately(monkeypatch):
    """A 10h-old video read 1 minute ago is still eligible - there is no
    interval left to wait out (still under the freeze age)."""
    just_read = _record("10h, read 1m ago", timedelta(hours=10), [timedelta(minutes=1)])
    assert _measurable(monkeypatch, just_read) == ["10h, read 1m ago"]


def test_recently_measured_video_under_freeze_age_is_NOT_eligible(monkeypatch):
    """A 3-day-old video read 1 minute ago is NOT eligible - videos past the
    fresh window slow down to a daily cadence."""
    just_read = _record("3d, read 1m ago", timedelta(days=3), [timedelta(minutes=1)])
    assert _measurable(monkeypatch, just_read) == []


def test_never_measured_video_under_freeze_age_is_measurable_however_late(monkeypatch):
    """A video that missed its first reading because a run failed is still
    picked up however late that first run ends up being, as long as it's
    under the freeze age."""
    never = _record("never measured, 20 days", timedelta(days=20), [])
    assert _measurable(monkeypatch, never) == ["never measured, 20 days"]


def test_never_measured_video_past_freeze_age_goes_to_final_refresh(monkeypatch):
    """The same case, but the run was missed for so long the video is now
    past the freeze age: it still gets its one final-refresh decision
    instead of silently never being read at all."""
    never = _record("never measured, 45 days", timedelta(days=45), [])
    assert _measurable(monkeypatch, never) == []
    assert _due_final(monkeypatch, never) == ["never measured, 45 days"]


def test_a_video_below_the_first_reading_age_is_not_due(monkeypatch):
    """The one remaining floor: nothing is measured before the 5h mark."""
    fresh = _record("2h old", timedelta(hours=2), [])
    assert _measurable(monkeypatch, fresh) == []
    assert _due_final(monkeypatch, fresh) == []
