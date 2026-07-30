"""
Stitches clip + narration + burned-in captions per segment into one
vertical (9:16) video ready for YouTube Shorts / faceless upload.
"""
import json
import math
import os
import random
import numpy as np
from PIL import Image
from moviepy.editor import (
    VideoFileClip, AudioFileClip, CompositeVideoClip, CompositeAudioClip,
    concatenate_videoclips, TextClip, ColorClip,
)
import moviepy.audio.fx.all as afx
from . import config

W, H = 1080, 1920  # vertical

# Bundled font file (rather than a font *name*) so captions render the same
# way on any machine — macOS and the GitHub Actions Ubuntu runner don't ship
# the same fonts, or register them with ImageMagick the same way.
FONT_PATH = os.path.join(os.path.dirname(__file__), "..", "assets", "DejaVuSans-Bold.ttf")

# Total scale change over a shot's duration — kept small so it reads as
# cinematic drift, not a shaky/distracting zoom.
KEN_BURNS_ZOOM = 0.06

# Bundled royalty-free ambient/tension beds (Pixabay Content License - free
# for commercial use, no attribution required; see assets/music/LICENSE.txt)
# rotated between videos rather than fetched at runtime, since there's no
# audio search API to fetch from (Pixabay's public API covers images/video
# only) and Jamendo's free-tier catalogue is non-commercial-only.
MUSIC_DIR = os.path.join(os.path.dirname(__file__), "..", "assets", "music")
MUSIC_VOLUME = 0.1  # roughly -20dB under the narration
MUSIC_FADE_SECONDS = 1.5

# Modern short-form caption style: a few words on screen at a time rather
# than a full sentence block.
WORDS_PER_CAPTION_CHUNK = 3
CAPTION_FONTSIZE = 72
CAPTION_STROKE_WIDTH = 5


def _next_subclip(source: VideoFileClip, used_seconds: float, needed: float) -> VideoFileClip:
    """Advances through source's own timeline on each reuse (rather than
    always restarting at frame 0), so a clip reused across several shots in
    the same segment shows a different moment each time, not a frozen
    repeat. Wraps/loops if the source is shorter than what's been used."""
    src_dur = source.duration
    start = used_seconds % src_dur if src_dur > 0 else 0
    if start + needed <= src_dur:
        return source.subclip(start, start + needed)
    return source.loop(duration=needed).subclip(0, needed)


def _fit_frame(clip: VideoFileClip) -> VideoFileClip:
    """Crop/resize to fill the 1080x1920 vertical frame."""
    clip = clip.resize(height=H) if clip.h < clip.w * (H / W) else clip.resize(width=W)
    return clip.crop(x_center=clip.w / 2, y_center=clip.h / 2, width=W, height=H)


def _ken_burns(clip: VideoFileClip, zoom_in: bool) -> VideoFileClip:
    """Slow, subtle zoom in/out over the shot's duration — static footage
    otherwise reads as a dead slideshow. Per-frame PIL resize+crop back to
    the exact original size (rather than moviepy's own time-varying resize,
    whose output size can't feed a fixed-size crop cleanly)."""
    duration = clip.duration or 0.01

    def effect(get_frame, t):
        progress = t / duration
        if not zoom_in:
            progress = 1 - progress
        ratio = 1 + KEN_BURNS_ZOOM * progress
        frame = Image.fromarray(get_frame(t))
        base_size = frame.size
        new_size = (math.ceil(base_size[0] * ratio), math.ceil(base_size[1] * ratio))
        frame = frame.resize(new_size, Image.LANCZOS)
        x = (new_size[0] - base_size[0]) // 2
        y = (new_size[1] - base_size[1]) // 2
        frame = frame.crop((x, y, x + base_size[0], y + base_size[1]))
        return np.array(frame)

    return clip.fl(effect)


def _caption_chunks(sentences: list[dict]) -> list[dict]:
    """Splits each sentence into a few-words-at-a-time caption chunk instead
    of showing the whole sentence at once. edge-tts emits no WordBoundary
    events on this endpoint (confirmed empty on both voices tried, only
    SentenceBoundary) so word timing isn't available - each sentence's known
    duration is split evenly by word count instead, which is close enough
    at this pace."""
    chunks = []
    for sent in sentences:
        words = sent["text"].split()
        if not words:
            continue
        per_word = sent["duration"] / len(words)
        for i in range(0, len(words), WORDS_PER_CAPTION_CHUNK):
            group = words[i:i + WORDS_PER_CAPTION_CHUNK]
            chunks.append({
                "text": " ".join(group),
                "start": sent["start"] + i * per_word,
                "duration": per_word * len(group),
            })
    return chunks


