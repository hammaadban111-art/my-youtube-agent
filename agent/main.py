"""
Runs the full pipeline end to end:
script -> fact-check -> voice -> visuals -> assemble -> upload -> record.

This is the single entry point GitHub Actions calls on a schedule. The
5-hour measurement is deliberately NOT here — it runs in agent/followup.py
on its own hourly schedule so this workflow never sits idle burning CI time.
"""
import json
import os
import signal
import sys
from datetime import datetime, timezone

from . import (assemble, checkpoint, config, followup, grounding,
               history, notify, packet, predict, quota, resilience,
               script_writer, store, tts, upload, velocity, visuals)

# The story this run claimed out of the weekly packet, so the top-level
# handler can move it back out of "queued" when the run dies. Set by run(),
# read only by __main__ below.
_claimed: dict | None = None


def _release_claim(reason: str) -> None:
    """Puts the claimed story back so the next slot can retry it."""
    if not _claimed:
        return
    try:
        packet.mark_failed(_claimed, reason)
    except Exception as ledger_error:  # noqa: BLE001
        print(f"[packet] could not release the claimed story: "
              f"{type(ledger_error).__name__}: {ledger_error}")


def _on_cancelled(signum, _frame):
    """GitHub cancels a queued or running job with SIGTERM.

    Without this the run dies holding its claim: the story stays "queued" with
    its attempt already counted, and nothing says why. The concurrency group
    (repo-data-writers) is NOT first-in-first-out — a newer run displaces an
    older PENDING one — so cancellation is a normal event here, not an
    exceptional one, and it has to leave the ledger in a state the next run can
    act on."""
    print(f"[main] cancelled (signal {signum}) — releasing the claimed story "
          "before exiting so the next slot can retry it")
    _release_claim(f"run cancelled by signal {signum}")
    sys.exit(143)


