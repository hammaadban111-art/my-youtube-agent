"""
Checkpoints have to buy resumability without ever buying a duplicate upload.

The dangerous direction is not "failed to resume" — that just costs what the
pipeline used to cost anyway. It is "resumed something it should not have",
which publishes a second copy of a video to the channel. So most of these tests
are about the four guards refusing a checkpoint, not about it being used.
"""
import json
import os

import pytest

from agent import checkpoint, config


@pytest.fixture
def fresh():
    checkpoint.clear()
    checkpoint.load()
    return checkpoint


def _write_raw(state: dict) -> None:
    os.makedirs(checkpoint.CHECKPOINT_DIR, exist_ok=True)
    with open(checkpoint.STATE_PATH, "w") as f:
        json.dump(state, f)


def _valid_state(**overrides) -> dict:
    state = {
        "schema_version": checkpoint.SCHEMA_VERSION,
        "niche": config.NICHE,
        "saved_at": checkpoint._now().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "stages": {"script": {"title": "A video"}},
    }
    state.update(overrides)
    return state


# --- Using a checkpoint -----------------------------------------------------

def test_stage_runs_the_work_once_and_reuses_it(fresh):
    calls = []

    def expensive():
        calls.append(1)
        return {"title": "A video"}

    first = checkpoint.stage("script", expensive)
    # A new process, restoring what the last one saved.
    checkpoint.load()
    second = checkpoint.stage("script", expensive)

    assert first == second == {"title": "A video"}
    assert calls == [1], "the Gemini call must not be paid for twice"


def test_a_failing_stage_saves_nothing(fresh):
    def boom():
        raise RuntimeError("503 UNAVAILABLE")

    with pytest.raises(RuntimeError):
        checkpoint.stage("script", boom)

    checkpoint.load()
    assert not checkpoint.has("script")


def test_an_earlier_stage_survives_a_later_failure(fresh):
    """The whole point: a 503 in grounding must not throw away the script."""
    checkpoint.stage("script", lambda: {"title": "A video"})
    with pytest.raises(RuntimeError):
        checkpoint.stage("grounding", lambda: (_ for _ in ()).throw(RuntimeError("503")))

    restored = checkpoint.load()
    assert restored == ["script"]
    assert checkpoint.get("script") == {"title": "A video"}


def test_clear_wipes_everything(fresh):
    checkpoint.stage("script", lambda: {"title": "A video"})
    checkpoint.clear()

    assert checkpoint.load() == []
    assert not checkpoint.has("script")
    assert not os.path.exists(checkpoint.STATE_PATH)


def test_drop_forgets_one_stage_only(fresh):
    checkpoint.stage("script", lambda: {"title": "A video"})
    checkpoint.stage("prediction", lambda: {"predicted_views": 100})

    checkpoint.drop("script")

    assert not checkpoint.has("script")
    assert checkpoint.has("prediction")
    # And it is gone from disk, not just from memory.
    checkpoint.load()
    assert not checkpoint.has("script")
    assert checkpoint.has("prediction")


# --- Guard 1: age -----------------------------------------------------------

def test_a_checkpoint_older_than_the_ttl_is_discarded():
    stale = checkpoint._now().timestamp() - (checkpoint.TTL_HOURS + 1) * 3600
    from datetime import datetime, timezone
    _write_raw(_valid_state(
        saved_at=datetime.fromtimestamp(stale, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")))

    assert checkpoint.load() == []
    assert not checkpoint.has("script")


def test_a_checkpoint_inside_the_ttl_is_kept():
    from datetime import datetime, timezone
    recent = checkpoint._now().timestamp() - (checkpoint.TTL_HOURS - 1) * 3600
    _write_raw(_valid_state(
        saved_at=datetime.fromtimestamp(recent, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")))

    assert checkpoint.load() == ["script"]


def test_a_checkpoint_with_no_timestamp_is_discarded():
    state = _valid_state()
    del state["saved_at"]
    _write_raw(state)

    assert checkpoint.load() == []


def test_a_checkpoint_with_a_garbage_timestamp_is_discarded():
    _write_raw(_valid_state(saved_at="not a date"))
    assert checkpoint.load() == []


# --- Guard 2: niche ---------------------------------------------------------

def test_a_checkpoint_from_a_different_niche_is_discarded():
    _write_raw(_valid_state(niche="something else entirely"))
    assert checkpoint.load() == []
    assert not checkpoint.has("script")


# --- Guard 3: schema and corruption -----------------------------------------

def test_a_checkpoint_from_a_different_schema_is_discarded():
    _write_raw(_valid_state(schema_version=checkpoint.SCHEMA_VERSION + 1))
    assert checkpoint.load() == []


def test_a_truncated_checkpoint_is_discarded_not_guessed_at():
    os.makedirs(checkpoint.CHECKPOINT_DIR, exist_ok=True)
    with open(checkpoint.STATE_PATH, "w") as f:
        f.write('{"schema_version": 1, "stages": {"script"')

    assert checkpoint.load() == []


def test_a_missing_checkpoint_is_a_clean_start():
    assert checkpoint.load() == []
    assert not checkpoint.has("script")


def test_writes_are_atomic(fresh):
    """The state file is renamed into place, so a run killed mid-write leaves
    the previous good checkpoint rather than a half-written one."""
    checkpoint.stage("script", lambda: {"title": "A video"})
    assert not os.path.exists(checkpoint.STATE_PATH + ".tmp")
    with open(checkpoint.STATE_PATH) as f:
        assert json.load(f)["stages"]["script"] == {"title": "A video"}


# --- The publish stage, the one that must never be replayed wrongly ---------

def test_publish_is_recorded_whole(fresh):
    published = {"video_id": "abc123", "duration_seconds": 61.4,
                 "voice": "en-US-Some-Voice", "tts_rate": "+8%"}
    checkpoint.stage("publish", lambda: published)

    checkpoint.load()
    assert checkpoint.get("publish") == published


def test_a_second_run_never_re_uploads_a_checkpointed_publish(fresh):
    uploads = []

    def do_upload():
        uploads.append("called")
        return {"video_id": "abc123", "duration_seconds": 61.4,
                "voice": "v", "tts_rate": "+8%"}

    checkpoint.stage("publish", do_upload)
    checkpoint.load()
    checkpoint.stage("publish", do_upload)

    assert uploads == ["called"], "the upload must happen exactly once"


def test_clearing_after_a_run_stops_the_next_slot_inheriting_a_video_id(fresh):
    checkpoint.stage("publish", lambda: {"video_id": "abc123"})
    checkpoint.clear()

    assert checkpoint.load() == []
    assert not checkpoint.has("publish")
