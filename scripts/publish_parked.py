#!/usr/bin/env python3
"""
Publishes videos that were rendered but never uploaded.

When an upload fails, agent/upload.py parks the finished .mp4 and its metadata
in workdir/pending_upload/ and daily.yml preserves that directory as an
Actions artifact. Nothing ever picked those up again - `park_for_next_run`'s
docstring promised "a later run can publish it" but no code path read the
directory, so six videos sat in artifacts through the 2026-08-07 token outage
with no way back. This is that missing path.

Pacing is the whole point. Six videos is 9,600 quota units against a
10,000/day cap, so publishing them in one go is not merely unwise, it is
arithmetically impossible alongside the scheduled uploads. Every video is
checked against the velocity guardrail and the quota reserve immediately
before it goes up, and the run stops cleanly at the first refusal rather than
forcing anything through.

Usage:
  # download the parked artifacts first
  gh run download <run_id> --dir parked/run_<run_id>

  python scripts/publish_parked.py parked/ --dry-run
  python scripts/publish_parked.py parked/ --max 2
  python scripts/publish_parked.py parked/ --max 2 --subjects subjects.json
"""
import argparse
import glob
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from agent import config, history, predict, quota, store, upload, velocity

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def discover(parked_dir: str) -> list[dict]:
    """Every parked bundle under `parked_dir`, oldest first. A bundle is a
    <stamp>.json next to the <stamp>.mp4 it names."""
    found = []
    for meta_path in glob.glob(os.path.join(parked_dir, "**", "*.json"), recursive=True):
        try:
            with open(meta_path) as f:
                meta = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if not meta.get("title") or not meta.get("video_file"):
            continue
        video_path = os.path.join(os.path.dirname(meta_path), meta["video_file"])
        if not os.path.exists(video_path):
            print(f"  ! {os.path.basename(meta_path)}: {meta['video_file']} missing, skipping")
            continue
        meta["_video_path"] = video_path
        meta["_meta_path"] = meta_path
        found.append(meta)
    return sorted(found, key=lambda m: m.get("parked_at", ""))


def subject_of(meta: dict, overrides: dict) -> str:
    """The topic_subject for a parked video.

    Bundles parked before 2026-08-08 hold no script at all, so the subject has
    to come from outside - `overrides` maps a parked_at stamp (or the run id in
    its path) to a subject recovered by hand, e.g. from the Actions log, where
    the grounding step prints the article it checked against. Without it the
    video can still be published, but it carries no subject for duplicate
    detection or predict.py to work with."""
    if meta.get("topic_subject"):
        return meta["topic_subject"]
    for key in (meta.get("parked_at", ""), meta.get("_meta_path", "")):
        for k, v in overrides.items():
            if k and key and k in key:
                return v
    return ""


def already_published_stamps() -> set:
    """The parked_at stamp of every bundle that has already been published.

    This, not the subject, is the identity of a parked video. Subject-only
    deduplication published the same bundle twice on 2026-08-09 (Iz-ULqySj2A
    then 3uRPvUI9vaI): that video's grounding had failed, so it carried no
    subject, so there was nothing for the subject check to match and the
    guard silently passed. A bundle is the same bundle regardless of whether
    anyone could work out what it was about."""
    stamps = set()
    for record in store.all_records():
        stamp = (record.get("recovered_from_parked") or {}).get("parked_at")
        if stamp:
            stamps.add(stamp)
    return stamps


def drop_already_handled(items: list[dict]) -> tuple[list[dict], list[tuple]]:
    """Removes parked videos that are already published - either this exact
    bundle, or a different bundle covering the same subject.

    The subject case still matters on its own: a failed run records nothing,
    so the NEXT scheduled run had no idea the topic had been used and could
    pick it again. Two of the six recovered on 2026-08-08 were both 'Yamal
    Peninsula' for exactly that reason."""
    published_stamps = already_published_stamps()
    published_subjects = history.load_recent_subjects(limit=200)
    keep, dropped = [], []
    for item in items:
        stamp = item.get("parked_at")
        if stamp and stamp in published_stamps:
            dropped.append((item, f"this exact bundle ({stamp}) is already published"))
            continue
        subject = item.get("_subject", "")
        if subject:
            clash = history.is_duplicate_subject(subject, published_subjects)
            if clash:
                dropped.append((item, f"repeats already-published '{clash}'"))
                continue
            published_subjects.append(subject)
        if stamp:
            published_stamps.add(stamp)
        keep.append(item)
    return keep, dropped


