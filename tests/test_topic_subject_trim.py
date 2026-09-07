"""
A topic_subject two words over the limit used to cost a whole scheduled slot.

validate_script rejects anything over MAX_TOPIC_SUBJECT_WORDS. generate_script
gets two attempts; if the model came back long twice — which it does, because
the failure is a habit of phrasing rather than a dice roll — generate_script
raised and the run died with no video, having spent the CI minutes and two
Gemini calls out of a 20/day free-tier allowance.

That is the same trade trim_long_visual_queries already settled the other way.
These tests pin the repair: the subject is trimmed to its leading words before
validation, so a long subject costs precision, not the video.
"""
from agent import script_writer


def test_long_topic_subject_is_trimmed_to_the_leading_words():
    # The prompt's own worked example of the mistake it is guarding against.
    data = {"topic_subject": "Lake Natron calcification phenomenon of Tanzania"}

    note = script_writer.trim_long_topic_subject(data)

    assert data["topic_subject"] == "Lake Natron calcification phenomenon"
    assert len(data["topic_subject"].split()) == script_writer.MAX_TOPIC_SUBJECT_WORDS
    assert "->" in note


def test_a_subject_at_the_limit_is_left_alone():
    data = {"topic_subject": "The Dancing Plague 1518"}
    assert script_writer.trim_long_topic_subject(data) is None
    assert data["topic_subject"] == "The Dancing Plague 1518"


def test_a_short_subject_is_left_alone():
    data = {"topic_subject": "Tamam Shud"}
    assert script_writer.trim_long_topic_subject(data) is None
    assert data["topic_subject"] == "Tamam Shud"


def test_missing_subject_is_not_invented():
    """Trimming can only shorten. An absent subject stays a real validation
    failure for the corrective re-ask, exactly as a too-SHORT visual_query
    does."""
    for data in ({}, {"topic_subject": ""}, {"topic_subject": "   "}):
        assert script_writer.trim_long_topic_subject(data) is None
        assert "topic_subject is missing or empty." in script_writer.validate_script(data)


def test_trimmed_subject_then_passes_validation():
    """The end-to-end point: after the trim, the length problem is gone from
    validate_script's output."""
    data = {"topic_subject": "Lake Natron calcification phenomenon of Tanzania"}
    before = [p for p in script_writer.validate_script(data) if "topic_subject" in p]
    assert before, "expected the long subject to be rejected before trimming"

    script_writer.trim_long_topic_subject(data)

    after = [p for p in script_writer.validate_script(data) if "topic_subject" in p]
    assert after == []


def test_whitespace_is_normalised_by_the_trim():
    data = {"topic_subject": "  The   Somerton   Man   Case   Adelaide  "}
    script_writer.trim_long_topic_subject(data)
    assert data["topic_subject"] == "The Somerton Man Case"
