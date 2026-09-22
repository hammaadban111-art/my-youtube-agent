#!/usr/bin/env python3
"""
Replaces a published video: re-renders it from its stored re-use bundle,
uploads the new copy, then deletes the old one.

This is the destructive half of the dashboard's "Replace" button. The button
itself cannot do any of this — the dashboard is a static page served from a
separate PUBLIC repo (scripts/publish_dashboard.sh) with no credentials and no
backend, so all it can do is send the operator to this workflow behind
GitHub's own login. Everything that actually costs quota or deletes anything
happens here, and is checked here.

ORDER MATTERS: upload first, delete second. The other way round is one failed
render away from having deleted a video and having nothing to put in its
place. Upload-then-delete's worst case is a duplicate that can be removed by
hand — recoverable, unlike a deletion.

Usage:
  python scripts/reupload_video.py <video_id> --confirm <video_id>
  python scripts/reupload_video.py <video_id> --dry-run
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from agent import (assemble, config, gha, history, predict, quota,
                   resilience, script_writer, store, tts, upload, velocity,
                   visuals)
from scripts.export_reuse_bundle import export_bundle_for_record, find_record_by_id

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
REUSE_DIR = os.path.join(ROOT, "data", "reuse")
# One delete + one insert. The upload costs an upload slot, not Data API units,
# so the units budgeted here are just for the delete.
REUPLOAD_UNITS = quota.UNITS_PER_DELETE
REPLACEMENT_RECEIPT_PREFIX = "replacement-"


class PreflightFailed(RuntimeError):
    """Raised before anything is rendered, uploaded or deleted."""


def _replacement_receipt_path(old_video_id: str, new_video_id: str) -> str:
    """A durable journal beside failure artifacts, not in git-tracked data.

    The replacement upload is live before the old video is deleted.  If the
    runner dies in either following step, the repository alone cannot tell
    whether it has two live videos or one live video with no record.  Keeping
    a small transaction receipt with the Action artifact makes that state
    visible and recoverable instead of silently treating the replacement as
    complete.
    """
    os.makedirs(upload.PENDING_DIR, exist_ok=True)
    return os.path.join(upload.PENDING_DIR,
                        f"{REPLACEMENT_RECEIPT_PREFIX}{old_video_id}-{new_video_id}.json")


def _write_replacement_receipt(old_record: dict, new_video_id: str, script: dict,
                               segments: list, bundle: dict, state: str,
                               error: str = "") -> str:
    path = _replacement_receipt_path(str(old_record.get("video_id") or "unknown"),
                                     new_video_id)
    payload = {
        "kind": "replacement_transaction",
        "schema_version": 1,
        "old_video_id": old_record.get("video_id"),
        "new_video_id": new_video_id,
        "state": state,
        "updated_at": store.iso(datetime.now(timezone.utc)),
        # The exact inputs needed to reconstruct the records after a runner
        # failure. This is metadata only; the replacement render itself is
        # already live, so copying the mp4 again would not make recovery safer.
        "old_record": old_record,
        "script": script,
        "segments": segments,
        "bundle": bundle,
    }
    if error:
        payload["error"] = error[:500]
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, path)
    return path


def _update_replacement_receipt(path: str, *, state: str, error: str = "") -> None:
    try:
        with open(path) as f:
            payload = json.load(f)
        if not isinstance(payload, dict):
            return
        payload["state"] = state
        payload["updated_at"] = store.iso(datetime.now(timezone.utc))
        if error:
            payload["error"] = error[:500]
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp, path)
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        print(f"[reupload] could not update replacement receipt {path}: "
              f"{type(exc).__name__}: {exc}")


def _alert_replacement_journal(path: str, state: str, error: Exception) -> None:
    message = (
        f"A replacement transaction stopped in state '{state}':\n"
        f"{type(error).__name__}: {error}\n\n"
        f"Its receipt is saved at {path} and is attached to the failed Action run. "
        "Do not rerun the replacement blindly: first check both YouTube video IDs, "
        "then reconcile the records from this receipt.")
    gha.error("Replacement needs manual reconciliation", message)


def load_bundle(video_id: str) -> dict:
    path = os.path.join(REUSE_DIR, f"{video_id}.json")
    if not os.path.exists(path):
        raise PreflightFailed(
            f"No re-use bundle at data/reuse/{video_id}.json. Export one first: "
            f"python scripts/export_reuse_bundle.py {video_id}")
    with open(path) as f:
        return json.load(f)


def rebuild_description(bundle: dict) -> str:
    """The YouTube description was never persisted on any record (see
    docs/session-handoff-2026-08-05.md), so a re-upload has to write a new one.
    Built from the bundle's own text rather than asking a model for it: a
    re-render's whole point is costing no model calls, and a description
    assembled from the real narration is closer to the original than a
    re-imagined one would be.

    The hashtags matter beyond the description itself — upload.build_tags()
    reads them, so they are what carries the subject into the video's tags."""
    subject = (bundle.get("topic_subject") or "").strip()
    niche = (bundle.get("niche") or "").strip()
    segments = (bundle.get("script") or {}).get("segments") or []
    opening = (segments[0].get("narration") or "").strip() if segments else ""

    def _tag(text: str) -> str:
        return "#" + "".join(ch for ch in text.title() if ch.isalnum())

    hashtags = [_tag(subject)] if subject else []
    hashtags += [_tag(w) for w in niche.split() if len(w) > 3][:2]
    hashtags.append("#shorts")
    # Deduped in order, so a one-word niche can't produce "#Shorts #shorts".
    seen, unique = set(), []
    for tag in hashtags:
        if len(tag) > 1 and tag.lower() not in seen:
            seen.add(tag.lower())
            unique.append(tag)

    body = opening if opening else bundle.get("title", "")
    return f"{body}\n\n{' '.join(unique)}"


