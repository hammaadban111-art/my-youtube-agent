"""
Stage checkpoints, so a failure late in the pipeline does not throw away the
expensive work that already succeeded.

WHY THIS EXISTS

run() is seven stages long and, until now, entirely all-or-nothing. A failure
in stage 2 discarded a perfectly good script from stage 1 and did the whole
thing again on the next slot. (When this was written, stage 1 was a Gemini call
out of a 20/day free-tier allowance, which is what made the waste expensive;
stage 1 is now a read from the weekly packet, so the cost is smaller but the
reasoning below still holds.) Worse, a crash anywhere between the upload and
the record write left a video PUBLISHED ON THE CHANNEL with nothing in data/
describing it. That happened three times
(2026-08-03, 08-05, 08-09); the records had to be reconstructed by hand from
Actions logs, which is what scripts/backfill_orphan_records.py exists for.

WHAT IS CHECKPOINTED

Only stages whose output is small, JSON-shaped, and expensive to recompute:

  script      the story claimed out of content/weekly_story_packet.json —
              checkpointed so a resumed run publishes the story it claimed
              rather than claiming a second one and burning a slot's content
  grounding   the Wikipedia fact-check
  prediction  cheap, but must not change between a retry and the record
  publish     the video id of an upload that ALREADY SUCCEEDED, plus the render
              facts the record needs. This is the important one: it makes the
              upload idempotent. A resumed run that finds it skips generation,
              rendering and upload entirely, writes the missing record, and
              stops — it can never publish the same video twice.

Deliberately NOT checkpointed: the narration audio, the downloaded B-roll and
the rendered mp4. They are tens of megabytes per run against a 10GB repo-wide
Actions cache limit, and re-rendering them is CPU the runner has to spare.

WHY IT IS SAFE TO RESUME FROM

A checkpoint that is reused when it should not be would publish a duplicate
video, which is worse than the failure it is trying to avoid. Four guards, all
of which must pass before anything is restored:

  1. TTL. Scheduled slots are ~5 hours apart, so a checkpoint older than
     TTL_HOURS belongs to a run whose slot has already been and gone.
  2. Niche. A checkpoint written under a different NICHE is for a different
     channel plan and is discarded rather than published.
  3. Schema version. A checkpoint written by an older layout is discarded
     rather than half-read.
  4. Caller veto (see main.py): a restored script whose topic_subject is
     already in the published history is dropped, because the only way that
     happens is a checkpoint that outlived its own video.

And it is cleared the moment a run completes, so the next slot always starts
from a blank sheet.
"""
import json
import os
import shutil
from datetime import datetime, timezone

from . import config

CHECKPOINT_DIR = os.path.join(config.WORKDIR, "checkpoint")
STATE_PATH = os.path.join(CHECKPOINT_DIR, "state.json")
# The one fact that must survive even a failure to write state.json: a video
# is live on the channel. See write_publish_receipt().
RECEIPT_NAME = "publish_receipt.json"
SCHEMA_VERSION = 1
# Scheduled slots are ~5h apart (see daily.yml's four crons), so anything older
# than this belongs to a slot that has already passed.
TTL_HOURS = 5


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _read_state() -> dict:
    if not os.path.exists(STATE_PATH):
        return {}
    try:
        with open(STATE_PATH) as f:
            state = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        # A truncated checkpoint is a checkpoint from a run the runner killed
        # mid-write. Starting clean is always correct; guessing at half a file
        # is not.
        print(f"[checkpoint] unreadable, starting clean ({type(e).__name__}: {e})")
        return {}
    if not isinstance(state, dict) or state.get("schema_version") != SCHEMA_VERSION:
        print("[checkpoint] written by a different schema version — discarding")
        return {}
    return state


def _write_state(state: dict) -> None:
    os.makedirs(CHECKPOINT_DIR, exist_ok=True)
    # Written to a sibling and renamed, so a run killed mid-write leaves the
    # previous good checkpoint rather than a truncated one.
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_PATH)


def _receipt_path() -> str:
    # Derived at call time, not bound at import: tests (and any caller that
    # relocates the checkpoint) move CHECKPOINT_DIR, and a constant computed at
    # import would keep pointing at the real workdir.
    return os.path.join(CHECKPOINT_DIR, RECEIPT_NAME)


