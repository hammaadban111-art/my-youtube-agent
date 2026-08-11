"""Tests for B2 — duplicate topic detection (agent/history.py).

The regression these guard is real and dated: on 2026-07-30 the channel
published "The Cold War Spy Who Never Existed" (qnAWjYhNlck) and, 3.5 hours
later, "The 74-Year Mystery of Australia's Somerton Man" (2NiZ95wtNX0). Both
carry topic_subject "Tamam Shud case". The titles share no words at all, so
the pre-existing title-based avoid list could not have caught it.
"""
import importlib
import json
from datetime import date

import pytest

from agent import history


@pytest.fixture
def tmp_history(monkeypatch, tmp_path):
    path = tmp_path / "topics.json"
    monkeypatch.setattr(history, "HISTORY_PATH", str(path))
    return path


def test_the_real_repeat_would_have_been_caught():
    """The exact pair that got through, in the exact form the records hold."""
    already_published = ["Tamam Shud case"]
    assert history.is_duplicate_subject("Tamam Shud case", already_published) == "Tamam Shud case"


def test_unrelated_subjects_are_not_flagged():
    """Two lake stories from the real history that must NOT collide."""
    previous = ["Lake Natron calcification of animals", "Lake Nyos disaster",
                "Lake Peigneur sinkhole disaster"]
    assert history.is_duplicate_subject("Lake Baikal ice rings", previous) is None
    assert history.is_duplicate_subject("Antikythera mechanism", previous) is None


def test_same_subject_different_word_order():
    assert history.is_duplicate_subject(
        "1518 dancing plague", ["Dancing plague of 1518"]) == "Dancing plague of 1518"


def test_generic_descriptor_words_do_not_hide_a_repeat():
    """'Tamam Shud' and 'Tamam Shud case' are one subject; so are the
    disappearance/incident/mystery variants of any other."""
    assert history.is_duplicate_subject("Tamam Shud", ["Tamam Shud case"])
    assert history.is_duplicate_subject("Sodder children", ["Sodder children disappearance"])


def test_wikipedia_disambiguator_does_not_hide_a_repeat():
    assert history.is_duplicate_subject(
        "Devil's Kettle", ["Devil's Kettle (Minnesota)"]) == "Devil's Kettle (Minnesota)"


def test_a_narrower_take_on_a_published_subject_counts_as_a_repeat():
    assert history.is_duplicate_subject(
        "Lake Natron calcification", ["Lake Natron"]) == "Lake Natron"


def test_one_shared_word_is_not_a_repeat():
    """The subset rule needs two words. 'Lake' alone is a coincidence."""
    assert history.is_duplicate_subject("Lake Nyos", ["Lake"]) is None


def test_empty_or_missing_subject_never_matches():
    assert history.is_duplicate_subject("", ["Tamam Shud case"]) is None
    assert history.is_duplicate_subject(None, ["Tamam Shud case"]) is None
    assert history.is_duplicate_subject("Tamam Shud case", ["", None]) is None


def test_legacy_entries_without_a_subject_do_not_crash(tmp_history):
    """Every entry written before this change has only a title."""
    tmp_history.write_text(json.dumps([
        {"title": "An old one"},
        {"title": "Another old one"},
        {"title": "A new one", "topic_subject": "Tunguska event"},
    ]))
    assert history.load_recent_titles() == ["An old one", "Another old one", "A new one"]
    assert history.load_recent_subjects() == ["Tunguska event"]


def test_append_entry_stores_both_fields(tmp_history):
    history.append_entry("A title", "Tunguska event")
    history.append_entry("A title with no subject")
    entries = json.loads(tmp_history.read_text())
    assert entries[0] == {"title": "A title", "topic_subject": "Tunguska event"}
    assert entries[1] == {"title": "A title with no subject"}


def test_recent_subjects_are_deduplicated(tmp_history):
    """The prompt should not list the same subject twice just because it was
    (wrongly) published twice."""
    tmp_history.write_text(json.dumps([
        {"title": "The Cold War Spy Who Never Existed", "topic_subject": "Tamam Shud case"},
        {"title": "The 74-Year Mystery of Australia's Somerton Man", "topic_subject": "Tamam Shud case"},
    ]))
    assert history.load_recent_subjects() == ["Tamam Shud case"]


def test_lookback_widened_to_sixty():
    """At 4 uploads/day, 30 was ~7 days of history."""
    assert history.MAX_CONTEXT == 60


def test_the_hold_date_gates_detection():
    """Group B lands one change at a time, >=48h apart. B1 landed 2026-08-05,
    so B2 is held until 2026-08-07."""
    assert history.detection_held_until(date(2026, 8, 5)) == "2026-08-07"
    assert history.detection_active(date(2026, 8, 5)) is False
    assert history.detection_active(date(2026, 8, 7)) is True
    assert history.detection_active(date(2026, 8, 8)) is True


def test_a_malformed_hold_date_holds_rather_than_releasing(monkeypatch):
    """Same fail-closed rule as predict.SELF_IMPROVE_AFTER: a typo must not
    silently land the change early."""
    monkeypatch.setattr(history, "DUPLICATE_BLOCK_AFTER", "2026-13-99")
    assert history.detection_active(date(2026, 8, 9)) is False


def test_an_unset_repo_variable_does_not_release_the_hold(monkeypatch):
    """GitHub passes an unset Variable through as an EMPTY string, and empty
    must mean "use the default hold date", not "no hold"."""
    monkeypatch.setenv("DUPLICATE_BLOCK_AFTER", "")
    reloaded = importlib.reload(history)
    try:
        assert reloaded.DUPLICATE_BLOCK_AFTER == "2026-08-07"
        assert reloaded.detection_active(date(2026, 8, 5)) is False
    finally:
        monkeypatch.delenv("DUPLICATE_BLOCK_AFTER", raising=False)
        importlib.reload(history)


def test_published_subjects_unions_records_and_history(tmp_history, monkeypatch):
    from agent import store
    tmp_history.write_text(json.dumps([
        {"title": "From Topics", "topic_subject": "subject A"},
    ]))

    def mock_all_records():
        return [
            {"topic_subject": "subject B", "uploaded_at": "2026-08-01T00:00:00Z"},
            {"topic_subject": "subject C", "uploaded_at": "2026-08-02T00:00:00Z"},
        ]
    monkeypatch.setattr(store, "all_records", mock_all_records)

    subjects = history.published_subjects()
    assert set(subjects) == {"subject A", "subject B", "subject C"}
    # oldest first from records, then topics.json
    assert subjects == ["subject B", "subject C", "subject A"]


def test_published_subjects_fallback_to_topics_on_error(tmp_history, monkeypatch):
    from agent import store
    tmp_history.write_text(json.dumps([
        {"title": "From Topics", "topic_subject": "subject A"},
    ]))

    def mock_error():
        raise ValueError("Cannot read records")
    monkeypatch.setattr(store, "all_records", mock_error)

    subjects = history.published_subjects()
    assert subjects == ["subject A"]


def test_published_subjects_deduplicates(tmp_history, monkeypatch):
    from agent import store
    tmp_history.write_text(json.dumps([
        {"title": "From Topics", "topic_subject": "Tunguska Event"},
    ]))

    def mock_all_records():
        return [
            {"topic_subject": "Tunguska Event", "uploaded_at": "2026-08-01T00:00:00Z"},
            {"topic_subject": "Tunguska event", "uploaded_at": "2026-08-02T00:00:00Z"},
        ]
    monkeypatch.setattr(store, "all_records", mock_all_records)

    subjects = history.published_subjects()
    # first seen wins
    assert subjects == ["Tunguska Event"]
