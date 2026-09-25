"""Slot arithmetic for the weekly packet. A gap here is a scheduled run with
nothing to publish; an overlap is a researched story silently dropped."""
import json
import sys
from datetime import datetime, timezone

import pytest

from agent import cadence, editorial, packet
from scripts import assemble_packet
from tests.test_packet import _packet, _story


def _utc(y, m, d, h=0, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=timezone.utc)


# The shared _story fixture titles everything "A real title about ...", which
# is fine for a one-story test and wrong for a full week: 28 of them is a 100%
# single-opener packet, which packet.validate_title_diversity now rejects on
# purpose (83.7% of the channel's first 129 real videos opened with "The").
# Varying the opener here keeps this fixture a realistic week rather than
# weakening the gate it would otherwise trip.
_TITLE_OPENERS = ("A", "The", "One", "Two", "Nine", "Someone", "Inside")


def _draft(subject, index=0):
    draft = _story(_utc(2026, 9, 9, 6, 7), subject)
    for key in ("story_id", "packet_id", "status", "slot"):
        draft.pop(key)
    opener = _TITLE_OPENERS[index % len(_TITLE_OPENERS)]
    draft["title"] = f"{opener} real title about {subject}"
    draft["editorial_rationale"] = (
        "Uses a fresh angle informed by the strong early-performance examples.")
    return draft


def _brief_provenance():
    return {
        "path": "content/weekly_editorial_brief.json",
        "generated_at": "2026-09-08T00:00:00Z",
        "sha256": "a" * 64,
        "usable_records": 42,
        "hook_retention_records": 30,
        "hook_evidence_records": 8,
        "hook_evidence_sufficient": True,
    }


def _brief_document(generated_at):
    return {
        "schema_version": editorial.BRIEF_SCHEMA_VERSION,
        "generated_at": generated_at,
        "purpose": "Required measured feedback for the next weekly research session.",
        "methodology": {"latest_views": "Directional mature evidence only."},
        "channel_snapshot": {
            "measured_records": 4,
            "fresh_measured_records": 4,
            "stale_measurement_records": 0,
            "evidence_status": "fresh",
            "usable_records": 4,
            "excluded_no_signal_records": 0,
            "comparable_early_records": 4,
            "hook_retention_records": 4,
            "hook_evidence_records": 4,
            "hook_evidence_minimum": 4,
            "hook_evidence_sufficient": True,
        },
        "editorial_rules": ["Use evidence only for fresh story angles."],
        "strong_mature_examples": [],
        "strong_early_examples": [],
        "strong_hook_examples": [],
        "hook_watch_examples": [],
        "early_performance_watchlist": [],
        "avoid_subjects": [],
    }


def test_slugs_survive_accents():
    """The id is what the ledger is keyed on forever, so Göbekli Tepe has to
    fold to gobekli-tepe and not to gbekli-tepe."""
    assert assemble_packet.slug("Göbekli Tepe") == "gobekli-tepe"
    assert assemble_packet.slug("Ötzi the Iceman") == "otzi-the-iceman"


def test_a_full_week_is_fourteen_slots_in_order():
    drafts = [_draft(f"Subject {i}", i) for i in range(cadence.SLOTS_PER_WEEK)]
    built = assemble_packet.build(
        drafts, packet_id="w1", start_after=_utc(2026, 9, 9, 15, 15),
        count=cadence.SLOTS_PER_WEEK, existing_path="/nonexistent")
    assert len(built["stories"]) == 14
    slots = [s["slot"]["utc"] for s in built["stories"]]
    assert slots == sorted(slots)
    assert slots[0] == "2026-09-10T0607Z"
    assert packet.validate_packet(built) == []


def test_packet_records_editorial_brief_provenance():
    drafts = [_draft(f"Subject {i}") for i in range(2)]
    brief = _brief_provenance()
    built = assemble_packet.build(
        drafts, packet_id="w1", start_after=_utc(2026, 9, 9, 15, 15),
        count=2, existing_path="/nonexistent", editorial_brief=brief)

    assert built["editorial_brief"] == brief
    assert "Editorial brief" in assemble_packet.markdown(built)
    assert "Editorial rationale" in assemble_packet.markdown(built)


def test_editorial_packet_refuses_a_draft_without_a_rationale():
    draft = _draft("Needs a reason")
    draft.pop("editorial_rationale")

    with pytest.raises(SystemExit, match="editorial_rationale"):
        assemble_packet.build(
            [draft], packet_id="w1", start_after=_utc(2026, 9, 9, 15, 15),
            count=1, existing_path="/nonexistent",
            editorial_brief=_brief_provenance())