def write_publish_receipt(published: dict, script: dict = None,
                          grounding: dict = None, prediction: dict = None) -> bool:
    """Records, in its own file, that a video is already on the channel.

    WHY THIS IS SEPARATE FROM state.json. The `publish` stage is what makes the
    upload idempotent, and it only becomes durable when record() manages to
    write state.json. If that write fails — a full disk, a killed process
    mid-rename — the video is live, nothing says so, and the NEXT run uploads
    it AGAIN. One write standing between a successful upload and a duplicate
    is one write too few.

    So the publish is written twice, to two files, through two independent
    calls: whichever survives is enough for the next run to write the record
    instead of re-uploading. It carries the script, grounding and prediction
    alongside the video id because a resume that knows only the id cannot write
    a usable record for it.

    Returns False rather than raising: failing to write the safety net must not
    be what takes down the run that just published."""
    try:
        os.makedirs(CHECKPOINT_DIR, exist_ok=True)
        payload = {"schema_version": SCHEMA_VERSION, "niche": config.NICHE,
                   "saved_at": _now().strftime("%Y-%m-%dT%H:%M:%SZ"),
                   "publish": published, "script": script,
                   "grounding": grounding, "prediction": prediction}
        tmp = _receipt_path() + ".tmp"
        with open(tmp, "w") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp, _receipt_path())
        return True
    except (OSError, TypeError, ValueError) as e:
        print(f"[checkpoint] COULD NOT WRITE THE PUBLISH RECEIPT "
              f"({type(e).__name__}: {e}). Video "
              f"{published.get('video_id')} is live and nothing durable "
              f"records it — see agent/main.py's resume path.")
        return False


def _read_receipt() -> dict:
    path = _receipt_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            receipt = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"[checkpoint] publish receipt unreadable ({type(e).__name__}: {e})")
        return {}
    if not isinstance(receipt, dict) or receipt.get("schema_version") != SCHEMA_VERSION:
        return {}
    if not isinstance(receipt.get("publish"), dict) or not receipt["publish"].get("video_id"):
        return {}
    return receipt


def _receipt_video_is_recorded(receipt: dict) -> bool:
    """Whether the receipt's live video already has its durable record.

    A publish receipt intentionally outlives the five-hour resume window.  It
    is only removed after ``_record_and_finish`` succeeds, but a runner can
    die after saving the record and before deleting the receipt.  In that
    case the receipt is stale bookkeeping, not permission to write a second
    record.  Importing here keeps checkpoint's normal write path independent
    of the record store.
    """
    video_id = (receipt.get("publish") or {}).get("video_id")
    if not video_id:
        return False
    try:
        from . import store
        return any(record.get("video_id") == video_id for record in store.all_records())
    except Exception as e:  # noqa: BLE001 - conservatively rescue if unknown
        print(f"[checkpoint] could not verify whether receipt video {video_id} "
              f"already has a record ({type(e).__name__}: {e})")
        return False


def _age_hours(state: dict) -> float | None:
    stamp = state.get("saved_at")
    if not stamp:
        return None
    try:
        saved = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None
    return (_now() - saved).total_seconds() / 3600


_state: dict = {}
_restored: list[str] = []


def load() -> list[str]:
    """Restores a usable checkpoint from a previous run, or starts a clean one.

    Returns the names of the stages that were restored (empty on a fresh
    start), so the caller can log and record them as a degradation."""
    global _state, _restored
    _restored = []
    state = _read_state()

    if state:
        age = _age_hours(state)
        if age is None:
            print("[checkpoint] no usable timestamp — discarding")
            state = {}
        elif age > TTL_HOURS:
            print(f"[checkpoint] {age:.1f}h old (limit {TTL_HOURS}h) — discarding")
            state = {}
        elif state.get("niche") != config.NICHE:
            print(f"[checkpoint] written for niche {state.get('niche')!r}, now "
                  f"{config.NICHE!r} — discarding")
            state = {}

    if state:
        _state = state
        _restored = sorted(state.get("stages", {}))
        print(f"[checkpoint] resuming: {', '.join(_restored) or 'nothing usable'} "
              f"(from {state.get('saved_at')})")
    else:
        _state = {"schema_version": SCHEMA_VERSION, "niche": config.NICHE,
                  "saved_at": _now().strftime("%Y-%m-%dT%H:%M:%SZ"), "stages": {}}

    # The receipt rescues one case: state.json was lost or never written, but
    # a video IS live.  Unlike an unfinished script checkpoint, a receipt must
    # NOT expire with its slot.  Ignoring a seven-hour-old receipt means a live
    # video has no record and the next run can upload a duplicate.  We first
    # discard the harmless case where the record already exists, then recover
    # every valid same-niche receipt regardless of age.
    if "publish" not in _state.get("stages", {}):
        receipt = _read_receipt()
        if receipt:
            if receipt.get("niche") != config.NICHE:
                print(f"[checkpoint] publish receipt is for niche "
                      f"{receipt.get('niche')!r} — ignoring")
            elif _receipt_video_is_recorded(receipt):
                print("[checkpoint] publish receipt already has a video record — clearing it")
                try:
                    os.remove(_receipt_path())
                except OSError:
                    pass
            else:
                age = _age_hours(receipt)
                if age is None:
                    print("[checkpoint] publish receipt has no usable timestamp; "
                          "recovering it anyway because its video may be live")
                elif age > TTL_HOURS:
                    print(f"[checkpoint] publish receipt is {age:.1f}h old, but its "
                          "video may still be live — recovering it instead of uploading again")
                print("[checkpoint] state.json carried no publish stage but a "
                      "publish receipt does — a video is live and unrecorded")
                stages = _state.setdefault("stages", {})
                stages["publish"] = receipt["publish"]
                # ...and everything the record needs alongside it, or the
                # resume writes a record with no script, no grounding and no
                # prediction for a video that is already on the channel.
                for name in ("script", "grounding", "prediction"):
                    if name not in stages and receipt.get(name) is not None:
                        stages[name] = receipt[name]
                _restored = sorted(set(stages))
    return list(_restored)