def _recover_parked_video() -> bool:
    """Publishes one video an earlier run rendered but could not upload.

    park_for_next_run() has promised since it was written that "a later run can
    publish it", and no later run ever did — six videos sat in artifacts
    through the 2026-08-07 outage. This is that path, running before a new
    story is claimed: recovering an already-rendered video is strictly cheaper
    than rendering another one, and publishing both in one run would be two
    uploads in a slot that budgets for one.

    Returns True when a video was published, which ends the run. Every guard a
    normal upload passes applies here too, and a bundle is claimed on disk
    BEFORE the upload so a crash mid-publish cannot be retried blindly into a
    duplicate."""
    parked = upload.parked_uploads()
    if not parked:
        return False

    meta = parked[0]
    if meta.get("requires_manual_reconciliation"):
        message = (
            f"Parked upload {meta.get('bundle_id') or meta.get('parked_at')} may already "
            "be live because YouTube lost the insert response. It is deliberately "
            "not being auto-published. Check the channel, then reconcile the bundle "
            "with scripts/publish_parked.py --force if needed.")
        print(f"[recover] {message}")
        notify.alert("A parked upload needs reconciliation before recovery", message)
        return False
    print(f"[recover] {len(parked)} rendered video(s) are parked; publishing "
          f"the oldest ({meta.get('parked_at')}) instead of rendering a new one")

    bundle_id = meta.get("bundle_id") or meta.get("parked_at")
    recovered_ids = {
        recovered.get("bundle_id") or recovered.get("parked_at")
        for record in store.all_records()
        for recovered in [record.get("recovered_from_parked") or {}]
    }
    if bundle_id and bundle_id in recovered_ids:
        print("[recover] that bundle is already published — leaving it alone")
        return False

    if not upload.claim_parked(meta):
        # An earlier attempt wrote the claim and then died. It may have got as
        # far as a live video, so a blind retry is how one render becomes two
        # videos. Say so and leave it for scripts/publish_parked.py.
        message = (
            f"A parked video from {meta.get('parked_at')} was already claimed "
            "by an earlier recovery attempt that did not finish. It may or may "
            "not be live on the channel. Check the channel, then publish it "
            "deliberately with scripts/publish_parked.py --force or delete the "
            ".claimed.json marker.")
        print(f"[recover] {message}")
        notify.alert("A parked video needs a human before it can be recovered",
                     message)
        return False

    velocity.check()
    quota.reconcile_uploads(store.all_records())
    if not quota.can_upload():
        raise RuntimeError(
            f"[0/7] Not enough YouTube quota left today to recover a parked "
            f"video: {quota.uploads_today()} of {quota.UPLOADS_PER_DAY_CAP} "
            "upload slots already used.")

    stored_script = meta.get("script") or {}
    script = {
        "title": meta["title"],
        "description": meta.get("description", ""),
        "topic_subject": meta.get("topic_subject", "")
                         or stored_script.get("topic_subject", ""),
        "segments": stored_script.get("segments", []),
        "story_id": meta.get("story_id", "") or stored_script.get("story_id", ""),
        "packet_id": meta.get("packet_id", "") or stored_script.get("packet_id", ""),
        "sources": meta.get("sources", []) or stored_script.get("sources", []),
        "slot": stored_script.get("slot", {}),
    }
    grounding_report = meta.get("grounding") or stored_script.get("grounding") or {}
    prediction = meta.get("prediction") or predict.predict(
        script.get("topic_subject", ""))
    tags = upload.build_tags(script, config.NICHE)
    video_id = upload.upload_video(meta["_video_path"], script["title"],
                                   script["description"], tags=tags,
                                   script=script, grounding=grounding_report)
    print(f"[recover] published https://youtube.com/watch?v={video_id}")

    # Recovery has the same narrow crash window as a normal upload.  Persist a
    # receipt before writing the record so a killed runner resumes bookkeeping
    # instead of seeing an already-claimed render and starting a new story.
    checkpoint.write_publish_receipt(
        {
            "video_id": video_id,
            "duration_seconds": sum(s.get("duration", 0) for s in script["segments"]),
            "voice": stored_script.get("voice"),
            "tts_rate": stored_script.get("tts_rate"),
        }, script, grounding_report,
        prediction,
    )

    record = store.new_record(video_id, script)
    record["privacy_status"] = config.PRIVACY_STATUS
    record["niche"] = config.NICHE
    record["prediction"] = prediction
    record["grounding"] = grounding_report
    segments = script["segments"]
    record["script"] = {
        "segments": segments,
        "segment_count": len(segments),
        "word_count": sum(len(s.get("narration", "").split()) for s in segments),
        "voice": stored_script.get("voice"),
        "tts_rate": stored_script.get("tts_rate"),
        "target_length_seconds": config.VIDEO_LENGTH_SECONDS,
    }
    record["recovered_from_parked"] = {
        "bundle_id": bundle_id,
        "parked_at": meta.get("parked_at"),
        "reason": meta.get("reason"),
        "published_at": store.iso(datetime.now(timezone.utc)),
        "script_metadata_preserved": bool(segments),
        "subject_source": "parked bundle" if meta.get("topic_subject") else "unknown",
    }
    record["degradations"] = resilience.degradations()
    store.save_record(record)
    packet.mark_published(script, video_id)
    history.append_entry(script["title"], script.get("topic_subject"))
    # The record and story ledger are durable now; do not let the emergency
    # receipt make a later run re-record this same live video.
    checkpoint.clear()
    resilience.record_degradation(
        "parked-recovery",
        f"a video rendered at {meta.get('parked_at')} had never been uploaded",
        f"published it as {video_id} instead of rendering a new one")
    try:
        os.remove(meta["_video_path"])
        os.remove(meta["_meta_path"])
        if meta.get("_claim_path"):
            os.remove(meta["_claim_path"])
    except OSError as e:  # noqa: BLE001 - the record is written; cleanup is cosmetic
        print(f"[recover] could not clean up the parked files: {e}")
    return True


