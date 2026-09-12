"""
Stitches clip + narration + burned-in captions per segment into one
vertical (9:16) video ready for YouTube Shorts / faceless upload.
"""
import json
import math
import os
import random
from functools import lru_cache
import numpy as np
from PIL import Image, ImageFont
from moviepy.editor import (
    VideoFileClip, AudioFileClip, CompositeVideoClip, CompositeAudioClip,
    concatenate_videoclips, TextClip, ColorClip,
)
import moviepy.audio.fx.all as afx
import moviepy.video.fx.all as vfx
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
CAPTION_MIN_FONTSIZE = 44        # floor before legibility suffers
CAPTION_FONTSIZE_STEP = 4
CAPTION_STROKE_WIDTH = 5
# Usable text width, leaving a safe margin either side of the 1080px frame so
# a wide chunk can never clip or silently wrap to an unexpected second line.
CAPTION_MAX_WIDTH = W - 160
# Hard gap between consecutive captions. Without it, floating-point windows
# that merely touch can both be composited on the boundary frame.
CAPTION_GAP = 0.02
# Absolute floor so a clip is never zero/negative-length. Deliberately below
# one frame at 30fps: only reachable with absurdly dense input, and a caption
# too brief to see is a legibility trade-off, never an overlap.
CAPTION_MIN_TICK = 0.001
# Below ~0.6s, a viewer cannot comfortably read a 2-4 word phrase. 0.6s at 30fps is 18 frames.
CAPTION_MIN_SECONDS = 0.6
# Ceiling on a merged caption. Merging exists to stop sub-readable flashes, not
# to rebuild sentence blocks - past this the caption is its own legibility
# problem, so a merge that would exceed it is refused and the flash is accepted.
CAPTION_MAX_MERGED_WORDS = 4
CAPTION_Y = int(H * 0.72)        # integer: sub-pixel Y jitters between frames
AUDIO_EDGE_FADE = 0.015          # 15ms fade in/out matching tts.FADE_MS to prevent hard-cut pops


@lru_cache(maxsize=None)
def _font(fontsize: int) -> ImageFont.FreeTypeFont:
    """Cached: parsing the TTF is far more expensive than measuring with it,
    and measurement happens many times per caption during width fitting."""
    return ImageFont.truetype(FONT_PATH, fontsize)


def _measure_text_width(text: str, fontsize: int) -> int:
    """Real rendered width of this string in the bundled font, including the
    stroke that's drawn outside the glyphs. Measured with PIL against the
    same TTF moviepy renders with, rather than estimated from character
    count - proportional fonts make any character-count estimate wrong."""
    left, _, right, _ = _font(fontsize).getbbox(text)
    return (right - left) + 2 * CAPTION_STROKE_WIDTH


def _fit_caption_text(words: list[str]) -> list[tuple[str, int]]:
    """Splits a word group into pieces that each provably fit the frame,
    returning [(text, fontsize), ...]. Splitting is preferred over shrinking
    (more words on screen is worse than more cuts); the font only steps down
    for a single word too long to split any further."""
    text = " ".join(words)
    if _measure_text_width(text, CAPTION_FONTSIZE) <= CAPTION_MAX_WIDTH:
        return [(text, CAPTION_FONTSIZE)]

    if len(words) > 1:
        mid = len(words) // 2
        return _fit_caption_text(words[:mid]) + _fit_caption_text(words[mid:])

    size = CAPTION_FONTSIZE
    while size > CAPTION_MIN_FONTSIZE and _measure_text_width(text, size) > CAPTION_MAX_WIDTH:
        size -= CAPTION_FONTSIZE_STEP
    if _measure_text_width(text, size) <= CAPTION_MAX_WIDTH:
        return [(text, size)]

    # A single word too wide even at the minimum legible size. Shrinking
    # further would be unreadable, so break it across captions with a hyphen.
    # Pathological for this niche, but the fit guarantee has to actually hold
    # rather than "hold for realistic input" - an over-wide clip is exactly
    # the silent clipping this function exists to prevent.
    pieces, remaining = [], text
    while remaining and _measure_text_width(remaining, size) > CAPTION_MAX_WIDTH:
        cut = len(remaining) - 1
        while cut > 1 and _measure_text_width(remaining[:cut] + "-", size) > CAPTION_MAX_WIDTH:
            cut -= 1
        pieces.append((remaining[:cut] + "-", size))
        remaining = remaining[cut:]
    if remaining:
        pieces.append((remaining, size))
    return pieces


