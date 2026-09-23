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
MIN_FINAL_PLAYBACK_SPEED = 0.85
MAX_FINAL_PLAYBACK_SPEED = 1.15
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
# Every voice that actually spoke a sentence in the current synthesis pass.
_pass_voices: set[str] = set()

# ------------------------------------------------------------ adaptive rate
#
# WHY. Between 2026-09-13 and 09-17, ten of roughly twenty upload runs died on
# the playback-speed guard in synthesize_all(), all with a message of the form
#
#     Narration is 29.2s but VIDEO_LENGTH_SECONDS is 35.0s. Required playback
#     speed 0.83 is outside the safe 0.85-1.15 range
#
# Three stories did it: Berners Street hoax (29.2s), Marree Man (40.7s) and
# Great Smog (29.7s). Within a day a length failure is deterministic — the same
# script synthesised three times in a row came back identical to the hundredth
# of a second — so each one burned three slots before being retired, and two
# good researched stories were thrown away.
#
# ACROSS days it is not. With the text, voice and edge-tts 7.2.8 all unchanged,
# the service itself moved: re-synthesised on 2026-09-17, Berners Street hoax
# came back at 34.4s (was 29.2s) and Marree Man at 37.5s (was 40.7s). The
# length of a script is therefore a property of the script AND of whatever
# Microsoft is running that day. A story can pass when it is written and fail
# when it airs.
#
# That rules out the obvious fix twice over. Checking length before rendering
# cannot see a service that has not drifted yet, and word count does not
# predict length even on a single day:
#
#     Berners Street hoax   122 words -> 29.2s   (251 words per minute)
#     Marree Man            115 words -> 40.7s   (170 words per minute)
#
# The same voice at the same nominal rate speaks those two texts at speeds 48%
# apart. Numbers, abbreviations and sentence shape move it more than length
# does. The only reliable measurement is the synthesis itself.
#
# So the repair happens where the measurement is: synthesise once at the house
# rate, and if the result lands outside the window, synthesise again at an
# edge-tts rate chosen to land it near the target. A neural voice speaking a
# little faster or slower is prosody, not a time-stretch, so this is kinder to
# the audio than widening the stretch range would be — the stretch still runs
# afterwards, but only has a few percent left to correct.
#
# The rate is clamped to a range that still sounds like natural narration.
# Anything that cannot be brought inside the window even at those limits is a
# genuinely wrong-length script and still fails — but as NarrationLengthError,
# which agent/main.py retires at once instead of retrying it into two more
# lost slots.
_active_rate: str | None = None
ADAPTIVE_RATE_MIN_PCT = -20
ADAPTIVE_RATE_MAX_PCT = 25
# How many re-syntheses the repair may spend before the script is declared the
# wrong length. Each costs one full narration (~20 edge-tts calls, well under a
# minute); a retired story costs a researched script and a slot.
REPAIR_PASSES = 2


class NarrationLengthError(RuntimeError):
    """The script cannot be spoken inside the length window at any natural rate.

    Raised only after the adaptive-rate repair has already been tried, so it
    means the script is far outside the window, not marginally. A retry the
    same day reproduces identical audio, so agent/main.py retires the story on
    the first occurrence rather than letting it consume MAX_ATTEMPTS slots.
    Service drift across days is why this retires rather than deletes: a
    retired story is not published, so a later packet may propose it again."""


def _rate_pct(rate) -> int:
    """'+8%' -> 8, '-12%' -> -12. Anything unparseable reads as 0."""
    try:
        return int(str(rate).strip().rstrip("%"))
    except (TypeError, ValueError):
        return 0


def _format_rate(pct: int) -> str:
    return f"{pct:+d}%"


def active_rate() -> str:
    """The edge-tts rate actually used for this video — the house rate unless
    the length repair chose another — so the saved record reflects reality."""
    return _active_rate or TTS_RATE


def adapted_rate(total_duration: float, target_duration: float,
                 gap_seconds: float, current_rate: str = None) -> str:
    """The edge-tts rate that should bring `total_duration` to roughly
    `target_duration`, clamped to the natural-sounding range.

    Only the SPEECH scales with rate. The silence padded around each sentence is
    a fixed GAP_MS and does not, so it is taken out before the ratio is computed;
    ignoring it overcorrects on sentence-heavy scripts, which are exactly the
    ones that ran fast."""
    current_pct = _rate_pct(current_rate or TTS_RATE)
    speech_now = max(0.1, total_duration - gap_seconds)
    speech_target = max(0.1, target_duration - gap_seconds)
    # Spoken duration is inversely proportional to (1 + rate).
    factor = (1 + current_pct / 100.0) * speech_now / speech_target
    pct = round((factor - 1) * 100)
    pct = max(ADAPTIVE_RATE_MIN_PCT, min(ADAPTIVE_RATE_MAX_PCT, pct))
    return _format_rate(pct)


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
    communicate = edge_tts.Communicate(text, voice or config.VOICE, rate=active_rate())
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
        _pass_voices.add(voice)
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
        _pass_voices.add(candidate)
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


def _synthesize_pass(script: dict) -> tuple[list[dict], float, float]:
    """One full synthesis of every segment at the current active rate.

    Returns (enriched segments, total spoken duration, fixed silence). The
    silence is GAP_MS per sentence — _synthesize_segment pads HALF_GAP on each
    side — and is reported separately because it does not scale with rate.

    If the voice changes DURING the pass — the active voice failed partway and
    _synthesize_with_fallback pinned another one — the sentences before the
    switch were spoken by the first voice and the rest by the second: two
    narrators in one 35-second video, which the fallback exists to prevent.
    The pass is then run once more, entirely in the voice that works."""
    result = _synthesize_pass_once(script)
    if len(_pass_voices) > 1:
        print(f"[tts] voice changed mid-narration ({', '.join(sorted(_pass_voices))}); "
              f"re-synthesising so {active_voice()!r} speaks all of it")
        result = _synthesize_pass_once(script)
    return result