def build_script(bundle: dict, regenerate_visuals: bool) -> dict:
    """The script dict the render pipeline expects, rebuilt from the bundle."""
    segments = []
    for seg in (bundle.get("script") or {}).get("segments") or []:
        segments.append({
            "narration": seg.get("narration", ""),
            "visual_query": (seg.get("visual_keywords") or "").strip(),
            "visual_fallback": "",
        })
    if not segments:
        raise PreflightFailed("The bundle has no segments to render.")

    if regenerate_visuals:
        print(f"[reupload] Stored visual queries are flagged legacy-generic — "
              f"rebuilding {len(segments)} of them from the stored narration "
              f"(no model call)")
        fresh = script_writer.derive_visual_queries(
            bundle.get("topic_subject") or bundle.get("title", ""),
            [s["narration"] for s in segments])
        for seg, q in zip(segments, fresh):
            seg["visual_query"] = q["visual_query"]
            seg["visual_fallback"] = q["visual_fallback"]
        resilience.record_degradation(
            "reupload-visuals",
            "the stored visual queries predate the anchored-visual fix "
            "(visual_queries_are_legacy_generic)",
            "rebuilt them from the subject and narration instead of "
            "reproducing the generic footage")

    return {
        "title": bundle.get("title", ""),
        "description": rebuild_description(bundle),
        "topic_subject": bundle.get("topic_subject", ""),
        "segments": segments,
    }


