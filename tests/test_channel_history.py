"""Guards the channel-level growth series added 2026-09-09.

The failure this prevents: for the channel's first 40 days the subscriber count
was fetched live for every dashboard build and then thrown away. 67,191 views
and 66 subscribers were both knowable at any instant, and the trend of either
was knowable at no instant, so no change to titles, hooks or topic selection
could ever be judged against the number it was meant to move.

The rules that matter here are about honesty, not arithmetic: a reading that
failed must never be stored as a zero, and eight sweeps a day must not become
eight rows.
"""
from datetime import datetime, timedelta, timezone

import pytest

from agent import store

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "CHANNEL_HISTORY_PATH",
                        str(tmp_path / "channel_history.json"))
    monkeypatch.setattr(store, "_utcnow", lambda: NOW)


def _stats(subs=66, views=67191, videos=114):
    return {"subscriber_count": subs, "total_views": views,
            "total_videos": videos}


def test_a_reading_is_recorded_and_read_back():
    store.record_channel_snapshot(_stats())
    history = store.load_channel_history()
    assert len(history) == 1
    assert history[0]["subscriber_count"] == 66
    assert history[0]["total_views"] == 67191
    assert history[0]["available"] is True
    assert history[0]["day"] == "2026-09-09"


def test_eight_sweeps_in_one_day_produce_one_row():
    """followup.yml runs every 3 hours. Without per-day dedup the series would
    be eight copies of a number that moves by single digits per week."""
    for subs in (66, 66, 67, 67, 67, 68, 68, 68):
        store.record_channel_snapshot(_stats(subs=subs))
    history = store.load_channel_history()
    assert len(history) == 1
    # The LAST reading of the day wins, not the first.
    assert history[0]["subscriber_count"] == 68


def test_separate_days_are_separate_rows(monkeypatch):
    store.record_channel_snapshot(_stats(subs=66))
    monkeypatch.setattr(store, "_utcnow", lambda: NOW + timedelta(days=1))
    store.record_channel_snapshot(_stats(subs=70))
    history = store.load_channel_history()
    assert [r["day"] for r in history] == ["2026-09-09", "2026-09-10"]
    assert [r["subscriber_count"] for r in history] == [66, 70]


def test_a_failed_reading_is_never_stored_as_zero():
    """The whole point. A zero here is indistinguishable from a channel that
    genuinely lost every subscriber, and would put a fake cliff in the trend."""
    row = store.record_channel_snapshot(error="RefreshError: bad token")
    assert row["available"] is False
    assert row["subscriber_count"] is None
    assert row["total_views"] is None
    assert "RefreshError" in row["reason"]


def test_a_failure_does_not_overwrite_a_good_reading_from_the_same_day():
    """Sweeps run 8x/day. Losing a real number because the 21:00 sweep hit a
    network blip would make the series worse, not more honest."""
    store.record_channel_snapshot(_stats(subs=66))
    store.record_channel_snapshot(error="ConnectionError: transient")
    history = store.load_channel_history()
    assert len(history) == 1
    assert history[0]["available"] is True
    assert history[0]["subscriber_count"] == 66


def test_a_hidden_subscriber_count_is_labelled_not_zeroed():
    row = store.record_channel_snapshot(
        {"subscriber_count": None, "total_views": 100, "total_videos": 5,
         "hidden_subscriber_count": True})
    assert row["subscriber_count"] is None
    assert row["available"] is True
    assert "hidden" in row["reason"]


def test_growth_needs_two_readings_before_it_claims_anything():
    assert store.channel_growth() is None
    store.record_channel_snapshot(_stats(subs=66))
    # One reading is not a trend, and reporting 0 growth would be a claim we
    # cannot support.
    assert store.channel_growth() is None


def test_growth_reports_the_delta_between_first_and_last(monkeypatch):
    store.record_channel_snapshot(_stats(subs=66, views=67191))
    monkeypatch.setattr(store, "_utcnow", lambda: NOW + timedelta(days=7))
    store.record_channel_snapshot(_stats(subs=80, views=72000))
    growth = store.channel_growth(days=7, now=NOW + timedelta(days=7))
    assert growth["subscribers_gained"] == 14
    assert growth["views_gained"] == 4809
    assert growth["subscribers_now"] == 80


def test_growth_ignores_failed_readings():
    store.record_channel_snapshot(error="boom")
    assert store.channel_growth() is None


def test_unreadable_history_reads_as_empty_not_an_exception(monkeypatch, tmp_path):
    bad = tmp_path / "corrupt.json"
    bad.write_text("{not json")
    monkeypatch.setattr(store, "CHANNEL_HISTORY_PATH", str(bad))
    assert store.load_channel_history() == []
    assert store.channel_growth() is None