def _synthesize_pass_once(script: dict) -> tuple[list[dict], float, float]:
    _pass_voices.clear()
    enriched = []
    sentence_count = 0
    for i, seg in enumerate(script["segments"]):
        out_path = f"{config.WORKDIR}/seg_{i}.mp3"
        sentence_timings = _synthesize_segment(seg["narration"], out_path, f"{config.WORKDIR}/_raw_{i}")
        sentence_count += len(sentence_timings)
        # Measured with the same decoder that'll play it back later (ffmpeg,
        # via moviepy) rather than mutagen's header-based estimate, which can
        # be off by close to a second for edge-tts's mp3 output.
        with AudioFileClip(out_path) as clip:
            duration = clip.duration
        enriched.append({**seg, "audio_path": out_path, "duration": duration,
                          "sentences": sentence_timings})
    total = sum(float(seg["duration"]) for seg in enriched)
    return enriched, total, sentence_count * GAP_MS / 1000.0


def _speed_ok(speed: float) -> bool:
    return MIN_FINAL_PLAYBACK_SPEED <= speed <= MAX_FINAL_PLAYBACK_SPEED


def synthesize_all(script: dict) -> list[dict]:
    """Returns segments enriched with 'audio_path', 'duration' (seconds),
    'sentences' (per-sentence timing) and 'shots' (cut plan for visuals)."""
    global _active_voice, _active_rate
    _active_rate = None
    requested_voice = voice_for(script)
    if requested_voice != config.VOICE:
        print(f"[tts] using experiment voice {requested_voice!r} "
              f"(default is {config.VOICE!r})")
        _active_voice = requested_voice

    target_duration = float(config.VIDEO_LENGTH_SECONDS)
    if target_duration <= 0:
        raise RuntimeError("VIDEO_LENGTH_SECONDS must be positive")

    enriched, total_duration, gap_seconds = _synthesize_pass(script)
    if total_duration <= 0:
        raise RuntimeError("Synthesized narration has no duration")
    playback_speed = total_duration / target_duration

    if not _speed_ok(playback_speed):
        # THE REPAIR. See the adaptive-rate notes at the top of this module.
        # Up to REPAIR_PASSES re-syntheses, each re-aiming from the length the
        # previous one actually produced: edge-tts rate is not exactly linear
        # in duration, so a first correction that lands just outside the
        # window is usually brought inside by a second. Stops early once the
        # rate is pinned at its clamp — another pass would repeat the audio.
        first_total, first_speed = total_duration, playback_speed
        for _ in range(REPAIR_PASSES):
            next_rate = adapted_rate(total_duration, target_duration,
                                     gap_seconds, current_rate=active_rate())
            if next_rate == active_rate():
                break
            _active_rate = next_rate
            print(f"[tts] narration ran {total_duration:.1f}s against a "
                  f"{target_duration:.0f}s target (speed {playback_speed:.2f}); "
                  f"re-synthesising at rate {_active_rate}")
            enriched, total_duration, gap_seconds = _synthesize_pass(script)
            playback_speed = total_duration / target_duration
            if _speed_ok(playback_speed):
                break
        resilience.record_degradation(
            "tts-rate",
            f"narration synthesised to {first_total:.1f}s at {TTS_RATE}, "
            f"outside the {MIN_FINAL_PLAYBACK_SPEED:.2f}-"
            f"{MAX_FINAL_PLAYBACK_SPEED:.2f} speed window "
            f"(needed {first_speed:.2f})",
            f"re-synthesised at {active_rate()}: {total_duration:.1f}s, "
            f"speed {playback_speed:.2f}",
        )

    if not _speed_ok(playback_speed):
        raise NarrationLengthError(
            f"Narration is {total_duration:.1f}s but VIDEO_LENGTH_SECONDS is "
            f"{target_duration:.1f}s even at rate {active_rate()}. Required "
            f"playback speed {playback_speed:.2f} is outside the safe "
            f"{MIN_FINAL_PLAYBACK_SPEED:.2f}-{MAX_FINAL_PLAYBACK_SPEED:.2f} range; "
            "the script's prose is the wrong length and has to be rewritten. "
            "Retrying it would produce the same audio, so the story is retired.")

    # Assemble applies this same speed factor to the audio file. Recompute
    # sentence/cut timings here so captions and footage stay locked to the
    # spoken audio, and the final sum is the configured duration.
    for i, seg in enumerate(enriched):
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
        # is_opening MUST be passed here, not only on a first plan. The shot
        # plan used to be computed twice — once before scaling, with the
        # opening cut, and again here without it — and this second plan is the
        # one assemble.py renders. The early cut added on 2026-09-09 to target
        # the 2-6% retention drop was therefore silently discarded on every
        # single video. tests/test_tts_rate.py pins that it now survives.
        seg["shots"] = _plan_shots(scaled_sentences, scaled_duration,
                                   is_opening=(i == 0))
    return enriched


if __name__ == "__main__":
    with open(f"{config.WORKDIR}/script.json") as f:
        script = json.load(f)
    segments = synthesize_all(script)
    with open(f"{config.WORKDIR}/segments.json", "w") as f:
        json.dump(segments, f, indent=2)
    print(f"Synthesized {len(segments)} audio segments.")