def preflight(video_id: str, bundle: dict, dry_run: bool = False) -> tuple:
    """Every reason to refuse, checked before a single second is rendered.

    A dry run spends no quota, deletes nothing and publishes nothing, so the
    three live-run gates below are reported instead of enforced — otherwise a
    rehearsal on a token that has not been re-minted yet could never get as far
    as the render it is meant to rehearse."""
    blockers, warnings = [], []

    def gate(reason: str) -> None:
        (warnings if dry_run else blockers).append(reason)

    found = find_record_by_id(video_id, base_dir=ROOT)
    # export_reuse_bundle returns both the record and its source path. Keep
    # the record only: passing the tuple into _record_replacement later would
    # crash after the old live video had already been deleted.
    old = found[0] if found is not None else None
    if old is None:
        # Not gated by dry_run: without a record there is nothing to replace.
        blockers.append(f"no stored record for {video_id}")

    # Book today's uploads before reading the ledger — otherwise this asks a
    # number that can be thousands of units short (see agent/quota.py).
    quota.reconcile_uploads(store.all_records())
    used = quota.units_used_today()
    if not quota.has_headroom_for(REUPLOAD_UNITS):
        gate(f"not enough YouTube quota headroom: {used} of {quota.DAILY_CAP} units "
             f"already used today and a replace costs {REUPLOAD_UNITS} "
             f"({quota.UNITS_PER_DELETE} delete). "
             f"A replace is discretionary, so it has to leave the 30% reserve the "
             f"day's scheduled reads run on — that ceiling is "
             f"{int(quota.DAILY_CAP * 0.7) - REUPLOAD_UNITS} units used.")
    if not quota.can_upload():
        gate(f"not enough upload slots: {quota.uploads_today()} of {quota.UPLOADS_PER_DAY_CAP} already used.")

    delete_scope, why = upload.delete_capable_scope()
    if delete_scope is None:
        gate(f"cannot delete the old video: {why}")

    pace = None
    try:
        pace = velocity.check()
    except velocity.VelocityBlocked as e:
        gate(str(e))

    if blockers:
        raise PreflightFailed("\n  - ".join(["Refusing to replace this video:"] + blockers))
    for warning in warnings:
        print(f"[reupload] DRY RUN would have been blocked: {warning}")

    print(f"[reupload] Preflight {'(dry run) ' if dry_run else ''}OK — quota "
          f"{used}/{quota.DAILY_CAP} used, {REUPLOAD_UNITS} needed; "
          f"delete scope {'present' if delete_scope else 'MISSING'}; "
          f"{pace['uploads_last_24h'] if pace else '?'} uploads in 24h")
    return old, delete_scope


def replace(video_id: str, dry_run: bool = False) -> str | None:
    resilience.reset()
    bundle = load_bundle(video_id)
    old_record, delete_scope = preflight(video_id, bundle, dry_run=dry_run)

    legacy = bool((bundle.get("reuse") or {}).get("visual_queries_are_legacy_generic"))
    script = build_script(bundle, regenerate_visuals=legacy)

    print(f"[reupload] 1/5 Synthesizing voice for {len(script['segments'])} segments...")
    segments = tts.synthesize_all(script)

    print("[reupload] 2/5 Re-downloading B-roll from Pexels...")
    # The bundle never stored Pexels clip ids, so every clip is fetched again.
    # Free-tier, but real: ~3 clips per segment.
    segments = visuals.fetch_all(segments)

    print("[reupload] 3/5 Assembling video...")
    video_path = f"{config.WORKDIR}/final_video.mp4"
    assemble.build_video(segments, video_path)

    if dry_run:
        print(f"[reupload] DRY RUN — rendered {video_path}, uploading and deleting "
              f"nothing. Would have spent {REUPLOAD_UNITS} quota units and 1 upload slot.")
        return None

    print(f"[reupload] 4/5 Uploading the replacement...")
    tags = upload.build_tags(script, bundle.get("niche") or config.NICHE)
    new_video_id = upload.upload_video(
        video_path, script["title"], script["description"], tags=tags)
    print(f"[reupload]     new video: https://youtube.com/watch?v={new_video_id}")

    receipt_path = _write_replacement_receipt(
        old_record, new_video_id, script, segments, bundle,
        state="replacement_uploaded_old_not_deleted")

    # Only now is the old one expendable.
    print(f"[reupload] 5/5 Deleting {video_id} ({quota.UNITS_PER_DELETE} units)...")
    try:
        upload.delete_video(video_id, scope=delete_scope)
    except Exception as exc:
        _update_replacement_receipt(
            receipt_path, state="replacement_uploaded_delete_unknown",
            error=f"{type(exc).__name__}: {exc}")
        _alert_replacement_journal(receipt_path, "replacement_uploaded_delete_unknown", exc)
        raise

    try:
        _record_replacement(old_record, new_video_id, script, segments, bundle)
    except Exception as exc:
        _update_replacement_receipt(
            receipt_path, state="old_deleted_records_not_written",
            error=f"{type(exc).__name__}: {exc}")
        _alert_replacement_journal(receipt_path, "old_deleted_records_not_written", exc)
        raise
    try:
        os.remove(receipt_path)
    except OSError as exc:
        # The records are already durable. Keeping a stale receipt is safer
        # than turning a successful replacement into a failed workflow.
        print(f"[reupload] completed, but could not remove journal {receipt_path}: {exc}")
    print(f"[reupload] Done. {video_id} -> {new_video_id}. "
          f"Quota used today: {quota.units_used_today()}/{quota.DAILY_CAP}")
    return new_video_id


