"""The garbled word at every paragraph join, present since the first upload.

What viewers heard (reported 2026-10-22 on "230 Years Of Digging Under Oak
Island Found Nothing", C7z93d0PyuI): the last word of a paragraph and the
first word of the next one blur together - "three teenagers with shovels" /
"Ten feet down" - at every paragraph boundary, never inside a paragraph.

What was actually in the audio: a ~60 ms burst at speech loudness sitting
inside what should have been the silent pause before the next paragraph.
Measured on the published track and reproduced locally, at all four joins.

Why. Each paragraph was exported as its own MP3 and timed by moviepy's
AudioFileClip, which takes its duration from ffmpeg's header estimate. For
these files the header claims ~60 ms more audio than the decoder can produce
(seg 0: header 7.82 s, decodable 7.7575 s). The paragraph was given the header
length on the timeline, so moviepy 1.0.3's reader was asked for samples past
the real end of the file - and instead of silence it handed back a bit-exact
copy of audio from ~0.9 s earlier in the same paragraph (the end of
"shovels", replayed). That burst landed right before the next paragraph's
first word.

Two fixes, one per layer: tts.py writes WAV and reports the paragraph length
from its own sample count, and assemble.py decodes the narration up front and
plays silence - never stale buffer contents - past the last real sample.

Every audio file here is built with the ffmpeg binary moviepy itself ships
(imageio-ffmpeg), because the CI test job has no system ffmpeg.
"""
import subprocess
import wave

import numpy as np
import pytest
from moviepy.config import get_setting
from moviepy.editor import AudioFileClip
from pydub.generators import Sine

from agent import assemble, config, tts

FFMPEG = get_setting("FFMPEG_BINARY")
EDGE_TTS_RATE = 24000  # edge-tts delivers 24 kHz mono


def _write_wav(path, samples: np.ndarray, rate: int = EDGE_TTS_RATE) -> None:
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())


def _encode_mp3_like_pydub(wav_path, mp3_path) -> None:
    """The exact command pydub's AudioSegment.export(format="mp3",
    bitrate="128k") ran for every paragraph before this fix."""
    subprocess.run([FFMPEG, "-y", "-v", "error", "-f", "wav", "-i", str(wav_path),
                    "-b:a", "128k", "-f", "mp3", str(mp3_path)], check=True)


def _decoded_seconds(path, rate: int = EDGE_TTS_RATE) -> float:
    """How much audio the decoder really produces - ignoring every header."""
    raw = subprocess.run([FFMPEG, "-v", "error", "-i", str(path), "-f", "f32le",
                          "-ac", "1", "-ar", str(rate), "-"],
                         check=True, capture_output=True).stdout
    return len(raw) / 4 / rate


def _render_front_to_back(audio) -> np.ndarray:
    """Reads the clip the way write_videofile does: start to finish in
    2000-sample chunks at 44.1 kHz. The bad samples only appeared on this
    access pattern - jumping straight to the tail read silence."""
    return np.vstack(list(audio.iter_chunks(fps=44100, chunksize=2000)))


def _paragraph_file(tmp_path, container: str, speech_seconds: float, tail_seconds: float):
    """A paragraph shaped like the real ones: loud speech-like audio, then the
    silence tts pads after the last sentence. Returns (path, header seconds
    as moviepy reads them, real decoded seconds)."""
    rng = np.random.default_rng(1855)
    speech = 0.3 * rng.standard_normal(int(round(speech_seconds * EDGE_TTS_RATE)))
    silence = np.zeros(int(round(tail_seconds * EDGE_TTS_RATE)))
    wav_path = tmp_path / "seg_0.wav"
    _write_wav(wav_path, np.concatenate([speech, silence]))
    path = wav_path
    if container == "mp3":
        path = tmp_path / "seg_0.mp3"
        _encode_mp3_like_pydub(wav_path, path)
    with AudioFileClip(str(path)) as header_clip:
        header_seconds = header_clip.duration
    return path, header_seconds, _decoded_seconds(path)


