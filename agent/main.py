"""
Runs the full pipeline end to end: script -> voice -> visuals -> assemble -> upload.
This is the single entry point GitHub Actions calls on a schedule.
"""
import json
from . import config, script_writer, tts, visuals, assemble, upload, history


def run():
    print(f"[1/5] Writing script for niche: {config.NICHE}")
    script = script_writer.generate_script()
    with open(f"{config.WORKDIR}/script.json", "w") as f:
        json.dump(script, f, indent=2)

    print("[2/5] Synthesizing voice...")
    segments = tts.synthesize_all(script)

    print("[3/5] Fetching stock visuals...")
    segments = visuals.fetch_all(segments)

    print("[4/5] Assembling video...")
    video_path = f"{config.WORKDIR}/final_video.mp4"
    assemble.build_video(segments, video_path)

    print("[5/5] Uploading to YouTube...")
    video_id = upload.upload_video(video_path, script["title"], script["description"])
    history.append_entry(script["title"])
    print(f"Done: https://youtube.com/watch?v={video_id}")


if __name__ == "__main__":
    run()
