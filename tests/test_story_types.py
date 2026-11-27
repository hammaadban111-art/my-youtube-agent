"""Only the story types that get views (agent/packet.py ALLOWED_STORY_TYPES)."""
from agent import editorial, packet


def _story(slot, story_type=None):
    s = {"slot": {"utc": slot}}
    if story_type is not None:
        s["editorial"] = {"story_type": story_type}
    return s


def test_a_new_story_must_declare_a_winning_type():
    problems = packet.validate_editorial(_story("2026-12-03T0607Z"), "s1")
    assert problems and "story_type" in problems[0]


def test_a_dropped_type_is_rejected():
    problems = packet.validate_editorial(
        _story("2026-12-04T1107Z", "people-crime-hoax"), "s1")
    assert problems and "Replace the story" in problems[0]


def test_each_winning_type_passes():
    for story_type in packet.ALLOWED_STORY_TYPES:
        assert packet.validate_editorial(
            _story("2026-12-05T0607Z", story_type), "s1") == []


def test_stories_already_written_for_this_week_still_publish():
    assert packet.validate_editorial(_story("2026-12-02T1107Z"), "s1") == []


def test_the_live_packet_still_validates():
    assert packet.validate_packet(packet.load_packet()) == []


def test_the_weekly_brief_tells_the_writer_the_rule():
    rules = " ".join(editorial.build_brief()["editorial_rules"])
    assert "story_type" in rules and "natural-wonder" in rules


def test_a_story_written_by_a_new_packet_needs_a_type_even_for_an_early_slot():
    story = {**_story("2026-12-02T0607Z"), "packet_id": "2026-11-29"}
    assert packet.validate_editorial(story, "s1")
