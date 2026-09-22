"""A checkpoint holding a story no run may render again must not cost a slot.

Two ways it went wrong before 2026-09-22:

  * reclaim_script retired a story on its third claim and raised, but the run
    still saved its checkpoint on the way out. Every run inside the 5-hour TTL
    restored the same retired story and died on it about a second in — four
    runs in a row on 2026-09-13 (Berners Street hoax).
  * mark_failed(permanent=True) retires a story on its FIRST attempt (a
    NarrationLengthError). reclaim_script only looked at the attempt count, so
    the next run re-queued that retired story from its checkpoint and rendered
    it again.

Now reclaim_script raises StaleCheckpoint for any story that is published,
failed, skipped or out of attempts, and main.run() discards the checkpoint and
claims the next due story in the same run.
"""
from unittest.mock import MagicMock

import pytest

from agent import checkpoint, main, packet

SCRIPT = {
    "story_id": "st-2026-09-13T0107Z-berners-street-hoax",
    "title": "The Berners Street Hoax",
    "description": "A hoax.",
    "topic_subject": "Berners Street hoax",
    "slot": {"iso": "2026-09-13T01:07:00Z"},
    "segments": [{"narration": "One morning in 1810 a chimney sweep knocked."}],
}
NEXT = {
    "story_id": "st-2026-09-13T0607Z-next",
    "title": "The Next Story",
    "description": "Next.",
    "topic_subject": "Next story",
    "slot": {"iso": "2026-09-13T06:07:00Z"},
    "segments": [{"narration": "Something else happened."}],
}


def _ledger_status(status, attempts):
    packet.record_status(SCRIPT, "queued")
    ledger = packet._load_ledger()
    entry = ledger["stories"][SCRIPT["story_id"]]
    entry["status"] = status
    entry["attempts"] = attempts
    packet._save_ledger(ledger)


# ------------------------------------------------------------ reclaim_script

@pytest.mark.parametrize("status,attempts", [
    ("failed", 1),        # retired on the first attempt by NarrationLengthError
    ("skipped", 0),
    ("published", 1),
    ("queued", packet.MAX_ATTEMPTS),
])
def test_a_story_that_may_not_render_again_is_a_stale_checkpoint(status, attempts):
    _ledger_status(status, attempts)
    with pytest.raises(packet.StaleCheckpoint):
        packet.reclaim_script(dict(SCRIPT))


def test_a_permanently_retired_story_is_not_requeued():
    """The exact NarrationLengthError sequence: claimed once, retired at once."""
    packet.record_status(SCRIPT, "queued")
    packet.mark_failed(SCRIPT, "NarrationLengthError: too long", permanent=True)
    with pytest.raises(packet.StaleCheckpoint):
        packet.reclaim_script(dict(SCRIPT))
    assert packet.ledger_entry(SCRIPT["story_id"])["status"] == "failed"


def test_a_live_story_is_reclaimed_and_counted():
    packet.record_status(SCRIPT, "queued")
    packet.mark_failed(SCRIPT, "transient")          # back to proposed, attempt 1
    assert packet.reclaim_script(dict(SCRIPT))["story_id"] == SCRIPT["story_id"]
    assert packet.ledger_entry(SCRIPT["story_id"])["attempts"] == 2


def test_stale_checkpoint_is_still_a_packet_error():
    """Anything catching PacketError keeps working."""
    assert issubclass(packet.StaleCheckpoint, packet.PacketError)


# ------------------------------------------------------------------ main.run

@pytest.fixture
def pipeline(monkeypatch, tmp_path):
    stubs = {
        "claim": MagicMock(return_value=dict(NEXT)),
        "grounding": MagicMock(return_value={"status": "skipped"}),
        "tts": MagicMock(return_value=[{"duration": 35.0}]),
        "upload": MagicMock(return_value="NEW_VIDEO"),
    }
    monkeypatch.setattr(main.velocity, "check",
                        lambda: {"uploads_last_24h": 0, "uploads_last_48h": 0,
                                 "override": False})
    monkeypatch.setattr(main.quota, "reconcile_uploads", lambda records: 0)
    monkeypatch.setattr(main.quota, "can_upload", lambda: True)
    monkeypatch.setattr(main.store, "all_records", lambda: [])
    monkeypatch.setattr(main.upload, "parked_uploads", lambda: [])
    monkeypatch.setattr(main.packet, "claim_script", stubs["claim"])
    monkeypatch.setattr(main.grounding, "ground_script", stubs["grounding"])
    monkeypatch.setattr(main.tts, "synthesize_all", stubs["tts"])
    monkeypatch.setattr(main.visuals, "fetch_all", lambda segments: segments)
    monkeypatch.setattr(main.assemble, "build_video", MagicMock())
    monkeypatch.setattr(main.predict, "predict", lambda subject: {
        "predicted_views": 1, "model_version": "t",
        "predicted_range": {"text": "1", "basis": "t"}})
    monkeypatch.setattr(main.upload, "upload_video", stubs["upload"])
    monkeypatch.setattr(main.upload, "build_tags", lambda script, niche: [])
    monkeypatch.setattr(main, "_record_and_finish", MagicMock())
    monkeypatch.setattr(main.config, "WORKDIR", str(tmp_path))
    return stubs


def _checkpoint_story():
    checkpoint.load()
    checkpoint.record("script", dict(SCRIPT))
    checkpoint.record("grounding", {"status": "checked", "claims_checked": 0,
                                    "supported": 0, "silent": 0,
                                    "contradicted": 0, "article": "x"})
    checkpoint.load()


def test_a_retired_checkpoint_does_not_cost_the_slot(pipeline):
    packet.record_status(SCRIPT, "queued")
    packet.mark_failed(SCRIPT, "NarrationLengthError", permanent=True)
    _checkpoint_story()

    main.run()

    # The next story was claimed and published in the SAME run...
    pipeline["claim"].assert_called_once()
    assert pipeline["upload"].call_args.args[1] == NEXT["title"]
    # ...and grounding ran for it, rather than reusing the dead story's report.
    pipeline["grounding"].assert_called_once()
    # The retired story stayed retired.
    assert packet.ledger_entry(SCRIPT["story_id"])["status"] == "failed"


def test_the_replacement_checkpoint_is_a_valid_one(pipeline):
    """discard() must leave a proper checkpoint behind (niche + schema), or the
    next run would throw away the NEW story's progress as foreign."""
    _ledger_status("failed", packet.MAX_ATTEMPTS)
    _checkpoint_story()
    main.run()
    state = checkpoint._read_state()
    assert state.get("niche") == main.config.NICHE
    assert state.get("schema_version") == checkpoint.SCHEMA_VERSION
    assert state["stages"]["script"]["story_id"] == NEXT["story_id"]


def test_a_live_checkpoint_is_still_resumed(pipeline):
    packet.record_status(SCRIPT, "queued")
    packet.mark_failed(SCRIPT, "transient")
    _checkpoint_story()

    main.run()

    pipeline["claim"].assert_not_called()
    pipeline["grounding"].assert_not_called()     # reused from the checkpoint
    assert pipeline["upload"].call_args.args[1] == SCRIPT["title"]