def _segment_clip(seg: dict, start_parity: int = 0) -> tuple[CompositeVideoClip, int]:
    """Returns (clip, end_parity) — end_parity feeds into the next segment's
    start_parity so the Ken Burns in/out alternation runs continuously
    across every shot in the whole video, not resetting at segment
    boundaries."""
    audio = AudioFileClip(seg["audio_path"])
    duration = seg["duration"]
    if audio.duration > duration:
        audio = audio.subclip(0, duration)

    # Several short shots per segment (a faster cut rhythm) rather than one
    # clip held for the whole segment. clip_paths may hold fewer distinct
    # clips than there are shots - shots then reuse clips round-robin.
    shots = seg.get("shots") or [{"start": 0.0, "duration": duration}]
    clip_paths = seg.get("clip_paths") or [seg["clip_path"]]
    sources = [VideoFileClip(p).without_audio() for p in clip_paths]
    used = [0.0] * len(sources)

    shot_clips = []
    for i, shot in enumerate(shots):
        src_idx = i % len(sources)
        source = sources[src_idx]
        shot_clip = _fit_frame(_next_subclip(source, used[src_idx], shot["duration"]))
        used[src_idx] += shot["duration"]
        zoom_in = (start_parity + i) % 2 == 0
        shot_clip = _ken_burns(shot_clip.set_duration(shot["duration"]), zoom_in)
        shot_clips.append(shot_clip)
    end_parity = (start_parity + len(shots)) % 2
    video = concatenate_videoclips(shot_clips, method="compose").set_duration(duration)

    # A few words on screen at a time, timed to the narration - not a full
    # sentence block held for its whole duration.
    sentences = seg.get("sentences") or [{"text": seg["narration"], "start": 0, "duration": duration}]
    captions = [
        TextClip(c["text"], fontsize=CAPTION_FONTSIZE, color="white", font=FONT_PATH,
                  method="caption", size=(W - 100, None), stroke_color="black",
                  stroke_width=CAPTION_STROKE_WIDTH)
        .set_position(("center", H * 0.72))
        .set_start(c["start"])
        .set_duration(c["duration"])
        for c in _caption_chunks(sentences)
    ]

    return CompositeVideoClip([video, *captions]).set_audio(audio).set_duration(duration), end_parity


def _background_music(duration: float) -> AudioFileClip | None:
    """Picks a random bundled track, loops/trims it to the video's exact
    length, and fades it in/out - mixed in well under the narration so it
    reads as a bed, not competing audio. Returns None if no tracks are
    bundled (never blocks a build over a missing asset)."""
    if not os.path.isdir(MUSIC_DIR):
        return None
    tracks = [f for f in os.listdir(MUSIC_DIR) if f.endswith(".mp3")]
    if not tracks:
        return None

    music = AudioFileClip(os.path.join(MUSIC_DIR, random.choice(tracks)))
    if music.duration < duration:
        music = music.fx(afx.audio_loop, duration=duration)
    else:
        music = music.subclip(0, duration)
    return (music.volumex(MUSIC_VOLUME)
                 .audio_fadein(MUSIC_FADE_SECONDS)
                 .audio_fadeout(MUSIC_FADE_SECONDS))


def build_video(segments: list[dict], out_path: str) -> str:
    clips = []
    parity = 0
    for seg in segments:
        clip, parity = _segment_clip(seg, parity)
        clips.append(clip)
    final = concatenate_videoclips(clips, method="compose")

    music = _background_music(final.duration)
    if music is not None:
        final = final.set_audio(CompositeAudioClip([final.audio, music]))

    final.write_videofile(out_path, fps=30, codec="libx264", audio_codec="aac",
                           threads=4, preset="medium")
    return out_path


if __name__ == "__main__":
    with open(f"{config.WORKDIR}/segments.json") as f:
        segments = json.load(f)
    out_path = f"{config.WORKDIR}/final_video.mp4"
    build_video(segments, out_path)
    print(f"Video assembled at {out_path}")
