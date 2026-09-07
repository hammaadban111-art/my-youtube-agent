"""Slot arithmetic for the weekly packet. A gap here is a scheduled run with
nothing to publish; an overlap is a researched story silently dropped."""
import json
from datetime import datetime, timezone

import pytest

from agent import cadence, packet
from scripts import assemble_packet
from tests.test_packet import _packet, _story


def _utc(y, m, d, h=0, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=timezone.utc)


def _draft(subject):
    draft = _story(_utc(2026, 9, 9, 6, 7), subject)
    for key in ("story_id", "packet_id", "status", "slot"):
        draft.pop(key)
    return draft


def test_slugs_survive_accents():
    """The id is what the ledger is keyed on forever, so Göbekli Tepe has to
    fold to gobekli-tepe and not to gbekli-tepe."""
    assert assemble_packet.slug("Göbekli Tepe") == "gobekli-tepe"
    assert assemble_packet.slug("Ötzi the Iceman") == "otzi-the-iceman"


def test_a_full_week_is_twenty_eight_slots_in_order():
    drafts = [_draft(f"Subject {i}") for i in range(cadence.SLOTS_PER_WEEK)]
    built = assemble_packet.build(
        drafts, packet_id="w1", start_after=_utc(2026, 9, 9, 15, 15),
        count=cadence.SLOTS_PER_WEEK, existing_path="/nonexistent")
    assert len(built["stories"]) == 28
    slots = [s["slot"]["utc"] for s in built["stories"]]
    assert slots == sorted(slots)
    assert slots[0] == "2026-09-09T1607Z"
    assert packet.validate_packet(built) == []


def test_too_few_drafts_refuses_rather_than_leaving_a_gap():
    with pytest.raises(SystemExit) as e:
        assemble_packet.build([_draft("One")], packet_id="w1",
                              start_after=_utc(2026, 9, 9, 15, 15), count=4,
                              existing_path="/nonexistent")
    assert "nothing to publish" in str(e.value)


def test_too_many_drafts_refuses_rather_than_overwriting_planned_work():
    with pytest.raises(SystemExit) as e:
        assemble_packet.build([_draft(f"S{i}") for i in range(5)], packet_id="w1",
                              start_after=_utc(2026, 9, 9, 15, 15), count=4,
                              existing_path="/nonexistent")
    assert "Trim the drafts" in str(e.value)


def test_unpublished_stories_in_the_overlap_are_carried_forward(tmp_path):
    """A fresh week always overlaps the tail of the previous one, because the
    week is generated mid-week. The stories already researched for those slots
    are kept exactly as they are."""
    previous = _packet([
        _story(_utc(2026, 9, 9, 16, 7), "Kept Subject", story_id="keep-me"),
        _story(_utc(2026, 9, 10, 1, 7), "Also Kept", story_id="keep-me-too"),
    ])
    existing = tmp_path / "packet.json"
    existing.write_text(json.dumps(previous))

    drafts = [_draft(f"New Subject {i}") for i in range(cadence.SLOTS_PER_WEEK - 2)]
    built = assemble_packet.build(
        drafts, packet_id="w2", start_after=_utc(2026, 9, 9, 15, 15),
        count=cadence.SLOTS_PER_WEEK, existing_path=str(existing))

    ids = [s["story_id"] for s in built["stories"]]
    assert ids[0] == "keep-me"
    assert ids[1] == "keep-me-too"
    assert built["carried_forward"] == ["2026-09-09T1607Z", "2026-09-10T0107Z"]


def test_a_published_story_in_the_overlap_is_not_carried_forward(tmp_path):
    story = _story(_utc(2026, 9, 9, 16, 7), "Already Out", story_id="done")
    packet.record_status(story, "published", video_id="vid1")
    existing = tmp_path / "packet.json"
    existing.write_text(json.dumps(_packet([story])))

    drafts = [_draft(f"New Subject {i}") for i in range(cadence.SLOTS_PER_WEEK)]
    built = assemble_packet.build(
        drafts, packet_id="w2", start_after=_utc(2026, 9, 9, 15, 15),
        count=cadence.SLOTS_PER_WEEK, existing_path=str(existing))
    assert built["carried_forward"] == []
    assert "done" not in [s["story_id"] for s in built["stories"]]
