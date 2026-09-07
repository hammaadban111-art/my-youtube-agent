"""
Stage checkpoints, so a failure late in the pipeline does not throw away the
expensive work that already succeeded.

WHY THIS EXISTS

run() is seven stages long and, until now, entirely all-or-nothing. A 503 in
stage 2 discarded a perfectly good script from stage 1 and paid for it again
from scratch on the next slot — out of a Gemini free tier that allows 20 calls
a day against a pipeline that runs four times a day plus re-asks. Worse, a
crash anywhere between the upload and the record write left a video PUBLISHED
ON THE CHANNEL with nothing in data/ describing it. That happened three times
(2026-08-03, 08-05, 08-09); the records had to be reconstructed by hand from
Actions logs, which is what scripts/backfill_orphan_records.py exists for.

WHAT IS CHECKPOINTED

Only stages whose output is small, JSON-shaped, and expensive to recompute:

  script      one Gemini call, plus up to one corrective re-ask
  grounding   Wikipedia fact-check, a second Gemini call
  prediction  cheap, but must not change between a retry and the record
  publish     the video id of an upload that ALREADY SUCCEEDED, plus the render
              facts the record needs. This is the important one: it makes the
              upload idempotent. A resumed run that finds it skips generation,
              rendering and upload entirely, writes the missing record, and
              stops — it can never publish the same video twice.

Deliberately NOT checkpointed: the narration audio, the downloaded B-roll and
the rendered mp4. They are tens of megabytes per run against a 10GB repo-wide
Actions cache limit, and re-rendering them is CPU the runner has to spare —
unlike Gemini calls, which come out of a hard daily allowance.

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