def _record_and_finish(script, report, prediction, published):
    """Stage 7 on its own, so the resume path can reach it without re-running
    any of the six stages in front of it.

    `published` is the checkpointed result of the upload: the video id plus the
    render facts (voice, rate, duration) that only the rendering stages knew.
    On a normal run it was produced seconds ago; on a resumed run it belongs to
    a video that is already live on the channel and has been waiting for its
    record."""
    print("[7/7] Recording...")
    video_id = published["video_id"]
    record = store.new_record(video_id, script)
    record["privacy_status"] = config.PRIVACY_STATUS
    record["niche"] = config.NICHE
    record["prediction"] = prediction
    record["grounding"] = report
    record["script"] = {
        "segments": script.get("segments", []),
        "segment_count": len(script.get("segments", [])),
        "word_count": sum(len(s["narration"].split()) for s in script.get("segments", [])),
        "duration_seconds": published["duration_seconds"],
        # The voice actually used, not the configured one - they differ when
        # the TTS fallback fired.
        "voice": published["voice"],
        "tts_rate": published["tts_rate"],
        "target_length_seconds": config.VIDEO_LENGTH_SECONDS,
    }
    # Hook selection is kept so the dashboard can show which of the three
    # candidate openings won, and so a later review can correlate hook style
    # with retention.
    record["hook"] = {
        "candidates": script.get("hook_candidates", []),
        "choice": script.get("hook_choice", {}),
        "opening_line": script_writer._first_sentence(
            script["segments"][0]["narration"]) if script.get("segments") else "",
    }
    record["degradations"] = resilience.degradations()
    record["story_id"] = script.get("story_id", "")
    record["packet_id"] = script.get("packet_id", "")
    record["sources"] = script.get("sources", [])
    store.save_record(record)
    # The ledger is what the next weekly packet reads to know this subject is
    # spent. Written straight after the record and before anything that can
    # fail, for the same reason the record is: a story published but not
    # marked published is a story the planner can propose again.
    packet.mark_published(script, video_id)
    # The subject goes in alongside the title: it is what duplicate detection
    # compares on the next run (agent/history.py).
    history.append_entry(script["title"], script.get("topic_subject"))

    # The video is now on the channel AND described in data/. Nothing after
    # this point is worth resuming, and keeping the checkpoint would let the
    # next slot inherit this video's id. Cleared before the analytics sweep so
    # even a crash in there cannot leave a reusable publish record behind.
    checkpoint.clear()

    # Refreshes every OTHER tracked video's likes/views too, not just the one
    # that just went up - the brand-new record is still under MEASURE_AFTER_HOURS
    # old so measurable_records() correctly leaves it for follow-up's first
    # reading rather than measuring it seconds after upload. Also resolves
    # any video that just crossed the 30-day freeze line.
    print("[7/7] Refreshing analytics for all tracked videos...")
    followup.sweep()