def _record_replacement(old_record: dict, new_video_id: str,
                        script: dict, segments: list, bundle: dict) -> None:
    """Writes the new video's record and marks the old one as replaced. The old
    record is kept, not deleted: its measurements are what made the case for
    replacing it, and predict.py's accuracy history should not silently lose
    entries."""
    record = store.new_record(new_video_id, script)
    record["privacy_status"] = config.PRIVACY_STATUS
    record["niche"] = bundle.get("niche") or config.NICHE
    record["prediction"] = predict.predict(script.get("topic_subject", ""))
    record["grounding"] = bundle.get("grounding") or {}
    record["script"] = {
        "segments": [{"narration": s["narration"],
                      "visual_keywords": s.get("visual_query", "")}
                     for s in script["segments"]],
        "segment_count": len(script["segments"]),
        "word_count": sum(len(s["narration"].split()) for s in script["segments"]),
        "duration_seconds": round(sum(s.get("duration", 0) for s in segments), 1),
        "voice": tts.active_voice(),
        "tts_rate": tts.TTS_RATE,
        "target_length_seconds": config.VIDEO_LENGTH_SECONDS,
    }
    record["degradations"] = resilience.degradations()
    record["replaces"] = {
        "video_id": old_record.get("video_id"),
        "views_at_replacement": (old_record.get("latest_measurement") or {}).get("actual_views"),
        "replaced_at": store.iso(datetime.now(timezone.utc)),
    }
    store.save_record(record)

    old_record["replaced_by"] = new_video_id
    old_record["replaced_at"] = store.iso(datetime.now(timezone.utc))
    # A deleted video cannot be measured again; freezing it stops the follow-up
    # job asking YouTube about an id that no longer exists on every run.
    store.freeze_record(old_record)
    store.save_record(old_record)

    history.append_entry(script["title"], script.get("topic_subject"))
    # The replacement gets its own bundle, so it is replaceable in turn.
    export_bundle_for_record(record, store.record_path(record), base_dir=ROOT)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Delete a published video and re-upload it from its stored re-use bundle.")
    parser.add_argument("video_id", help="The video to replace")
    parser.add_argument("--confirm", default="",
                        help="Must equal the video id. Guards against a stray run.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Render only: no upload, no delete, no quota spent")
    args = parser.parse_args()

    if not args.dry_run and args.confirm != args.video_id:
        print(f"Refusing: --confirm must be the video id ({args.video_id}). "
              "This deletes a live video and cannot be undone.", file=sys.stderr)
        return 2

    try:
        replace(args.video_id, dry_run=args.dry_run)
    except PreflightFailed as e:
        print(f"[reupload] {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
