import agent.assemble as assemble
import agent.tts as tts
from pydub import AudioSegment
from pydub.generators import Sine


def test_fade_applied_to_synthetic_mp3(tmp_path):
    """Builds a short synthetic mp3 with pydub and asserts _trim_and_fade output
    starts and ends quieter than its own peak amplitude."""
    # 1 second loud sine wave
    sound = Sine(440).to_audio_segment(duration=1000).apply_gain(0)
    mp3_path = tmp_path / "synthetic.mp3"
    sound.export(mp3_path, format="mp3")

    raw_audio = AudioSegment.from_mp3(mp3_path)
    faded = tts._trim_and_fade(raw_audio)

    start_rms = faded[:5].rms
    end_rms = faded[-5:].rms
    mid_rms = faded[400:600].rms

    assert start_rms < mid_rms, f"Start RMS ({start_rms}) must be quieter than peak RMS ({mid_rms})"
    assert end_rms < mid_rms, f"End RMS ({end_rms}) must be quieter than peak RMS ({mid_rms})"


def test_fade_constants_within_audited_range():
    """Asserts tts.FADE_MS and assemble.AUDIO_EDGE_FADE are within 10-20ms."""
    assert 10 <= tts.FADE_MS <= 20, f"tts.FADE_MS ({tts.FADE_MS}) out of range [10, 20]"
    assemble_fade_ms = assemble.AUDIO_EDGE_FADE * 1000.0
    assert 10 <= assemble_fade_ms <= 20, f"assemble.AUDIO_EDGE_FADE ({assemble_fade_ms}ms) out of range [10, 20]"