def _record_for(video_id: str, meta: dict, subject: str) -> dict:
    script = {"title": meta["title"], "description": meta.get("description", ""),
              "topic_subject": subject}
    record = store.new_record(video_id, script)
    record["privacy_status"] = config.PRIVACY_STATUS
    record["niche"] = config.NICHE
    record["prediction"] = predict.predict(subject)
    record["grounding"] = meta.get("script", {}).get("grounding", {}) or {}
    stored_script = meta.get("script") or {}
    segments = stored_script.get("segments", [])
    record["script"] = {
        "segments": segments,
        "segment_count": len(segments),
        "word_count": sum(len(s.get("narration", "").split()) for s in segments),
        "voice": stored_script.get("voice"),
        "tts_rate": stored_script.get("tts_rate"),
        "target_length_seconds": config.VIDEO_LENGTH_SECONDS,
    }
    # Honest about what this record is and is not. A recovered video that was
    # parked before the script started being saved has no segments, no hook and
    # no grounding report, and the dashboard should say so rather than showing
    # blanks that look like a bug.
    record["recovered_from_parked"] = {
        "parked_at": meta.get("parked_at"),
        "reason": meta.get("reason"),
        "published_at": store.iso(datetime.now(timezone.utc)),
        "script_metadata_preserved": bool(segments),
        "subject_source": "parked bundle" if meta.get("topic_subject") else (
            "recovered manually" if subject else "unknown"),
    }
    if not segments:
        record["degradations"] = [{
            "stage": "parked-recovery",
            "what": "the parked bundle predates script preservation, so segments, "
                    "hook and grounding are gone",
            "instead": "published with title, description and subject only",
        }]
    return record


def publish_one(meta: dict, subject: str, dry_run: bool) -> str | None:
    title = meta["title"]
    description = meta.get("description", "")
    script = {"title": title, "description": description, "topic_subject": subject}
    tags = upload.build_tags(script, config.NICHE)

    if dry_run:
        print(f"    DRY RUN - would upload {os.path.basename(meta['_video_path'])} "
              f"({os.path.getsize(meta['_video_path']) // 1048576}MB), tags={tags[:5]}")
        return None

    video_id = upload.upload_video(meta["_video_path"], title, description,
                                   tags=tags, script=script)
    record = _record_for(video_id, meta, subject)
    store.save_record(record)
    history.append_entry(title, subject)
    print(f"    published https://youtube.com/watch?v={video_id}")
    return video_id


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Publish rendered-but-never-uploaded videos, respecting the guardrails.")
    parser.add_argument("parked_dir", help="Directory holding downloaded parked artifacts")
    parser.add_argument("--max", type=int, default=None,
                        help="Stop after this many uploads (the guardrails may stop it sooner)")
    parser.add_argument("--subjects", default=None,
                        help="JSON map of {parked_at-or-run-id: topic_subject} for bundles "
                             "parked before scripts were preserved")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be published; upload nothing, spend nothing")
    args = parser.parse_args()

    overrides = {}
    if args.subjects:
        with open(args.subjects) as f:
            overrides = json.load(f)

    items = discover(args.parked_dir)
    if not items:
        print(f"No parked videos found under {args.parked_dir}")
        return 1
    for item in items:
        item["_subject"] = subject_of(item, overrides)

    print(f"Found {len(items)} parked video(s):")
    for item in items:
        print(f"  {item.get('parked_at')}  {item['title'][:52]!r}  "
              f"subject={item['_subject'] or 'UNKNOWN'}")

    items, dropped = drop_already_handled(items)
    for item, why in dropped:
        print(f"\n  SKIPPING {item['title'][:52]!r} - {why}")

    quota.reconcile_uploads(store.all_records())
    print(f"\nQuota now: {quota.units_used_today()}/{quota.DAILY_CAP} units used.")

    published = 0
    attempted = 0
    for item in items:
        if args.max is not None and attempted >= args.max:
            print(f"\nStopping: reached --max {args.max}.")
            break
        attempted += 1

        print(f"\n[{attempted}/{len(items)}] {item['title'][:60]!r}")

        try:
            pace = velocity.check()
        except velocity.VelocityBlocked as e:
            print(f"    STOP - upload pace guardrail: {e}")
            break
        # The reserve line, not the hard cap: recovering a parked video is
        # discretionary, and must not eat the allowance the scheduled uploads
        # and the follow-up reads still need today.
        # But uploads no longer consume units, they consume slots. We just need
        # to ensure there are slots available.
        if not args.dry_run and not quota.can_upload():
            print(f"    STOP - quota slots: {quota.uploads_today()} of "
                  f"{quota.UPLOADS_PER_DAY_CAP} upload slots used today. "
                  f"Run again after the Pacific midnight reset.")
            break
        print(f"    pace {pace['uploads_last_24h']}/{velocity.MAX_UPLOADS_24H} in 24h; "
              f"slots {quota.uploads_today()}/{quota.UPLOADS_PER_DAY_CAP}")

        try:
            if publish_one(item, item["_subject"], args.dry_run):
                published += 1
        except Exception as e:  # noqa: BLE001 - one bad video must not strand the rest
            print(f"    FAILED: {type(e).__name__}: {e}")
            break

    remaining = len(items) - published
    print(f"\nPublished {published}. {remaining} still parked.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