def restored() -> list[str]:
    """Stage names this run took from a previous run's checkpoint."""
    return list(_restored)


def has(name: str) -> bool:
    return name in _state.get("stages", {})


def get(name: str, default=None):
    return _state.get("stages", {}).get(name, default)


def record(name: str, value) -> None:
    """Saves one stage's output immediately. Called on the success path only —
    a stage that raised has no output worth resuming from."""
    _state.setdefault("stages", {})[name] = value
    _state["saved_at"] = _now().strftime("%Y-%m-%dT%H:%M:%SZ")
    _write_state(_state)


def drop(name: str) -> None:
    """Forgets one stage, so it is recomputed. For the caller's own veto —
    see the topic_subject guard in main.py."""
    global _restored
    if _state.get("stages", {}).pop(name, None) is not None:
        _write_state(_state)
    _restored = [s for s in _restored if s != name]


def stage(name: str, fn):
    """Returns the checkpointed output of `name` if a previous run got that
    far, otherwise runs fn(), saves its result and returns it."""
    if has(name):
        print(f"      [checkpoint] reusing {name} from a previous run")
        return get(name)
    value = fn()
    record(name, value)
    return value


def clear() -> None:
    """Wipes the checkpoint. Called when a run completes, so the next slot can
    never inherit a finished run's script or, worse, its video id."""
    global _state, _restored
    _state = {}
    _restored = []
    if os.path.isdir(CHECKPOINT_DIR):
        shutil.rmtree(CHECKPOINT_DIR, ignore_errors=True)


# --------------------------------------------------------- recovery pre-flight

# workdir/pending_upload, duplicated from agent/upload.py rather than imported.
# upload.py pulls in google-auth and googleapiclient; this probe has to answer
# BEFORE daily.yml has run `pip install`, so it must stay standard-library
# only. tests/test_recovery_probe.py asserts the two paths agree.
PENDING_UPLOAD_DIR = os.path.join(config.WORKDIR, "pending_upload")


def parked_bundle_waiting(pending_dir: str = None) -> bool:
    """Whether a rendered-but-unuploaded video is parked on disk.

    Deliberately a cheaper, weaker test than upload.parked_uploads(): it only
    answers "is there something here worth booting the full pipeline for", and
    a false positive costs one wasted install, not a wrong upload. upload.py
    re-validates every field before it publishes anything."""
    pending_dir = pending_dir or PENDING_UPLOAD_DIR
    if not os.path.isdir(pending_dir):
        return False
    for name in sorted(os.listdir(pending_dir)):
        if not name.endswith(".json") or name.endswith(".claimed.json"):
            continue
        try:
            with open(os.path.join(pending_dir, name)) as f:
                meta = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(meta, dict):
            continue
        video_file = meta.get("video_file")
        if not meta.get("title") or not video_file:
            continue
        if os.path.exists(os.path.join(pending_dir, video_file)):
            return True
    return False


def recovery_pending() -> bool:
    """Whether this run has unfinished work from a previous one to finish.

    daily.yml gates the whole expensive half of the job on a story being due.
    That was correct for rendering and wrong for recovery: a video that was
    uploaded but never recorded, or rendered but never uploaded, still needs
    finishing even when the packet has nothing left to publish — and its story
    is precisely the one that is no longer selectable, so `due` is false exactly
    when recovery matters most. Checked with the standard library only so it can
    run before pip install."""
    load()
    return has("publish") or parked_bundle_waiting()


if __name__ == "__main__":  # pragma: no cover - exercised via the workflow
    import sys

    if "--recovery-pending" in sys.argv:
        pending = recovery_pending()
        out = os.getenv("GITHUB_OUTPUT")
        if out:
            with open(out, "a") as fh:
                fh.write(f"pending={'true' if pending else 'false'}\n")
        print(f"recovery pending: {pending}")
        sys.exit(0)
    print("usage: python -m agent.checkpoint --recovery-pending")
    sys.exit(2)
