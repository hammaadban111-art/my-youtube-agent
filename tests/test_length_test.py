"""The 35s vs 60s length test (agent/packet.py LENGTH_TEST_PACKET)."""
from datetime import datetime, timedelta, timezone

from agent import config, editorial, packet


def _story(slot, length=None, packet_id="2026-11-29"):
    s = {"slot": {"utc": slot}, "packet_id": packet_id,
         "editorial": {"story_type": "natural-wonder"}}
    if length is not None:
        s["editorial"]["length_seconds"] = length
    return s


def _slot(raw):
    return datetime.strptime(raw, "%Y-%m-%dT%H%MZ").replace(tzinfo=timezone.utc)


def test_each_day_has_one_long_and_one_short_slot_and_they_swap():
    day = datetime(2026, 12, 3, tzinfo=timezone.utc)
    arms = []
    for d in range(4):
        morning = day + timedelta(days=d, hours=6, minutes=7)
        evening = day + timedelta(days=d, hours=11, minutes=7)
        pair = (packet.length_arm(morning), packet.length_arm(evening))
        assert sorted(pair) == [35, 60]
        arms.append(pair)
    assert arms[0] != arms[1] and arms[0] == arms[2]


def test_a_new_story_must_declare_its_slots_length():
    slot = "2026-12-03T0607Z"
    arm = str(packet.length_arm(_slot(slot)))
    wrong = "35" if arm == "60" else "60"
    assert "length_seconds" in packet.validate_editorial(_story(slot), "s1")[0]
    assert packet.validate_editorial(_story(slot, wrong), "s1")
    assert packet.validate_editorial(_story(slot, arm), "s1") == []


def test_stories_from_older_packets_are_exempt():
    assert packet.validate_editorial(
        _story("2026-12-02T1107Z", packet_id="2026-11-25"), "s1") == []


def test_render_length_follows_the_story():
    assert config.target_seconds({"editorial": {"length_seconds": "60"}}) == 60.0
    assert config.target_seconds({"editorial": {}}) == float(config.VIDEO_LENGTH_SECONDS)
    assert config.target_seconds({"editorial": {"length_seconds": "90"}}) == float(
        config.VIDEO_LENGTH_SECONDS)


def test_length_seconds_reaches_the_script():
    story = _story("2026-12-03T0607Z", "60")
    assert packet.to_script(story)["editorial"]["length_seconds"] == "60"


def test_the_brief_lists_slot_lengths():
    rule = editorial._length_rule(datetime(2026, 11, 29, tzinfo=timezone.utc))
    assert "length_seconds" in rule and "2026-12-03T0607Z=" in rule
    rules = " ".join(editorial.build_brief()["editorial_rules"])
    assert "LENGTH TEST" in rules
