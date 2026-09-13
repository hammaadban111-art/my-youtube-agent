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
from . import config, resilience

TTS_RATE = "+8%"
# A small final time-stretch makes the configured video length an actual
# output contract rather than a metadata field.  Beyond this range the packet
# prose is materially the wrong length and must be repaired instead of making
# a narrator sound unnatural.
#
# Defined in config so script_writer can predict against the same band at
# packet-validation time; re-exported here because this module is where it is
# actually applied, and callers have always read it from tts.
MIN_FINAL_PLAYBACK_SPEED = config.MIN_FINAL_PLAYBACK_SPEED
MAX_FINAL_PLAYBACK_SPEED = config.MAX_FINAL_PLAYBACK_SPEED
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

# Target cut rhythm for shot planning: a sentence longer than this gets cut
# mid-sentence into pieces sized around the target instead of holding one
# clip for the whole sentence.
MAX_SHOT_SECONDS = 3.0
TARGET_SHOT_SECONDS = 2.25
# Where the first cut lands, in seconds. Sits inside the 2-6%-of-video window
# where the median retention curve takes its steepest fall (measured over 91
# videos, 2026-09-09); on a 35s Short that window is about 0.7s-2.1s.
FIRST_CUT_SECONDS = 1.2
# The opening shot is only split when doing so leaves the second piece at least
# this long. Prevents turning a 1.3s opening into a 1.2s shot plus a 0.1s
# flicker, which would be worse than the held frame it replaced.
MIN_OPENING_TAIL_SECONDS = 0.8

# Tried in order if the configured voice fails. edge-tts talks to Microsoft's
# public endpoint, where an individual voice can be transiently unavailable
# while others work — swapping voice mid-video would be audible, so a fallback
# applies to the whole run and is recorded as a degradation.
FALLBACK_VOICES = ["en-US-ChristopherNeural", "en-US-EricNeural", "en-GB-RyanNeural"]
_active_voice: str | None = None


def active_voice() -> str:
    """The voice actually used, which differs from config.VOICE if a fallback
    kicked in — so the saved record reflects reality, not intent."""
    return _active_voice or config.VOICE


def _split_sentences(text: str) -> list[str]:
    # Allow an optional closing quote/bracket between the sentence-ending
    # punctuation and the whitespace (e.g. `signal.' What...`) — without
    # this, such sentences never split, silently reintroducing the uneven
    # multi-sentence-in-one-TTS-call pacing problem for that one case.
    parts = re.split(r"(?<=[.!?])(?:[\"'’”\)\]\}]+)?\s+", text.strip())
    return [p for p in parts if p]


async def _synthesize_raw(text: str, out_path: str, voice: str = None):
    communicate = edge_tts.Communicate(text, voice or config.VOICE, rate=TTS_RATE)
    await communicate.save(out_path)


def _synthesize_with_fallback(text: str, out_path: str) -> None:
    """Synthesizes one sentence, retrying the active voice before falling back
    to an alternate. Once a fallback voice works it is pinned for the rest of
    the run, so a single video never mixes two different narrator voices."""
    global _active_voice
    voice = _active_voice or config.VOICE

    try:
        resilience.retry(
            lambda: asyncio.run(_synthesize_raw(text, out_path, voice)),
            label=f"edge-tts ({voice})", attempts=3, base_delay=3.0,
        )
        return
    except Exception as primary:  # noqa: BLE001 - fall back to another voice
        last_error = primary

    for candidate in FALLBACK_VOICES:
        if candidate == voice:
            continue
        try:
            asyncio.run(_synthesize_raw(text, out_path, candidate))
        except Exception as e:  # noqa: BLE001 - try the next voice
            last_error = e
            continue
        resilience.record_degradation(
            "edge-tts",
            f"voice {voice!r} failed: {type(last_error).__name__}: {last_error}",
            f"switched to {candidate!r} for the rest of this video",
        )
        _active_voice = candidate
        return

    raise RuntimeError(
        f"edge-tts failed on every voice tried. Last error: {last_error}"
    ) from last_error