def _assert_no_overlap(chunks: list[dict]) -> None:
    """Fails loudly if two captions would ever be on screen together. This is
    a correctness guarantee, not a nicety: silently compositing two TextClips
    at the same timestamp is exactly the glitch this pipeline must never ship,
    and it is invisible in logs if we only check by eye."""
    ordered = sorted(chunks, key=lambda c: c["start"])
    for prev, nxt in zip(ordered, ordered[1:]):
        prev_end = prev["start"] + prev["duration"]
        if prev_end > nxt["start"] + 1e-9:
            raise AssertionError(
                "Caption overlap: "
                f"{prev['text']!r} ends at {prev_end:.4f}s but "
                f"{nxt['text']!r} starts at {nxt['start']:.4f}s"
            )


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
    duration is split by word count instead, which is close enough at this
    pace.

    Windows are computed CUMULATIVELY from the sentence start and the last
    chunk is clamped to the sentence end: multiplying an index by a per-word
    duration accumulates float drift, which is what lets a chunk creep past
    its neighbour's start and put two captions on screen at once. Each window
    is then closed CAPTION_GAP early so consecutive captions can't touch.
    """
    chunks = []
    for sent in sentences:
        words = sent["text"].split()
        if not words:
            continue

        # Word groups first, then each group split further if it's too wide to
        # render - so the fitted pieces, not the raw groups, are what get timed.
        pieces = []
        for i in range(0, len(words), WORDS_PER_CAPTION_CHUNK):
            pieces.extend(_fit_caption_text(words[i:i + WORDS_PER_CAPTION_CHUNK]))

        # Merge adjacent pieces whose allotted slot falls below CAPTION_MIN_SECONDS,
        # provided the merged text does not exceed 4 words (CAPTION_MAX_MERGED_WORDS).
        #
        # Evidence from real render: out of 46 caption chunks in a full video, 16 were
        # under CAPTION_MIN_SECONDS (0.6s), down to 0.277s-0.350s for single words like
        # "Radar", "observatory", "temperatures", "freezing". This happened because
        # _fit_caption_text preferred splitting over shrinking for normal layout (3 long
        # words exceeded CAPTION_MAX_WIDTH at size 72), and the merge loop previously
        # refused to merge them back at size 72.
        #
        # Normal path preference stays split-over-shrink, but for sub-0.6s flicker chunks
        # we invert this preference: if merged text exceeds CAPTION_MAX_WIDTH at current size,
        # step fontsize down in CAPTION_FONTSIZE_STEP decrements (no lower than
        # CAPTION_MIN_FONTSIZE=44) until it fits. Merge is only refused if it still exceeds
        # CAPTION_MAX_WIDTH at CAPTION_MIN_FONTSIZE.
        changed = True
        while changed and len(pieces) > 1:
            changed = False
            total_words = sum(len(text.split()) for text, _ in pieces)
            for i in range(len(pieces)):
                dur = sent["duration"] * (len(pieces[i][0].split()) / total_words)
                if dur < CAPTION_MIN_SECONDS:
                    neighbors = []
                    if i + 1 < len(pieces):
                        neighbors.append(i + 1)
                    if i - 1 >= 0:
                        neighbors.append(i - 1)

                    merged_found = False
                    for nbr in neighbors:
                        left_idx, right_idx = min(i, nbr), max(i, nbr)
                        t1, s1 = pieces[left_idx]
                        t2, s2 = pieces[right_idx]
                        merged_text = f"{t1} {t2}"
                        merged_words = len(merged_text.split())
                        if merged_words <= CAPTION_MAX_MERGED_WORDS:
                            merged_size = max(s1, s2)
                            while (merged_size > CAPTION_MIN_FONTSIZE
                                   and _measure_text_width(merged_text, merged_size) > CAPTION_MAX_WIDTH):
                                merged_size -= CAPTION_FONTSIZE_STEP
                            if _measure_text_width(merged_text, merged_size) <= CAPTION_MAX_WIDTH:
                                pieces[left_idx:right_idx + 1] = [(merged_text, merged_size)]
                                changed = True
                                merged_found = True
                                break
                    if merged_found:
                        break

        total_words = sum(len(text.split()) for text, _ in pieces)
        sent_start, sent_end = sent["start"], sent["start"] + sent["duration"]

        # The gap has to scale with how much time each caption actually gets.
        # A fixed gap subtracted from a slot smaller than the gap yields a
        # negative duration, and clamping that back up is what pushed a
        # caption past its neighbour's start (caught by fuzzing, not by eye).
        mean_slot = sent["duration"] / max(len(pieces), 1)
        gap = min(CAPTION_GAP, mean_slot * 0.25)

        cursor = sent_start
        consumed = 0
        for idx, (text, fontsize) in enumerate(pieces):
            consumed += len(text.split())
            is_last = idx == len(pieces) - 1
            # Boundary derived from the running word total, not from an
            # accumulated sum of per-chunk durations, so drift can't compound.
            boundary = (sent_end if is_last
                        else sent_start + sent["duration"] * (consumed / total_words))
            end = boundary if is_last else boundary - gap
            chunks.append({
                "text": text,
                "fontsize": fontsize,
                "start": cursor,
                "duration": max(end - cursor, CAPTION_MIN_TICK),
            })
            cursor = boundary

    _assert_no_overlap(chunks)
    return chunks


def _segment_clip(seg: dict, start_parity: int = 0) -> tuple[CompositeVideoClip, int]:
    """Returns (clip, end_parity) — end_parity feeds into the next segment's
    start_parity so the Ken Burns in/out alternation runs continuously
    across every shot in the whole video, not resetting at segment
    boundaries."""
    audio = AudioFileClip(seg["audio_path"])
    duration = seg["duration"]
    speed = float(seg.get("audio_playback_speed", 1.0) or 1.0)
    if speed != 1.0:
        audio = audio.fx(vfx.speedx, factor=speed)
    if audio.duration > duration:
        audio = audio.subclip(0, duration)
    # Apply subtle 15ms audio edge fades to eliminate hard-cut clicks or pops
    # when segment audio is truncated or concatenated.
    audio = audio.audio_fadein(AUDIO_EDGE_FADE).audio_fadeout(AUDIO_EDGE_FADE)

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
    # method="label" (not "caption") because every chunk is already measured
    # to fit CAPTION_MAX_WIDTH - "caption" re-wraps inside a fixed box, which
    # is the unexpected-wrap failure the measurement exists to prevent.
    captions = []
    for c in _caption_chunks(sentences):
        text_clip = TextClip(
            c["text"], fontsize=c["fontsize"], color="white", font=FONT_PATH,
            method="label", stroke_color="black",
            stroke_width=CAPTION_STROKE_WIDTH,
        )
        # Integer x as well as y: centring on a float half-pixel makes the
        # text shimmer between frames as it rounds differently each frame.
        x = int((W - text_clip.w) / 2)
        captions.append(
            text_clip.set_position((x, CAPTION_Y))
                     .set_start(c["start"])
                     .set_duration(c["duration"])
        )

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
    # synthesize_all() normalizes every segment to this duration.  Keep a
    # defensive assertion here because publishing a 45s file when the channel
    # contract says 35s is a product bug, not merely a display discrepancy.
    if abs(final.duration - config.VIDEO_LENGTH_SECONDS) > 0.05:
        raise RuntimeError(
            f"Assembled duration {final.duration:.2f}s does not match "
            f"VIDEO_LENGTH_SECONDS={config.VIDEO_LENGTH_SECONDS}.")

    music = _background_music(final.duration)
    if music is not None:
        final = final.set_audio(CompositeAudioClip([final.audio, music]))

    # preset="medium" is deliberate and was re-measured on 2026-09-09 rather
    # than assumed. A read-only audit suggested dropping to "fast" or
    # "veryfast" to save "2.5-5 minutes" of the ~14-minute run. Benchmarked on
    # a pipeline-shaped clip (1080x1920, 30fps, 35s, Ken Burns zoom, through
    # this very write_videofile call):
    #
    #     medium    47.0s   35.000000s   12,867,578 bytes
    #     fast      45.2s   35.000000s   11,900,309 bytes
    #     veryfast  43.6s   35.000000s    6,799,452 bytes
    #
    # Duration is preserved exactly by all three, so none of them breaks the
    # caption/audio sync the way a frame-rate change would. But the saving is
    # 1.8 seconds, not minutes: moviepy's frame compositing dominates this
    # call, not the x264 encoder, so the preset is simply not the lever it
    # looks like. "veryfast" additionally throws away 47% of the bitrate, which
    # is a poor trade on a channel whose entire product is a watchable image.
    # Left at "medium" on the evidence. Re-measure before changing it.
    final.write_videofile(out_path, fps=30, codec="libx264", audio_codec="aac",
                           threads=4, preset="medium")
    return out_path


if __name__ == "__main__":
    with open(f"{config.WORKDIR}/segments.json") as f:
        segments = json.load(f)
    out_path = f"{config.WORKDIR}/final_video.mp4"
    build_video(segments, out_path)
    print(f"Video assembled at {out_path}")
