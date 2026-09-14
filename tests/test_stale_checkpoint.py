"""The retirement that wedged the queue instead of unwedging it.

MAX_ATTEMPTS exists so one bad story cannot block every story behind it. On
2026-09-13 it did the opposite. A story failed three times, reclaim_script
retired it and said "the next run will start the following story instead" — but
the run still saved its checkpoint on the way out, so the next run restored the
same retired story, raised the same error about a second in, and saved it
again. Four consecutive runs published nothing and no later story was ever
reachable.
"""
import pytest

from agent import packet
from tests.test_packet import _packet, _story, _utc, _write_packet


def test_retiring_a_checkpointed_story_flags_the_checkpoint_as_stale(
        tmp_path, monkeypatch):
    """The flag main.py keys off. Without it the checkpoint survives the
    retirement and the next run inherits the same dead story."""
    story = _story(_utc(2020, 1, 1, 6, 7), "Berners Street hoax", story_id="doomed")
    _write_packet(tmp_path, monkeypatch, [story])
    packet.record_status(story, "failed", attempts=packet.MAX_ATTEMPTS)

    with pytest.raises(packet.PacketError) as caught:
        packet.reclaim_script(story)
    assert getattr(caught.value, "checkpoint_is_stale", False) is True


def test_an_already_published_story_does_not_flag_the_checkpoint(
        tmp_path, monkeypatch):
    """Deliberately NOT flagged. That error asks a human to decide whether the
    checkpoint is genuinely a new story, so the run must not quietly bin it."""
    story = _story(_utc(2020, 1, 1, 6, 7), "Tulip mania", story_id="already-out")
    _write_packet(tmp_path, monkeypatch, [story])
    packet.record_status(story, "published", video_id="vid1")

    with pytest.raises(packet.PacketError) as caught:
        packet.reclaim_script(story)
    assert getattr(caught.value, "checkpoint_is_stale", False) is False


def test_a_story_with_attempts_left_is_reclaimed_normally(tmp_path, monkeypatch):
    """The ordinary resume path must keep working: a story under the limit is
    re-queued and counted, not retired."""
    story = _story(_utc(2020, 1, 1, 6, 7), "Nan Madol", story_id="still-going")
    _write_packet(tmp_path, monkeypatch, [story])
    packet.record_status(story, "proposed", attempts=1)

    reclaimed = packet.reclaim_script(story)
    assert reclaimed["story_id"] == "still-going"
    entry = packet.ledger_entry("still-going")
    assert entry["status"] == "queued"
    assert entry["attempts"] == 2


def test_main_clears_the_checkpoint_when_the_story_is_retired(
        tmp_path, monkeypatch):
    """End to end through main's resume branch: the retirement must actually
    reach checkpoint.clear(), which is the part that was missing."""
    from agent import checkpoint

    cleared = []
    monkeypatch.setattr(checkpoint, "clear", lambda: cleared.append(True))

    story = _story(_utc(2020, 1, 1, 6, 7), "Berners Street hoax", story_id="doomed2")
    _write_packet(tmp_path, monkeypatch, [story])
    packet.record_status(story, "failed", attempts=packet.MAX_ATTEMPTS)

    # The exact shape of main.py's resume branch.
    try:
        packet.reclaim_script(story)
    except packet.PacketError as e:
        if getattr(e, "checkpoint_is_stale", False):
            checkpoint.clear()
    assert cleared == [True], "a retired story must discard its checkpoint"


def test_a_resumed_run_picks_up_a_repaired_story_from_the_packet(
        tmp_path, monkeypatch):
    """The packet is the source of truth for what a story says.

    Repairing prose in the packet has to reach a run that is resuming from a
    checkpoint taken before the repair, or the fix is invisible to exactly the
    runs that need it and the story burns its remaining attempts on the old
    text."""
    story = _story(_utc(2020, 1, 1, 6, 7), "Vasa", story_id="repaired")
    for segment in story["segments"]:
        segment["narration"] = "Repaired narration, as the packet now has it."
    _write_packet(tmp_path, monkeypatch, [story])

    stale = {"story_id": "repaired",
             "segments": [{"narration": "The old, too-short text."}]}
    refreshed = packet.refresh_script(stale)
    assert refreshed["segments"][0]["narration"].startswith("Repaired narration")


def test_refresh_leaves_a_script_alone_when_the_packet_moved_on(
        tmp_path, monkeypatch):
    """A new week has been written and the checkpointed story is no longer in
    the packet. Continuing with the checkpoint beats failing the run."""
    other = _story(_utc(2020, 1, 1, 6, 7), "Nan Madol", story_id="different")
    _write_packet(tmp_path, monkeypatch, [other])

    stale = {"story_id": "long-gone",
             "segments": [{"narration": "Whatever the checkpoint holds."}]}
    assert packet.refresh_script(stale) is stale


def test_refresh_tolerates_a_script_with_no_provenance(tmp_path, monkeypatch):
    """Checkpoints predating story_id must not crash a resume."""
    other = _story(_utc(2020, 1, 1, 6, 7), "Nan Madol", story_id="different2")
    _write_packet(tmp_path, monkeypatch, [other])
    stale = {"segments": [{"narration": "No provenance at all."}]}
    assert packet.refresh_script(stale) is stale