def _trim_and_fade(audio: AudioSegment) -> AudioSegment:
    """Removes leading/trailing silence conservatively, then fades the cut
    edges so splicing against pure silence doesn't produce a click."""
    start = detect_leading_silence(audio, silence_threshold=SILENCE_THRESH_DB)
    end = detect_leading_silence(audio.reverse(), silence_threshold=SILENCE_THRESH_DB)
    trimmed = audio[start: len(audio) - end]
    if len(trimmed) < 200:  # safety net: don't trim to near-nothing
        trimmed = audio
    return trimmed.fade_in(FADE_MS).fade_out(FADE_MS)


def _synthesize_segment(narration: str, out_path: str, tmp_prefix: str) -> list[dict]:
    """Returns per-sentence timing (text/start/duration, in seconds, relative
    to this segment's own audio) so captions can be synced sentence-by-
    sentence instead of showing the whole segment's text at once."""
    sentences = _split_sentences(narration) or [narration]
    pad = AudioSegment.silent(duration=HALF_GAP)
    combined = AudioSegment.empty()
    timings = []
    for i, sentence in enumerate(sentences):
        raw_path = f"{tmp_prefix}_{i}.mp3"
        _synthesize_with_fallback(sentence, raw_path)
        clip = _trim_and_fade(AudioSegment.from_mp3(raw_path))
        combined += pad
        start_ms = len(combined)
        combined += clip
        timings.append({
            "text": sentence,
            "start": start_ms / 1000.0,
            "duration": len(clip) / 1000.0,
        })
        combined += pad
        os.remove(raw_path)
    combined.export(out_path, format="mp3", bitrate=EXPORT_BITRATE)
    return timings


def _plan_shots(sentences: list[dict], duration: float,
                is_opening: bool = False) -> list[dict]:
    """Turns per-sentence timings into cut points for a faster edit rhythm:
    each sentence is one shot, unless it runs past MAX_SHOT_SECONDS, in which
    case it's split into evenly-sized pieces around TARGET_SHOT_SECONDS so no
    single clip holds the screen past the target cut pace.

    Sentence timings themselves leave small silent gaps uncovered (the
    inter-sentence pause _synthesize_segment pads in) - each shot is
    stretched to the next cut point (or segment end) so every moment of the
    segment is covered by exactly one shot, with no blank gaps.
    """
    if not sentences:
        return [{"start": 0.0, "duration": duration}]

    cuts = []
    for sent in sentences:
        s_start, s_dur = sent["start"], sent["duration"]
        if s_dur <= MAX_SHOT_SECONDS:
            cuts.append(s_start)
        else:
            n = max(2, round(s_dur / TARGET_SHOT_SECONDS))
            piece = s_dur / n
            cuts.extend(s_start + k * piece for k in range(n))
    cuts[0] = 0.0

    # THE OPENING CUT.
    #
    # Measured across the 91 videos that carry retention curves (2026-09-09):
    # the steepest median drop in the whole catalogue sits between 2% and 6% of
    # the video, which on a 35-second Short is roughly the first one to two
    # seconds — before the opening sentence has even finished. 73 of 91 videos
    # take their single biggest drop before the 20% mark.
    #
    # The opening sentence is usually under MAX_SHOT_SECONDS, so it was a
    # single unbroken shot: one still-ish frame held across the exact moment
    # the audience is deciding whether to stay. This forces a second shot
    # inside the first FIRST_CUT_SECONDS so there is visual change there.
    #
    # Deliberately only the FIRST shot, and only when it is long enough to
    # survive being halved — the fix for a slow opening is not a strobe. If the
    # opening shot is already shorter than the threshold there is already a cut
    # in the window and nothing is added.
    if is_opening and len(cuts) >= 1:
        first_end = cuts[1] if len(cuts) > 1 else duration
        if first_end - cuts[0] > FIRST_CUT_SECONDS + MIN_OPENING_TAIL_SECONDS:
            cuts.insert(1, cuts[0] + FIRST_CUT_SECONDS)

    shots = []
    for i, start in enumerate(cuts):
        end = cuts[i + 1] if i + 1 < len(cuts) else duration
        shots.append({"start": start, "duration": max(end - start, 0.1)})
    return shots


