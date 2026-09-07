"""The packet is now the only source of stories, so every way it can go wrong
is a way the channel goes silent. These are the ways it has to fail loudly, and
the ways it must not fail at all."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from agent import cadence, packet


def _utc(y, m, d, h=0, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=timezone.utc)


def _story(slot, subject="Dyatlov Pass", story_id=None, status="proposed"):
    """A minimal story that passes every validator, so a test can break exactly
    one thing and see only that break reported."""
    hook = "Nine hikers cut their tent open from inside"
    return {
        "story_id": story_id or f"st-{cadence.slot_id(slot)}-x",
        "packet_id": "test",
        "status": status,
        "slot": {"utc": cadence.slot_id(slot),
                 "iso": slot.isoformat().replace("+00:00", "Z"),
                 "local": cadence.describe(slot)},
        "topic_subject": subject,
        "title": f"A real title about {subject}",
        "description": f"A description of {subject}. #One #Two #shorts",
        "hook_candidates": [
            {"text": hook, "total": 36, "why": "w"},
            {"text": "A second candidate opening line", "total": 28, "why": "w"},
            {"text": "A third candidate opening line", "total": 25, "why": "w"},
        ],
        "hook_choice": {"chosen_index": 0, "reason": "best"},
        "factual_claims": [{"text": f"A checkable claim about {subject}",
                            "segment_index": i} for i in range(5)],
        "segments": [
            {"narration": f"{hook}. Then the rest of segment {i}."
                          if i == 0 else f"Segment {i} narration text.",
             "visual_query": "ural mountains snow ridge",
             "visual_fallback": "winter mountain"}
            for i in range(5)
        ],
        "research": {
            "summary": f"Background on {subject}.",
            "sources": [{"title": "Reference", "url": "https://example.org/a",
                         "type": "reference"}],
            "verification": [{"claim": f"A checkable claim about {subject}",
                              "segment_index": i, "verdict": "SUPPORTED",
                              "note": "checked", "source": "https://example.org/a",
                              "correction": None} for i in range(5)],
        },
        "thumbnail_prompt": "A thumbnail",
        "metadata": {"tags": ["a", "b", "c"], "category": "Education",
                     "language": "en"},
    }


def _packet(stories):
    return {
        "schema_version": packet.SCHEMA_VERSION,
        "packet_id": "test-packet",
        "generated_at": "2026-09-07T19:00:00Z",
        "generated_by": "claude-opus-5",
        "niche": "unsolved mysteries and bizarre history",
        "window": {"slot_count": len(stories)},
        "stories": stories,
    }


# ------------------------------------------------------------------ loading

def test_a_missing_packet_fails_loudly_and_says_what_to_do(tmp_path):
    with pytest.raises(packet.PacketError) as e:
        packet.load_packet(str(tmp_path / "nope.json"))
    message = str(e.value)
    assert "No story packet" in message
    # The whole point of removing the generator: the failure must say that
    # nothing was invented instead.
    assert "no longer writes its own stories" in message


def test_a_corrupt_packet_fails_rather_than_being_half_read(tmp_path):
    path = tmp_path / "packet.json"
    path.write_text("{ this is not json")
    with pytest.raises(packet.PacketError) as e:
        packet.load_packet(str(path))
    assert "could not be read" in str(e.value)
    assert "will not invent a story" in str(e.value)


def test_a_packet_from_a_future_schema_is_refused(tmp_path):
    path = tmp_path / "packet.json"
    path.write_text(json.dumps({"schema_version": 99, "stories": []}))
    with pytest.raises(packet.PacketError):
        packet.load_packet(str(path))


# --------------------------------------------------------------- validation

def test_a_sound_packet_validates_clean():
    assert packet.validate_packet(_packet([_story(_utc(2026, 9, 9, 6, 7))])) == []


def test_two_stories_may_not_claim_the_same_slot():
    slot = _utc(2026, 9, 9, 6, 7)
    problems = packet.validate_packet(_packet([
        _story(slot, "Dyatlov Pass", story_id="a"),
        _story(slot, "Voynich manuscript", story_id="b"),
    ]))
    assert any("two stories claim slot" in p for p in problems)


def test_a_slot_that_is_not_a_real_publishing_slot_is_rejected():
    problems = packet.validate_packet(_packet([_story(_utc(2026, 9, 9, 3, 7))]))
    assert any("not one of the real publishing slots" in p for p in problems)


def test_a_repeated_subject_inside_one_packet_is_rejected():
    """The Tamam Shud failure, prevented a week early instead of at run time:
    two videos went up 3.5 hours apart on the same subject under titles that
    shared no words."""
    problems = packet.validate_packet(_packet([
        _story(_utc(2026, 9, 9, 6, 7), "Tamam Shud case", story_id="a"),
        _story(_utc(2026, 9, 9, 11, 7), "Tamam Shud", story_id="b"),
    ]))
    assert any("already planned in this same packet" in p for p in problems)


def test_a_subject_the_channel_already_published_is_rejected():
    problems = packet.validate_packet(
        _packet([_story(_utc(2026, 9, 9, 6, 7), "Lake Natron")]),
        published_subjects=["Lake Natron calcification of animals"])
    assert any("has already been published" in p for p in problems)


def test_the_script_contract_is_enforced_on_packet_stories():
    """A packet story clears exactly the bar a generated script had to."""
    story = _story(_utc(2026, 9, 9, 6, 7))
    story["segments"][2]["visual_query"] = "fog"
    story["segments"][3]["visual_query"] = "a"
    problems = packet.validate_packet(_packet([story]))
    assert any("banned generic query" in p for p in problems)
    assert any("must be 3-6 words" in p for p in problems)


def test_a_contradicted_verdict_without_a_correction_is_rejected():
    """The pipeline rewrites narration from this field. A verdict that says the
    line is wrong and offers nothing in its place would ship the wrong line."""
    story = _story(_utc(2026, 9, 9, 6, 7))
    story["research"]["verification"][1]["verdict"] = "CONTRADICTED"
    problems = packet.validate_packet(_packet([story]))
    assert any("carries no correction" in p for p in problems)


def test_a_claim_nobody_checked_is_rejected():
    story = _story(_utc(2026, 9, 9, 6, 7))
    story["research"]["verification"].pop()
    problems = packet.validate_packet(_packet([story]))
    assert any("does not cover the claims from segment(s) 4" in p for p in problems)


def test_a_story_with_no_source_is_rejected():
    story = _story(_utc(2026, 9, 9, 6, 7))
    story["research"]["sources"] = []
    problems = packet.validate_packet(_packet([story]))
    assert any("sources needs at least" in p for p in problems)


def test_a_title_youtube_would_refuse_is_rejected():
    story = _story(_utc(2026, 9, 9, 6, 7))
    story["title"] = "x" * 101
    problems = packet.validate_packet(_packet([story]))
    assert any("YouTube's limit" in p for p in problems)


# ---------------------------------------------------------------- selection

def test_stories_are_taken_in_slot_order_not_exact_slot_match():
    """FIFO, deliberately. Exact matching would drop a failed slot's story
    forever, and GitHub's scheduler is late often enough to lose slots on its
    own."""
    early = _story(_utc(2026, 9, 9, 1, 7), "Dyatlov Pass", story_id="early")
    late = _story(_utc(2026, 9, 9, 6, 7), "Voynich manuscript", story_id="late")
    chosen = packet.select_story(_packet([late, early]), now=_utc(2026, 9, 9, 6, 30))
    assert chosen["story_id"] == "early"


def test_a_slot_in_the_future_is_not_claimed_early():
    story = _story(_utc(2026, 9, 12, 6, 7))
    with pytest.raises(packet.PacketError) as e:
        packet.select_story(_packet([story]), now=_utc(2026, 9, 9, 6, 30))
    assert "No story is due yet" in str(e.value)


def test_an_exhausted_packet_says_so_and_publishes_nothing():
    story = _story(_utc(2026, 9, 9, 6, 7))
    packet.record_status(story, "published", video_id="abc")
    with pytest.raises(packet.PacketError) as e:
        packet.select_story(_packet([story]), now=_utc(2026, 9, 9, 6, 30))
    assert "exhausted" in str(e.value)
    assert "no story was invented" in str(e.value)


def test_a_published_story_is_never_selected_twice():
    first = _story(_utc(2026, 9, 9, 1, 7), "Dyatlov Pass", story_id="first")
    second = _story(_utc(2026, 9, 9, 6, 7), "Voynich manuscript", story_id="second")
    packet.record_status(first, "published", video_id="abc")
    chosen = packet.select_story(_packet([first, second]), now=_utc(2026, 9, 9, 6, 30))
    assert chosen["story_id"] == "second"


def test_a_story_that_keeps_failing_is_retired_rather_than_wedging_the_queue():
    stuck = _story(_utc(2026, 9, 9, 1, 7), "Dyatlov Pass", story_id="stuck")
    nxt = _story(_utc(2026, 9, 9, 6, 7), "Voynich manuscript", story_id="next")
    for _ in range(packet.MAX_ATTEMPTS):
        packet.record_status(stuck, "queued")
    chosen = packet.select_story(_packet([stuck, nxt]), now=_utc(2026, 9, 9, 6, 30))
    assert chosen["story_id"] == "next"


# ------------------------------------------------------------------ ledger

def test_the_ledger_keeps_a_durable_history_per_story():
    story = _story(_utc(2026, 9, 9, 6, 7))
    packet.record_status(story, "queued", note="claimed")
    entry = packet.record_status(story, "published", note="done", video_id="vid1")
    assert entry["status"] == "published"
    assert entry["video_id"] == "vid1"
    assert [h["status"] for h in entry["history"]] == ["queued", "published"]
    assert entry["attempts"] == 1


def test_a_failure_before_the_attempt_limit_returns_the_story_to_the_queue():
    story = _story(_utc(2026, 9, 9, 6, 7))
    script = packet.to_script(story)
    packet.record_status(story, "queued")
    packet.mark_failed(script, "RemoteProtocolError: server disconnected")
    assert packet.ledger_entry(story["story_id"])["status"] == "proposed"


def test_a_failure_never_un_publishes_a_published_story():
    """A crash in the analytics sweep runs AFTER the upload. Marking that story
    failed would let the next weekly packet propose it again."""
    story = _story(_utc(2026, 9, 9, 6, 7))
    script = packet.to_script(story)
    packet.mark_published(script, "vid1")
    packet.mark_failed(script, "something later blew up")
    assert packet.ledger_entry(story["story_id"])["status"] == "published"


def test_an_unreadable_ledger_is_fatal_rather_than_read_as_empty(monkeypatch, tmp_path):
    """Reading a damaged ledger as "nothing has ever been published" would
    republish the whole week."""
    path = tmp_path / "story_history.json"
    path.write_text("{ not json")
    monkeypatch.setattr(packet, "LEDGER_PATH", str(path))
    with pytest.raises(packet.PacketError):
        packet.ledger_entry("anything")


# ------------------------------------------------------------------- claim

def test_claiming_skips_a_story_whose_subject_went_out_since(monkeypatch, tmp_path):
    """The one duplicate a pre-flight check cannot catch: a week-old packet
    naming a subject a later run has since covered."""
    early = _story(_utc(2026, 9, 9, 1, 7), "Lake Natron", story_id="dupe")
    fresh = _story(_utc(2026, 9, 9, 6, 7), "Voynich manuscript", story_id="fresh")
    path = tmp_path / "packet.json"
    path.write_text(json.dumps(_packet([early, fresh])))
    monkeypatch.setattr(packet, "PACKET_PATH", str(path))
    monkeypatch.setattr(packet.history, "published_subjects",
                        lambda: ["Lake Natron calcification of animals"])

    script = packet.claim_script(now=_utc(2026, 9, 9, 6, 30))
    assert script["story_id"] == "fresh"
    assert packet.ledger_entry("dupe")["status"] == "skipped"
    assert packet.ledger_entry("fresh")["status"] == "queued"


def test_the_script_the_pipeline_receives_has_the_old_shape_plus_provenance():
    script = packet.to_script(_story(_utc(2026, 9, 9, 6, 7)))
    for field in ("title", "description", "topic_subject", "hook_candidates",
                  "hook_choice", "factual_claims", "segments"):
        assert field in script, field
    assert set(script["segments"][0]) == {"narration", "visual_query",
                                          "visual_fallback"}
    # New, and carried on the script so it survives the checkpoint to the
    # resume path.
    assert script["story_id"]
    assert script["verification"]
    assert script["sources"]


def test_lateness_is_measured_from_the_slot():
    story = _story(_utc(2026, 9, 9, 6, 7))
    assert packet.lateness_hours(story, _utc(2026, 9, 9, 9, 7)) == pytest.approx(3.0)
    assert packet.lateness_hours(story, _utc(2026, 9, 9, 5, 7)) == 0.0
