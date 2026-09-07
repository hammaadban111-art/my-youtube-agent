"""
Runs the full pipeline end to end:
script -> fact-check -> voice -> visuals -> assemble -> upload -> record -> dashboard.

This is the single entry point GitHub Actions calls on a schedule. The
5-hour measurement is deliberately NOT here — it runs in agent/followup.py
on its own hourly schedule so this workflow never sits idle burning CI time.
"""
import json
from . import (assemble, checkpoint, config, dashboard, followup, grounding,
               history, notify, predict, quota, resilience, script_writer,
               store, tts, upload, velocity, visuals)


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
    store.save_record(record)
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

    dashboard.build()


def run():
    resilience.reset()
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

    print(f"[1/7] Writing script for niche: {config.NICHE}")
    script = checkpoint.stage("script", script_writer.generate_script)

    print("[2/7] Fact-checking claims against Wikipedia...")
    report = checkpoint.stage("grounding", lambda: grounding.ground_script(script))
    if report.get("status") == "checked":
        print(f"      {report['claims_checked']} claims vs '{report.get('article')}' — "
              f"{report['supported']} supported, {report['silent']} not covered, "
              f"{report['contradicted']} contradicted")
        script = grounding.apply_corrections(script, report)
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
                                       script["description"], tags=tags, script=script)
        return {
            "video_id": video_id,
            "duration_seconds": round(sum(s.get("duration", 0) for s in segments), 1),
            # The voice actually used, not the configured one - they differ when
            # the TTS fallback fired.
            "voice": tts.active_voice(),
            "tts_rate": tts.TTS_RATE,
        }

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
        stage = str(e).split("]")[0] + "]" if str(e).startswith("[") else None
        notify.alert(
            f"Run failed{' at ' + stage if stage else ''}: {type(e).__name__}",
            str(e),
        )
        raise
