"""Guards the suppression-aware training rules added in Phase 1.

Real context these encode: on 2026-07-30 this channel hit a platform
distribution throttle. Videos from that window measured 0-16 views while
healthy ones measured ~1,000. Feeding the throttled ones into the model as
"bad topics" would have (a) dragged the baseline prediction for every future
video down to ~5, and (b) permanently steered the script writer away from
subjects that were never actually given a chance.
"""
import statistics

import pytest

from agent import predict


def _record(topic: str, views: int, retention_available: bool = False) -> dict:
    return {
        "topic_subject": topic,
        "title": f"{topic} video",
        "measurement": {
            "actual_views": views,
            "retention": {"available": retention_available, "curve": []},
        },
    }


def test_throttled_videos_are_excluded_from_signal():
    """A near-zero-view video carries no information about its topic."""
    assert predict.has_signal(_record("healthy", 1000)) is True
    assert predict.has_signal(_record("throttled", 0)) is False
    assert predict.has_signal(_record("throttled", 5)) is False
    assert predict.has_signal(_record("throttled", 16)) is False
    # Unmeasured is also not signal.
    assert predict.has_signal({"measurement": {"actual_views": None}}) is False
    assert predict.has_signal({}) is False


def test_throttled_videos_do_not_drag_the_baseline_down(monkeypatch):
    """The real regression: with throttled videos in the median, the model
    predicted ~5 views for every future video regardless of topic."""
    healthy = [_record("a", 999), _record("b", 1022),
               _record("c", 1052), _record("d", 1055)]
    throttled = [_record("e", 0), _record("f", 3), _record("g", 4),
                 _record("h", 4), _record("i", 5)]
    monkeypatch.setattr(predict.store, "measured_records",
                        lambda: healthy + throttled)
    monkeypatch.setattr(predict, "self_improve_active", lambda now=None: (False, 2))

    naive_median = statistics.median(
        [r["measurement"]["actual_views"] for r in healthy + throttled])
    result = predict.predict("a")

    assert naive_median < 10, "fixture should reproduce the dragged-down median"
    assert result["predicted_views"] > 900, (
        "throttled videos must not pull the baseline toward zero")
    assert result["basis"]["n_excluded_no_signal"] == 5


def test_throttled_topic_is_not_punished(monkeypatch):
    """A topic that was throttled must not be learned as a bad topic."""
    # Needs >= MIN_SAMPLES_FOR_LEARNING records that SURVIVE filtering, or
    # predict() stops at the baseline branch before any multiplier is computed.
    records = [_record("good", 1000) for _ in range(predict.MIN_SAMPLES_FOR_LEARNING)]
    records += [_record("throttled topic", 3) for _ in range(4)]
    monkeypatch.setattr(predict.store, "measured_records", lambda: records)
    monkeypatch.setattr(predict, "self_improve_active", lambda now=None: (True, 30))

    result = predict.predict("throttled topic")
    # With the throttled records excluded there is no topic history left, so
    # the multiplier must be neutral (1.0) rather than punishing.
    assert result["basis"]["topic_multiplier"] == 1.0
    assert result["basis"]["topic_samples"] == 0


def test_top_performers_excludes_throttled(monkeypatch):
    """top_performers feeds back into the script prompt - a throttled video
    must never be held up as an example to imitate."""
    monkeypatch.setattr(predict.store, "measured_records", lambda: [
        _record("healthy", 1000), _record("throttled", 4)])
    subjects = [t["subject"] for t in predict.top_performers()]
    assert subjects == ["healthy"]


@pytest.mark.parametrize("value,today,expected_hold", [
    ("", "2026-08-01", None),                      # unset -> no hold
    ("2026-08-20", "2026-08-01", "2026-08-20"),    # future -> hold
    ("2026-08-20", "2026-08-25", None),            # past   -> released
    ("not-a-date", "2026-08-01", "not-a-date"),    # garbage -> FAIL CLOSED
])
def test_self_improve_brake(monkeypatch, value, today, expected_hold):
    """SELF_IMPROVE_AFTER must fail closed: a typo holds learning off rather
    than silently re-enabling it on throttled data."""
    from datetime import date
    monkeypatch.setattr(predict, "SELF_IMPROVE_AFTER", value)
    assert predict._hold_until(date.fromisoformat(today)) == expected_hold


def test_brake_overrides_sufficient_history(monkeypatch):
    """With plenty of history and approval already granted, the gate is open;
    the brake must still close it regardless of approval."""
    monkeypatch.setattr(predict.store, "days_of_history", lambda now=None: 30)
    monkeypatch.setattr(predict, "_is_approved", lambda: True)

    monkeypatch.setattr(predict, "SELF_IMPROVE_AFTER", "")
    assert predict.self_improve_active()[0] is True

    monkeypatch.setattr(predict, "SELF_IMPROVE_AFTER", "2099-01-01")
    assert predict.self_improve_active()[0] is False


def test_gate_blocks_without_approval_even_with_sufficient_history(monkeypatch):
    """Sufficient history and no brake, but no approval yet: stays inactive,
    and the dashboard status reports it as waiting on approval."""
    monkeypatch.setattr(predict.store, "days_of_history", lambda now=None: 30)
    monkeypatch.setattr(predict, "SELF_IMPROVE_AFTER", "")
    monkeypatch.setattr(predict, "_is_approved", lambda: False)
    assert predict.self_improve_status()["awaiting_approval"] is True

    assert predict.self_improve_active()[0] is False