# Two ways the timeline slot ends up longer than the audio, both measured:
#  - mp3: the format every paragraph used until this fix. moviepy's bundled
#    ffmpeg (7.1, imageio-ffmpeg 0.6.0) reports the gross MP3 length, ~66 ms
#    past what decodes (encoder delay + padding are trimmed on decode).
#  - wav: ffmpeg prints durations rounded half-up to 10 ms, so a 7.6475 s WAV
#    reads as 7.65 s. Only 2.5 ms over - and the old reader still replayed
#    ~45 ms of earlier audio for it. Why WAV alone was not the fix.
@pytest.mark.parametrize("container,tail_seconds", [
    ("mp3", tts.HALF_GAP / 1000.0),
    ("wav", 0.1475),
])
@pytest.mark.parametrize("speed", [1.0, 1.1109])   # Oak Island played at 1.11x
def test_the_pause_after_a_paragraph_is_silent(tmp_path, container, tail_seconds, speed):
    """Timed the way the old pipeline timed it (moviepy's header duration),
    nothing after the paragraph's speech may be audible: that is the pause
    before the next paragraph's first word. Before the fix this held a 0.6+
    peak - a bit-exact replay of audio from ~0.3-0.9 s earlier in the same
    paragraph, because moviepy 1.0.3's FFMPEG_AudioReader.buffer_around moves
    its buffer_startframe forward at end of file without new audio arriving."""
    speech_seconds = 7.5
    path, header_seconds, real_seconds = _paragraph_file(
        tmp_path, container, speech_seconds, tail_seconds)
    if header_seconds <= real_seconds:
        pytest.skip(f"this ffmpeg reports {container} length exactly "
                    f"({header_seconds:.4f}s); nothing past the end to read. "
                    "test_narration_is_loaded_sample_exact still pins the fix.")

    seg = {"audio_path": str(path), "duration": header_seconds / speed,
           "audio_playback_speed": speed}
    audio = assemble._segment_audio(seg)
    # From a little after the speech ends (MP3's own smearing) to the end of
    # the paragraph's slot on the timeline.
    tail = _render_front_to_back(audio)[int((speech_seconds + 0.03) / speed * 44100):]
    peak = float(np.abs(tail).max())
    assert peak < 0.01, (
        f"{peak:.3f} peak in the pause after the paragraph's last word "
        f"({container}, header {header_seconds:.4f}s, real {real_seconds:.4f}s)")


def test_narration_is_loaded_sample_exact(tmp_path):
    """What is loaded is exactly what the file holds: same length, and the
    same samples at the same level moviepy's AudioFileClip produced before
    this fix (its ffmpeg mono-to-stereo upmix is -3 dB, which the music bed's
    volume was tuned against - the loudness must not move)."""
    rate = 44100
    t = np.arange(int(1.2345 * rate)) / rate
    tone = 0.5 * np.sin(2 * np.pi * 330 * t)
    path = tmp_path / "seg_0.wav"
    _write_wav(path, tone, rate=rate)

    clip = assemble._load_narration(str(path))
    assert clip.duration == pytest.approx(len(tone) / rate, abs=1e-6)
    got = _render_front_to_back(clip)
    assert got.shape[1] == 2
    with AudioFileClip(str(path)) as before:
        old = _render_front_to_back(before)
    # Interior only: the old reader's last few ms are the very bug under test.
    n = min(len(got), len(old)) - 2000
    assert np.allclose(got[1:n], old[1:n], atol=1e-3)
    assert np.allclose(got[1:n, 0], tone[1:n] / np.sqrt(2), atol=1e-3)
    # Past the end is silence, not a repeat of anything.
    past_end = clip.set_duration(clip.duration + 0.1).subclip(clip.duration, clip.duration + 0.1)
    assert float(np.abs(past_end.to_soundarray(fps=rate)).max()) == 0.0


def test_reported_paragraph_length_is_the_length_of_its_audio(tmp_path, monkeypatch):
    """tts used to report the MP3 header's length, ~60 ms longer than the
    audio. The timeline is built from this number, so it has to be the real
    one: the reported duration must equal what the written file decodes to."""
    monkeypatch.setattr(config, "WORKDIR", str(tmp_path))
    lengths_ms = iter([2345, 1789, 3011])

    def fake_edge_tts(text, out_path):
        open(out_path, "wb").close()

    def fake_from_mp3(path):
        return (Sine(220, sample_rate=EDGE_TTS_RATE)
                .to_audio_segment(duration=next(lengths_ms), volume=-6.0)
                .set_channels(1))

    monkeypatch.setattr(tts, "_synthesize_with_fallback", fake_edge_tts)
    monkeypatch.setattr(tts.AudioSegment, "from_mp3", staticmethod(fake_from_mp3))

    script = {"segments": [{"narration": "Ten feet down, flagstones. Then oak."},
                           {"narration": "Then the shaft flooded."}]}
    enriched, total, _ = tts._synthesize_pass_once(script)

    for seg in enriched:
        real = _decoded_seconds(seg["audio_path"])
        assert seg["duration"] == pytest.approx(real, abs=0.001), seg["audio_path"]
        # The last sentence ends a trailing HALF_GAP before the paragraph does.
        last = seg["sentences"][-1]
        assert last["start"] + last["duration"] + tts.HALF_GAP / 1000.0 == \
            pytest.approx(seg["duration"], abs=0.002)
    assert total == pytest.approx(sum(s["duration"] for s in enriched))