def run():
    global _claimed
    _claimed = None
    resilience.reset()
    # Opens the retry budget for this run. Caps the pathological tail — 24 of
    # September's 48 upload runs took 15+ minutes and burned 610 of the month's
    # 806 billed upload minutes, almost all of it asleep between retries rather
    # than rendering. See agent/resilience.py for why this cannot interrupt a
    # healthy run.
    granted = resilience.start_budget()
    if granted:
        print(f"[0/7] Retry budget for this run: {granted / 60:.0f} minutes.")
    notify.reset_reported()
    # GitHub can terminate a writer while a newer queued job is waiting.  A
    # running process gets SIGTERM first, which is our chance to return its
    # ledger claim before the runner disappears.  signal.signal is only legal
    # from the main thread, so keep local/library callers usable too.
    try:
        signal.signal(signal.SIGTERM, _on_cancelled)
    except (ValueError, AttributeError):
        pass
    restored = checkpoint.load()
    if restored:
        resilience.record_degradation(
            "resumed-run",
            f"a previous run left a checkpoint holding: {', '.join(restored)}",
            "reused that work instead of paying for it again",
        )

    # THE RESUME PATH, taken before any guardrail runs. A checkpointed
    # "publish" means the video is ALREADY on the channel and only its record
    # is missing — the exact state that stranded three videos in August. The
    # pace and quota guards below both refuse work that would ADD an upload,
    # and applying them here would refuse to write down one that already
    # happened, which is backwards: the slot is spent either way.
    if checkpoint.has("publish"):
        published = checkpoint.get("publish")
        print(f"[resume] a previous run already uploaded "
              f"https://youtube.com/watch?v={published['video_id']} but never "
              f"recorded it — writing the record and stopping.")
        resilience.record_degradation(
            "resumed-publish",
            f"video {published['video_id']} was uploaded by an earlier run that "
            "died before writing its record",
            "wrote the missing record instead of re-uploading or losing it",
        )
        _record_and_finish(checkpoint.get("script"), checkpoint.get("grounding"),
                           checkpoint.get("prediction"), published)
        return

    # A rendered upload that failed or was interrupted is recovered before a
    # new packet is claimed.  One scheduled slot may publish one video, not a
    # recovery AND a new render.  claim_parked() makes a crash in this path
    # require a deliberate human decision rather than blindly duplicating it.
    if _recover_parked_video():
        return

    # Before anything expensive: refuse to add to a burst. Raises and ends the
    # run rather than rendering a video it would then decline to publish.
    pace = velocity.check()
    print(f"[0/7] Upload pace OK: {pace['uploads_last_24h']} in 24h, "
          f"{pace['uploads_last_48h']} in 48h"
          + (" (override active)" if pace["override"] else ""))

    # Book any upload the ledger missed before reading it. Without this the
    # check below is answered from a number that can be several thousand units
    # short of the truth.
    quota.reconcile_uploads(store.all_records())
    if not quota.can_upload():
        raise RuntimeError(
            f"[0/7] Not enough YouTube quota left today for an upload: "
            f"{quota.uploads_today()} of {quota.UPLOADS_PER_DAY_CAP} upload slots already used. "
            "Refusing before rendering rather than after."
        )
    # The scheduled upload is the thing this whole pipeline exists to do, so it
    # gets refused only when it genuinely cannot succeed (no slots left).
    # Discretionary spend (final refreshes, re-uploads) is what the units reserve protects.
    print(f"[0/7] Quota OK: {quota.uploads_today()}/{quota.UPLOADS_PER_DAY_CAP} upload slots used, "
          f"{quota.units_used_today()}/{quota.DAILY_CAP} Data API units used.")

    # Guard 4 from agent/checkpoint.py: a restored script whose subject is
    # already published means the checkpoint outlived its own video (its run
    # uploaded, recorded, and then failed to clear). Reusing it would publish
    # a straight duplicate, so it is dropped and the script regenerated.
    if checkpoint.has("script"):
        stale_subject = (checkpoint.get("script") or {}).get("topic_subject", "")
        if stale_subject and history.is_duplicate_subject(
                stale_subject, history.published_subjects()):
            print(f"[checkpoint] restored script is about {stale_subject!r}, which "
                  "is already published — discarding it and starting fresh")
            for name in ("script", "grounding", "prediction"):
                checkpoint.drop(name)

    # [1/7] is no longer a generation step. The story was researched and
    # written days ago by the weekly Claude task and committed to
    # content/weekly_story_packet.json; this claims the one whose slot has
    # come round. There is deliberately no fallback generator: if the packet
    # cannot supply a story, packet.PacketError ends the run and nothing is
    # published. See agent/packet.py.
    print(f"[1/7] Claiming this slot's story from the weekly packet "
          f"(niche: {config.NICHE})")
    script_was_restored = checkpoint.has("script")
    script = checkpoint.stage("script", packet.claim_script)
    if script_was_restored:
        # checkpoint.stage intentionally skips its function on a resume.  A
        # resumed render must nevertheless consume another attempt, otherwise
        # a bad story can remain attempt 1 forever and wedge the queue.
        script = packet.reclaim_script(script)
        checkpoint.record("script", script)
    _claimed = script

    print("[2/7] Fact-checking claims against Wikipedia...")
    report = checkpoint.stage("grounding", lambda: grounding.ground_script(script))
    if report.get("status") == "checked":
        print(f"      {report['claims_checked']} claims vs '{report.get('article')}' — "
              f"{report['supported']} supported, {report['silent']} not covered, "
              f"{report['contradicted']} contradicted")
        script = grounding.apply_corrections(script, report)
        # The resume checkpoint must contain the spoken, corrected version;
        # leaving the pre-correction script here made a later retry render the
        # factually rejected narration again.
        checkpoint.record("script", script)

        # Applied AFTER corrections, because a contradiction that got corrected
        # is not a contradiction any more — the corrected sentence is what gets
        # spoken. What this catches is the residue: claims the checker rejected
        # and could not rewrite. See grounding.enforce_publication_policy for
        # the policy and why the final segment is treated differently.
        uncorrected = grounding.enforce_publication_policy(report)
        for verdict in uncorrected:
            resilience.record_degradation(
                "uncorrected-contradiction",
                f"segment {verdict.get('segment_index')} claim "
                f"{str(verdict.get('claim'))[:120]!r} contradicts the source "
                "and no correction was available",
                "published anyway — the claim is supporting detail, not the "
                "payoff line, and the run had already paid for the render",
            )
        if uncorrected:
            notify.alert(
                f"{len(uncorrected)} uncorrected contradiction(s) published",
                f"Story {script.get('topic_subject')!r} went out with "
                f"{len(uncorrected)} claim(s) the fact-checker rejected and "
                "could not rewrite:\n\n"
                + "\n".join(f"  segment {v.get('segment_index')}: "
                            f"{str(v.get('claim'))[:200]}\n"
                            f"    note: {str(v.get('note'))[:200]}"
                            for v in uncorrected)
                + "\n\nNone of them is the final segment — that case blocks the "
                  "upload instead. Worth reading: repeated misses here usually "
                  "mean the source article is a poor match for the subject.")
    else:
        print(f"      grounding {report.get('status')}: {report.get('error', '')}")
    with open(f"{config.WORKDIR}/script.json", "w") as f:
        json.dump(script, f, indent=2)

    print("[3/7] Synthesizing voice...")
    segments = tts.synthesize_all(script)

    print("[4/7] Fetching stock visuals...")
    segments = visuals.fetch_all(segments)

    print("[5/7] Assembling video...")
    video_path = f"{config.WORKDIR}/final_video.mp4"
    assemble.build_video(segments, video_path)

    # Predicted before upload so the number can never be influenced by the
    # upload's own outcome — and checkpointed for the same reason, so a resumed
    # run scores the video against the forecast that was made before it went
    # up, not one recomputed afterwards from a model that has since moved.
    prediction = checkpoint.stage(
        "prediction", lambda: predict.predict(script.get("topic_subject", "")))
    # Printed as a range, not a point. The point estimate is still what gets
    # recorded and scored; a bare number in the log reads as a forecast when
    # the backtested error runs from 3x to 199x.
    band = prediction["predicted_range"]
    print(f"      predicted views at {store.MEASURE_AFTER_HOURS}h: "
          f"{band['text']} ({prediction['model_version']}, {band['basis']})")

    print("[6/7] Uploading to YouTube...")
    tags = upload.build_tags(script, config.NICHE)

    def _publish():
        """Uploads, then captures the facts about the rendered video that only
        exist in this process. Checkpointed as ONE unit with the video id: a
        run that resumes past this point has to describe the video that is
        actually live, and re-deriving the voice or the duration from a rerun
        of the TTS stage would describe a different render."""
        video_id = upload.upload_video(video_path, script["title"],
                                       script["description"], tags=tags, script=script,
                                       grounding=report, prediction=prediction)
        published = {
            "video_id": video_id,
            "duration_seconds": round(sum(s.get("duration", 0) for s in segments), 1),
            # The voice actually used, not the configured one - they differ when
            # the TTS fallback fired.
            "voice": tts.active_voice(),
            "tts_rate": tts.active_rate(),
        }
        # This receipt is written before checkpoint.stage() writes state.json.
        # If the process dies in that tiny window, the next run records this
        # live video rather than uploading a second copy.
        checkpoint.write_publish_receipt(published, script, report, prediction)
        return published

    published = checkpoint.stage("publish", _publish)
    print(f"      Done: https://youtube.com/watch?v={published['video_id']}")

    _record_and_finish(script, report, prediction, published)


