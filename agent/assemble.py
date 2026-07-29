"""
Stitches clip + narration + burned-in captions per segment into one
vertical (9:16) video ready for YouTube Shorts / faceless upload.
"""
import json
import os
from moviepy.editor import (
    VideoFileClip, AudioFileClip, CompositeVideoClip, concatenate_videoclips,
    TextClip, ColorClip,
)
from . import config

W, H = 1080, 1920  # vertical

# Bundled font file (rather than a font *name*) so captions render the same
# way on any machine — macOS and the GitHub Actions Ubuntu runner don't ship
# the same fonts, or register them with ImageMagick the same way.
FONT_PATH = os.path.join(os.path.dirname(__file__), "..", "assets", "DejaVuSans-Bold.ttf")


def _segment_clip(seg: dict) -> CompositeVideoClip:
    video = VideoFileClip(seg["clip_path"]).without_audio()
    audio = AudioFileClip(seg["audio_path"])
    duration = seg["duration"]
    if audio.duration > duration:
        audio = audio.subclip(0, duration)

    # Crop/resize to fill 1080x1920 vertical frame
    video = video.resize(height=H) if video.h < video.w * (H / W) else video.resize(width=W)
    video = video.crop(x_center=video.w / 2, y_center=video.h / 2, width=W, height=H)
    video = video.loop(duration=duration) if video.duration < duration else video.subclip(0, duration)

    # One caption per sentence, each shown only while that sentence is
    # actually being spoken — not the whole segment's text at once.
    sentences = seg.get("sentences") or [{"text": seg["narration"], "start": 0, "duration": duration}]
    captions = [
        TextClip(sent["text"], fontsize=52, color="white", font=FONT_PATH,
                  method="caption", size=(W - 120, None), stroke_color="black", stroke_width=3)
        .set_position(("center", H * 0.72))
        .set_start(sent["start"])
        .set_duration(sent["duration"])
        for sent in sentences
    ]

    return CompositeVideoClip([video, *captions]).set_audio(audio).set_duration(duration)


def build_video(segments: list[dict], out_path: str) -> str:
    clips = [_segment_clip(seg) for seg in segments]
    final = concatenate_videoclips(clips, method="compose")
    final.write_videofile(out_path, fps=30, codec="libx264", audio_codec="aac",
                           threads=4, preset="medium")
    return out_path


if __name__ == "__main__":
    with open(f"{config.WORKDIR}/segments.json") as f:
        segments = json.load(f)
    out_path = f"{config.WORKDIR}/final_video.mp4"
    build_video(segments, out_path)
    print(f"Video assembled at {out_path}")
