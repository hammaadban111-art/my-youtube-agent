"""
The resume path in agent/main.py: a run that already uploaded but never wrote
the record.

Three videos went up with no record in data/ on 2026-08-03, 08-05 and 08-09,
because the process died between the upload call and store.save_record and the
next run started again from stage 1. That is the worst outcome the pipeline
has: the slot and the quota are spent, the video is public, and nothing knows
it exists — the prediction cannot be scored and duplicate detection cannot see
the subject, which is how the Yamal Peninsula repeat got published.

main.run() now checks for a checkpointed "publish" before any guardrail and
finishes the record instead of starting over.
"""
from unittest.mock import MagicMock

import pytest

from agent import checkpoint, main


PUBLISHED = {"video_id": "vid_from_previous_run", "duration_seconds": 61.4,
             "voice": "en-US-ChristopherNeural", "tts_rate": "+8%"}
SCRIPT = {
    "title": "The Somerton Man",
    "description": "A body on a beach.",
    "topic_subject": "Tamam Shud",
    "segments": [{"narration": "A man was found dead. Nobody knew his name.",
                  "duration": 6.0}],
    "hook_candidates": [{"text": "A man was found dead"}],
    "hook_choice": {"chosen_index": 0},
}
GROUNDING = {"status": "checked", "claims_checked": 3, "supported": 3,
             "silent": 0, "contradicted": 0, "article": "Tamam Shud case"}
PREDICTION = {"predicted_views": 120, "model_version": "v3",
              "predicted_range": {"text": "40-360", "basis": "backtest"}}


@pytest.fixture
def stranded(monkeypatch):
    """A checkpoint in exactly the state the August failures left behind: the
    video is live, the record was never written."""
    checkpoint.clear()
    checkpoint.load()
    checkpoint.record("script", SCRIPT)
    checkpoint.record("grounding", GROUNDING)
    checkpoint.record("prediction", PREDICTION)
    checkpoint.record("publish", PUBLISHED)
    checkpoint.load()

    stubs = {
        "velocity": MagicMock(),
        "quota": MagicMock(),
        "script_writer_generate": MagicMock(),
        "upload": MagicMock(),
        "tts": MagicMock(),
        "visuals": MagicMock(),
        "assemble": MagicMock(),
        "grounding": MagicMock(),
        "predict": MagicMock(),
        "store_save": MagicMock(),
        "history_append": MagicMock(),
        "followup": MagicMock(),
        "dashboard": MagicMock(),
    }
    monkeypatch.setattr(main.velocity, "check", stubs["velocity"])
    monkeypatch.setattr(main.quota, "can_upload", stubs["quota"])
    monkeypatch.setattr(main.packet, "claim_script", stubs["script_writer_generate"])
    monkeypatch.setattr(main.packet, "mark_published", MagicMock())
    monkeypatch.setattr(main.upload, "upload_video", stubs["upload"])
    monkeypatch.setattr(main.tts, "synthesize_all", stubs["tts"])
    monkeypatch.setattr(main.visuals, "fetch_all", stubs["visuals"])
    monkeypatch.setattr(main.assemble, "build_video", stubs["assemble"])
    monkeypatch.setattr(main.grounding, "ground_script", stubs["grounding"])
    monkeypatch.setattr(main.predict, "predict", stubs["predict"])
    monkeypatch.setattr(main.store, "save_record", stubs["store_save"])
    monkeypatch.setattr(main.store, "new_record",
                        lambda video_id, script: {"video_id": video_id})
    monkeypatch.setattr(main.history, "append_entry", stubs["history_append"])
    monkeypatch.setattr(main.followup, "sweep", stubs["followup"])
    monkeypatch.setattr(main.dashboard, "build", stubs["dashboard"])
    return stubs


def test_resume_writes_the_missing_record(stranded):
    main.run()

    stranded["store_save"].assert_called_once()
    record = stranded["store_save"].call_args.args[0]
    assert record["video_id"] == "vid_from_previous_run"
    assert record["prediction"] == PREDICTION
    assert record["grounding"] == GROUNDING
    # The render facts come from the checkpoint, describing the video that is
    # actually live — not from a rerun of the TTS stage.
    assert record["script"]["voice"] == "en-US-ChristopherNeural"
    assert record["script"]["duration_seconds"] == 61.4


def test_resume_never_uploads_again(stranded):
    main.run()
    stranded["upload"].assert_not_called()


def test_resume_regenerates_nothing(stranded):
    """No packet claim, no TTS, no Pexels fetch, no render. The work is done."""
    main.run()

    stranded["script_writer_generate"].assert_not_called()
    stranded["grounding"].assert_not_called()
    stranded["predict"].assert_not_called()
    stranded["tts"].assert_not_called()
    stranded["visuals"].assert_not_called()
    stranded["assemble"].assert_not_called()


def test_resume_is_not_blocked_by_the_pace_or_quota_guards(stranded):
    """Both guards refuse work that would ADD an upload. This upload already
    happened, so refusing here would abandon the record for a second time —
    and it is the record, not the video, that is missing."""
    stranded["velocity"].side_effect = AssertionError("pace guard must not run")
    stranded["quota"].return_value = False  # "no slots left today"

    main.run()

    stranded["store_save"].assert_called_once()


def test_resume_appends_the_subject_to_history(stranded):
    """Duplicate detection reads this. Skipping it is how the Yamal Peninsula
    repeat got published."""
    main.run()
    stranded["history_append"].assert_called_once_with("The Somerton Man", "Tamam Shud")


def test_resume_clears_the_checkpoint_so_the_next_slot_starts_clean(stranded):
    main.run()

    assert checkpoint.load() == []
    assert not checkpoint.has("publish")


def test_resume_flags_itself_as_a_degradation(stranded):
    """A run that silently behaved differently is worse than one that failed.
    The dashboard shows degradations, so this one is visible."""
    main.run()

    stages = [d["stage"] for d in main.resilience.degradations()]
    assert "resumed-publish" in stages