def voice_for(script: dict) -> str:
    """The voice this script asks for, or the configured default.

    The first experiment dimension the packet can actually drive. Every one of
    the channel's first 114 videos used en-US-GuyNeural, so "does the voice
    matter" has never been askable of our own data — not because the answer is
    no, but because there was no second arm. A packet story may now declare
    `editorial.voice`; anything it does not declare stays on config.VOICE, so
    the default path is unchanged.

    Validated as a non-empty string by packet.validate_editorial. An unknown
    voice name still falls through to the existing edge-tts fallback in
    _synthesize_segment, which is why a bad value degrades rather than fails."""
    requested = (script.get("editorial") or {}).get("voice")
    return requested.strip() if isinstance(requested, str) and requested.strip() \
        else config.VOICE


def synthesize_all(script: dict) -> list[dict]:
    """Returns segments enriched with 'audio_path', 'duration' (seconds),
    'sentences' (per-sentence timing) and 'shots' (cut plan for visuals)."""
    global _active_voice
    requested_voice = voice_for(script)
    if requested_voice != config.VOICE:
        print(f"[tts] using experiment voice {requested_voice!r} "
              f"(default is {config.VOICE!r})")
        _active_voice = requested_voice
    enriched = []
    for i, seg in enumerate(script["segments"]):
        out_path = f"{config.WORKDIR}/seg_{i}.mp3"
        sentence_timings = _synthesize_segment(seg["narration"], out_path, f"{config.WORKDIR}/_raw_{i}")
        # Measured with the same decoder that'll play it back later (ffmpeg,
        # via moviepy) rather than mutagen's header-based estimate, which can
        # be off by close to a second for edge-tts's mp3 output.
        with AudioFileClip(out_path) as clip:
            duration = clip.duration
        # Only the video's FIRST segment gets the opening cut: the drop this
        # targets is at the start of the VIDEO, and re-cutting every segment
        # would change the pacing of the whole edit rather than the one moment
        # the measurement points at.
        shots = _plan_shots(sentence_timings, duration, is_opening=(i == 0))
        enriched.append({**seg, "audio_path": out_path, "duration": duration,
                          "sentences": sentence_timings, "shots": shots})
    total_duration = sum(float(seg["duration"]) for seg in enriched)
    target_duration = float(config.VIDEO_LENGTH_SECONDS)
    if target_duration <= 0:
        raise RuntimeError("VIDEO_LENGTH_SECONDS must be positive")
    if total_duration <= 0:
        raise RuntimeError("Synthesized narration has no duration")

    playback_speed = total_duration / target_duration
    if not MIN_FINAL_PLAYBACK_SPEED <= playback_speed <= MAX_FINAL_PLAYBACK_SPEED:
        raise RuntimeError(
            f"Narration is {total_duration:.1f}s but VIDEO_LENGTH_SECONDS is "
            f"{target_duration:.1f}s. Required playback speed {playback_speed:.2f} "
            f"is outside the safe {MIN_FINAL_PLAYBACK_SPEED:.2f}-"
            f"{MAX_FINAL_PLAYBACK_SPEED:.2f} range; repair the packet's prose "
            "length instead of publishing distorted narration.")

    # Assemble applies this same speed factor to the audio file.  Recompute
    # sentence/cut timings here so captions and footage stay locked to the
    # spoken audio, and the final sum is the configured duration.
    for seg in enriched:
        source_duration = float(seg["duration"])
        scaled_duration = source_duration / playback_speed
        scaled_sentences = [
            {**sentence,
             "start": float(sentence["start"]) / playback_speed,
             "duration": float(sentence["duration"]) / playback_speed}
            for sentence in seg["sentences"]
        ]
        seg["source_duration"] = source_duration
        seg["duration"] = scaled_duration
        seg["audio_playback_speed"] = playback_speed
        seg["sentences"] = scaled_sentences
        seg["shots"] = _plan_shots(scaled_sentences, scaled_duration)
    return enriched


if __name__ == "__main__":
    with open(f"{config.WORKDIR}/script.json") as f:
        script = json.load(f)
    segments = synthesize_all(script)
    with open(f"{config.WORKDIR}/segments.json", "w") as f:
        json.dump(segments, f, indent=2)
    print(f"Synthesized {len(segments)} audio segments.")