def test_cli_requires_and_records_a_fresh_editorial_brief(tmp_path, monkeypatch):
    drafts = tmp_path / "drafts.json"
    drafts.write_text(json.dumps([_draft("Fresh editorial subject")]))
    brief = tmp_path / "brief.json"
    brief.write_text(json.dumps(_brief_document(
        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))))
    out = tmp_path / "packet.json"
    monkeypatch.setattr(assemble_packet, "MARKDOWN_PATH", str(tmp_path / "packet.md"))

    monkeypatch.setattr(sys, "argv", [
        "assemble_packet.py", str(drafts), "--packet-id", "brief-test",
        "--count", "1", "--start-after", "2026-09-09T15:15:00Z",
        "--out", str(out), "--editorial-brief", str(brief),
    ])

    assert assemble_packet.main() == 0
    built = json.loads(out.read_text())
    assert built["editorial_brief"]["sha256"]
    assert built["editorial_brief"]["usable_records"] == 4
    assert built["editorial_brief"]["hook_evidence_sufficient"] is True


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
        _story(_utc(2026, 9, 10, 6, 7), "Kept Subject", story_id="keep-me"),
        _story(_utc(2026, 9, 10, 11, 7), "Also Kept", story_id="keep-me-too"),
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
    assert built["carried_forward"] == ["2026-09-10T0607Z", "2026-09-10T1107Z"]


def test_a_published_story_in_the_overlap_is_not_carried_forward(tmp_path):
    story = _story(_utc(2026, 9, 10, 6, 7), "Already Out", story_id="done")
    packet.record_status(story, "published", video_id="vid1")
    existing = tmp_path / "packet.json"
    existing.write_text(json.dumps(_packet([story])))

    drafts = [_draft(f"New Subject {i}") for i in range(cadence.SLOTS_PER_WEEK)]
    built = assemble_packet.build(
        drafts, packet_id="w2", start_after=_utc(2026, 9, 9, 15, 15),
        count=cadence.SLOTS_PER_WEEK, existing_path=str(existing))
    assert built["carried_forward"] == []
    assert "done" not in [s["story_id"] for s in built["stories"]]


# ------------------------------------------------ overdue stories (2026-09-23)
# 2026-W39 skipped six researched stories because their slots had passed
# unpublished when the new week was written and nothing could carry them.

def _week_with_previous(tmp_path, previous_stories, drafts=None):
    existing = tmp_path / "packet.json"
    existing.write_text(json.dumps(_packet(previous_stories)))
    drafts = drafts or [_draft(f"New Subject {i}", i) for i in range(cadence.SLOTS_PER_WEEK)]
    return assemble_packet.build(
        drafts, packet_id="w2", start_after=_utc(2026, 9, 9, 15, 15),
        count=cadence.SLOTS_PER_WEEK, existing_path=str(existing))


def test_an_overdue_unpublished_story_is_carried_in_front_of_the_week(tmp_path):
    late = _story(_utc(2026, 9, 9, 11, 7), "Late Subject", story_id="late-one")
    built = _week_with_previous(tmp_path, [late])
    assert built["stories"][0]["story_id"] == "late-one"
    assert built["carried_overdue"] == ["2026-09-09T1107Z"]
    assert len(built["stories"]) == cadence.SLOTS_PER_WEEK + 1
    assert packet.validate_packet(built) == []
    # FIFO: it is the first story the next run publishes.
    due = packet.due_stories(built, now=_utc(2026, 9, 9, 16, 10), allow_early=False)
    assert due[0]["story_id"] == "late-one"


def test_published_failed_or_duplicated_overdue_stories_are_not_carried(tmp_path):
    out = _story(_utc(2026, 9, 9, 6, 7), "Already Out", story_id="out")
    packet.record_status(out, "published", video_id="v")
    dead = _story(_utc(2026, 9, 9, 1, 7), "Retired One", story_id="dead")
    packet.record_status(dead, "failed")
    clash = _story(_utc(2026, 9, 9, 11, 7), "New Subject 3", story_id="clash")
    built = _week_with_previous(tmp_path, [out, dead, clash])
    assert built["carried_overdue"] == []
    assert packet.validate_packet(built) == []


def test_at_most_one_day_of_overdue_stories_is_carried(tmp_path):
    names = iter(["Alder", "Birch", "Cedar", "Dogwood", "Elm", "Fir", "Ginkgo", "Hazel"])
    late = [_story(_utc(2026, 9, 7 + d, h, 7), f"{next(names)} Mystery",
                   story_id=f"late-{d}-{h}")
            for d in range(2) for h in cadence.SLOT_HOURS_UTC]
    built = _week_with_previous(tmp_path, late)
    assert len(built["carried_overdue"]) == assemble_packet.MAX_OVERDUE_CARRY
    assert built["carried_overdue"][-1] == "2026-09-08T1107Z"   # the newest kept
    assert packet.validate_packet(built) == []


def test_validation_refuses_more_overdue_stories_than_the_cap(tmp_path):
    built = _week_with_previous(tmp_path, [])
    extra = [_story(_utc(2026, 9, 8, h, 7), f"{name} Riddle", story_id=f"x-{h}")
             for h, name in zip(cadence.SLOT_HOURS_UTC, ["Oak", "Pine", "Rowan", "Yew"])] + [
             _story(_utc(2026, 9, 7, 16, 7), "One Too Many", story_id="x-extra")]
    built["stories"] = extra + built["stories"]
    assert any("before the window" in p for p in packet.validate_packet(built))
