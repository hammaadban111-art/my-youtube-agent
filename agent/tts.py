"""
Converts each script segment to speech with edge-tts (free, no API key,
uses Microsoft's neural voices) and returns per-segment audio durations
so the video assembler can time visuals correctly.

Each segment's narration is synthesized ONE SENTENCE AT A TIME rather than
as a single multi-sentence call: edge-tts inserts its own inconsistent
pauses (measured up to ~0.9s) and varies pacing unpredictably when given
several sentences at once. Synthesizing sentence-by-sentence lets us
control pacing directly with a single, consistent gap applied everywhere
a cut happens — between sentences within a segment and between segments.
"""
import asyncio
import json
import os
import re
import edge_tts
from moviepy.editor import AudioFileClip
from pydub import AudioSegment
from pydub.silence import detect_leading_silence
from . import config

TTS_RATE = "+8%"
# Stricter (more negative) than before so trimming doesn't clip into the
# soft onset of real speech (e.g. consonants like "s"/"th"/"f").
SILENCE_THRESH_DB = -50.0
# Total silence at every cut point — sentence-to-sentence AND segment-to-
# segment. Applied as HALF_GAP on each side of every chunk, so two adjacent
# chunks' pads sum to exactly GAP_MS at their shared boundary.
GAP_MS = 280
HALF_GAP = GAP_MS // 2
# Short fade so the splice from true silence into speech doesn't click.
FADE_MS = 15
EXPORT_BITRATE = "128k"


def _split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return [p for p in parts if p]


async def _synthesize_raw(text: str, out_path: str):
    communicate = edge_tts.Communicate(text, config.VOICE, rate=TTS_RATE)
    await communicate.save(out_path)


def _trim_and_fade(audio: AudioSegment) -> AudioSegment:
    """Removes leading/trailing silence conservatively, then fades the cut
    edges so splicing against pure silence doesn't produce a click."""
    start = detect_leading_silence(audio, silence_threshold=SILENCE_THRESH_DB)
    end = detect_leading_silence(audio.reverse(), silence_threshold=SILENCE_THRESH_DB)
    trimmed = audio[start: len(audio) - end]
    if len(trimmed) < 200:  # safety net: don't trim to near-nothing
        trimmed = audio
    return trimmed.fade_in(FADE_MS).fade_out(FADE_MS)


def _synthesize_segment(narration: str, out_path: str, tmp_prefix: str):
    sentences = _split_sentences(narration) or [narration]
    pad = AudioSegment.silent(duration=HALF_GAP)
    combined = AudioSegment.empty()
    for i, sentence in enumerate(sentences):
        raw_path = f"{tmp_prefix}_{i}.mp3"
        asyncio.run(_synthesize_raw(sentence, raw_path))
        clip = _trim_and_fade(AudioSegment.from_mp3(raw_path))
        combined += pad + clip + pad
        os.remove(raw_path)
    combined.export(out_path, format="mp3", bitrate=EXPORT_BITRATE)


def synthesize_all(script: dict) -> list[dict]:
    """Returns segments enriched with 'audio_path' and 'duration' (seconds)."""
    enriched = []
    for i, seg in enumerate(script["segments"]):
        out_path = f"{config.WORKDIR}/seg_{i}.mp3"
        _synthesize_segment(seg["narration"], out_path, f"{config.WORKDIR}/_raw_{i}")
        # Measured with the same decoder that'll play it back later (ffmpeg,
        # via moviepy) rather than mutagen's header-based estimate, which can
        # be off by close to a second for edge-tts's mp3 output.
        with AudioFileClip(out_path) as clip:
            duration = clip.duration
        enriched.append({**seg, "audio_path": out_path, "duration": duration})
    return enriched


if __name__ == "__main__":
    with open(f"{config.WORKDIR}/script.json") as f:
        script = json.load(f)
    segments = synthesize_all(script)
    with open(f"{config.WORKDIR}/segments.json", "w") as f:
        json.dump(segments, f, indent=2)
    print(f"Synthesized {len(segments)} audio segments.")
