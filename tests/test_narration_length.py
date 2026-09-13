"""The length gate.

On 2026-09-12 a packet shipped in which half the stories could not be spoken
into VIDEO_LENGTH_SECONDS. Nothing checked, so `--validate` passed it, and the
channel then went dark for 22 hours while the renderer discovered the problem
one slot at a time — three failed attempts per story, each one burning a real
publishing slot. These tests hold the check at the packet, where the prose can
still be rewritten.
"""
import pytest

from agent import cadence, config, packet, script_writer
from tests.test_packet import _packet, _story, _utc, _write_packet


def _narration_of(seconds_target, sentences=10):
    """Prose whose ESTIMATE lands near `seconds_target`, as five segments."""
    chars = int((seconds_target + 6.058 - 0.4387 * sentences) / 0.0550)
    per_segment = max(chars // 5, 1)
    # Two sentences per segment keeps the sentence count at `sentences`.
    out = []
    for _ in range(5):
        half = max(per_segment // 2 - 2, 1)
        out.append(("x" * half) + ". " + ("y" * (per_segment - half - 2)) + ".")
    return out


def test_the_estimator_tracks_real_synthesized_audio():
    """Fitted against real edge-tts output for the twenty stories of the
    2026-W39 packet; these are three of those measurements, so a change to the
    coefficients that breaks the fit fails here rather than on the channel."""
    # (chars, sentences, measured seconds) — vasa, copper-scroll and marree-man
    # as they were written in the 2026-W39 bridge packet, each synthesized with
    # the configured voice at TTS_RATE and measured through the same trim-and-
    # pad path _synthesize_segment uses. marree-man is the worst residual in
    # the whole fit at 1.98s, which is what sets DURATION_SAFETY_MARGIN_SECONDS.
    for chars, sentences, measured in ((532, 7, 26.9), (655, 9, 34.1), (729, 10, 40.4)):
        estimated = (script_writer.SECONDS_PER_CHAR * chars
                     + script_writer.SECONDS_PER_SENTENCE * sentences
                     + script_writer.DURATION_INTERCEPT)
        assert abs(estimated - measured) < 2.5, (chars, estimated, measured)


def test_bounds_sit_strictly_inside_what_the_renderer_accepts():
    """The gate must reject before tts does, never after: a story the packet
    passes and the renderer then refuses is exactly the 2026-09-13 outage."""
    low, high = script_writer.narration_duration_bounds()
    target = float(config.VIDEO_LENGTH_SECONDS)
    assert low > target * config.MIN_FINAL_PLAYBACK_SPEED
    assert high < target * config.MAX_FINAL_PLAYBACK_SPEED


def test_prose_that_is_too_short_is_rejected_and_told_to_grow():
    problem = script_writer.check_narration_length(_narration_of(24.0))
    assert "too SHORT" in problem
    assert "Add roughly" in problem


def test_prose_that_is_too_long_is_rejected_and_told_to_shrink():
    problem = script_writer.check_narration_length(_narration_of(46.0))
    assert "too LONG" in problem
    assert "Cut roughly" in problem


def test_prose_of_the_right_length_passes():
    assert script_writer.check_narration_length(_narration_of(35.0)) == ""


def test_the_sentence_splitter_matches_the_synthesizer():
    """The per-sentence term models the silence _synthesize_segment pads around
    every sentence, so the two splitters have to agree or the model mispredicts
    the very prose it exists to catch."""
    tts = pytest.importorskip("agent.tts")
    text = "She heeled over. Water came in through the ports.' Then she sank."
    assert script_writer._split_sentences(text) == tts._split_sentences(text)


def test_a_mislengthed_story_fails_the_packet_gate(tmp_path, monkeypatch):
    """The 2026-09-13 regression, end to end: a short story must stop the
    packet at --validate rather than at render time."""
    story = _story(_utc(2099, 1, 1, 6, 7), "Berners Street hoax")
    for segment in story["segments"]:
        segment["narration"] = "Nine hikers cut their tent open from inside."
    problems = packet.validate_packet(_packet([story]))
    assert any("too SHORT" in p for p in problems), problems


def test_an_already_published_story_is_exempt(tmp_path, monkeypatch):
    """A published story cannot be relengthened — the video is on the channel.
    Failing the packet over one would take every FUTURE slot down to complain
    about a past one, which is the outage detector causing the outage."""
    short = _story(_utc(2020, 1, 1, 6, 7), "Codex Gigas", story_id="already-out")
    for segment in short["segments"]:
        segment["narration"] = "Nine hikers cut their tent open from inside."
    _write_packet(tmp_path, monkeypatch, [short])
    packet.record_status(short, "published", video_id="vid1")

    problems = packet.validate_packet(_packet([short]))
    assert not any("too SHORT" in p for p in problems), problems
