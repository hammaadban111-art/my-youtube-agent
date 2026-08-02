"""
Runs the full pipeline end to end:
script -> fact-check -> voice -> visuals -> assemble -> upload -> record -> dashboard.

This is the single entry point GitHub Actions calls on a schedule. The
5-hour measurement is deliberately NOT here — it runs in agent/followup.py
on its own hourly schedule so this workflow never sits idle burning CI time.
"""
import json
from . import (assemble, config, dashboard, followup, grounding, history,
               predict, resilience, script_writer, store, tts, upload,
               velocity, visuals)


def run():
    resilience.reset()

    # Before anything expensive: refuse to add to a burst. Raises and ends the
    # run rather than rendering a video it would then decline to publish.
    pace = velocity.check()
    print(f"[0/7] Upload pace OK: {pace['uploads_last_24h']} in 24h, "
          f"{pace['uploads_last_48h']} in 48h"
          + (" (override active)" if pace["override"] else ""))

    print(f"[1/7] Writing script for niche: {config.NICHE}")
    script = script_writer.generate_script()

    print("[2/7] Fact-checking claims against Wikipedia...")
    report = grounding.ground_script(script)
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
    # upload's own outcome.
    prediction = predict.predict(script.get("topic_subject", ""))
    # Printed as a range, not a point. The point estimate is still what gets
    # recorded and scored; a bare number in the log reads as a forecast when
    # the backtested error runs from 3x to 199x.
    band = prediction["predicted_range"]
    print(f"      predicted views at {store.MEASURE_AFTER_HOURS}h: "
          f"{band['text']} ({prediction['model_version']}, {band['basis']})")

    print("[6/7] Uploading to YouTube...")
    video_id = upload.upload_video(video_path, script["title"], script["description"])
    print(f"      Done: https://youtube.com/watch?v={video_id}")

    print("[7/7] Recording...")
    record = store.new_record(video_id, script)
    record["privacy_status"] = config.PRIVACY_STATUS
    record["niche"] = config.NICHE
    record["prediction"] = prediction
    record["grounding"] = report
    record["script"] = {
        "segments": script.get("segments", []),
        "segment_count": len(script.get("segments", [])),
        "word_count": sum(len(s["narration"].split()) for s in script.get("segments", [])),
        "duration_seconds": round(sum(s.get("duration", 0) for s in segments), 1),
        # The voice actually used, not the configured one - they differ when
        # the TTS fallback fired.
        "voice": tts.active_voice(),
        "tts_rate": tts.TTS_RATE,
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
    history.append_entry(script["title"])

    # Refreshes every OTHER tracked video's likes/views too, not just the one
    # that just went up - the brand-new record is still under MEASURE_AFTER_HOURS
    # old so measurable_records() correctly leaves it for follow-up's first
    # reading rather than measuring it seconds after upload. Also resolves
    # any video that just crossed the 30-day freeze line.
    print("[7/7] Refreshing analytics for all tracked videos...")
    followup.sweep()

    dashboard.build()


if __name__ == "__main__":
    run()