if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        # Alerts and then RE-RAISES: the workflow must still go red. This is
        # the only place a scheduled run's failure becomes visible to a human
        # without them going and looking - four days of outage
        # (2026-08-07 to 08-11) passed unnoticed because nothing did this.
        #
        # The step numbers in run()'s own messages ("[3/7] Synthesizing
        # voice...") are the cheapest stage marker available, and the guardrail
        # failures raise with one already in the text.
        # Release the claimed story before alerting, so the next slot can
        # retry it rather than finding it stuck in "queued" forever. Failing
        # to write the ledger must never mask the original error.
        if _claimed:
            try:
                # A wrong-length script fails identically on every retry, so it
                # is retired now instead of costing two more scheduled slots.
                packet.mark_failed(
                    _claimed, f"{type(e).__name__}: {e}",
                    permanent=isinstance(e, tts.NarrationLengthError))
            except Exception as ledger_error:  # noqa: BLE001
                print(f"[packet] could not release the claimed story: "
                      f"{type(ledger_error).__name__}: {ledger_error}")
        stage = str(e).split("]")[0] + "]" if str(e).startswith("[") else None
        reason = f"{type(e).__name__}: {e}"
        if not notify.already_reported(reason):
            notify.alert(
                f"Run failed{' at ' + stage if stage else ''}: {type(e).__name__}",
                str(e),
            )
        raise
