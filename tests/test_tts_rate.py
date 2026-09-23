"""Adaptive narration rate and the length-failure retirement, added 2026-09-17.

The incident: between 2026-09-13 and 09-17, ten of roughly twenty upload runs
failed on the playback-speed guard. Three stories caused all ten — Berners
Street hoax (29.2s), Marree Man (40.7s) and Great Smog (29.7s) — each failing
identically on every retry because the same script always synthesises to the
same length. Two were retired after three lost slots apiece.

Word count could not have caught them: Berners at 122 words ran 251 words per
minute and Marree at 115 words ran 170. These tests pin the repair that works
from the real synthesis instead.
"""
import pytest

from agent import config, packet, tts


@pytest.fixture(autouse=True)
def _reset_tts_state(monkeypatch):
    monkeypatch.setattr(tts, "_active_rate", None)
    monkeypatch.setattr(tts, "_active_voice", None)
    monkeypatch.setattr(config, "VIDEO_LENGTH_SECONDS", 35)
    yield


# ------------------------------------------------------------- rate arithmetic

def test_a_short_narration_is_slowed_down():
    """Berners Street hoax: 29.2s at +8% across ten sentences."""
    rate = tts.adapted_rate(29.2, 35.0, gap_seconds=10 * tts.GAP_MS / 1000)
    assert tts._rate_pct(rate) < 0


def test_a_long_narration_is_sped_up():
    """Marree Man: 40.7s at +8% across nine sentences."""
    rate = tts.adapted_rate(40.7, 35.0, gap_seconds=9 * tts.GAP_MS / 1000)
    assert tts._rate_pct(rate) > tts._rate_pct(tts.TTS_RATE)


def test_the_real_failures_land_inside_the_window_after_repair():
    """Model each failing story as: speech scales with 1/(1+rate), silence does
    not. The adapted rate must bring every one of them inside the window the
    final stretch can finish."""
    cases = [(29.2, 10), (40.7, 9), (29.7, 9)]
    for total, sentences in cases:
        gaps = sentences * tts.GAP_MS / 1000
        rate = tts.adapted_rate(total, 35.0, gaps)
        speech = (total - gaps) * (1 + tts._rate_pct(tts.TTS_RATE) / 100) \
            / (1 + tts._rate_pct(rate) / 100)
        new_speed = (speech + gaps) / 35.0
        assert tts.MIN_FINAL_PLAYBACK_SPEED <= new_speed <= tts.MAX_FINAL_PLAYBACK_SPEED, \
            (total, rate, new_speed)


def test_the_rate_is_clamped_to_natural_speech():
    """A wildly wrong script must not be spoken at 3x speed to make it fit."""
    assert tts._rate_pct(tts.adapted_rate(5.0, 35.0, 1.0)) == tts.ADAPTIVE_RATE_MIN_PCT
    assert tts._rate_pct(tts.adapted_rate(120.0, 35.0, 1.0)) == tts.ADAPTIVE_RATE_MAX_PCT


def test_ignoring_the_fixed_silence_would_overcorrect():
    """The padding between sentences does not scale with rate. A sentence-heavy
    script — the ones that ran fast — needs a gentler correction than a naive
    total/target ratio would give."""
    naive = tts.adapted_rate(29.2, 35.0, gap_seconds=0.0)
    aware = tts.adapted_rate(29.2, 35.0, gap_seconds=2.8)
    assert tts._rate_pct(aware) <= tts._rate_pct(naive)


def test_rate_strings_round_trip():
    assert tts._rate_pct("+8%") == 8
    assert tts._rate_pct("-12%") == -12
    assert tts._format_rate(-12) == "-12%"
    assert tts._format_rate(8) == "+8%"
    assert tts._rate_pct("nonsense") == 0


# --------------------------------------------------------- synthesize_all flow

def _fake_pass(durations, sentences_per_segment=2):
    """A stand-in for _synthesize_pass that returns successive totals, and
    records the rate each pass was asked to use."""
    calls = []

    def fake(script):
        calls.append(tts.active_rate())
        total = durations[min(len(calls) - 1, len(durations) - 1)]
        n = len(script["segments"])
        per = total / n
        enriched = []
        for i, seg in enumerate(script["segments"]):
            enriched.append({**seg, "audio_path": f"seg_{i}.mp3", "duration": per,
                             "sentences": [
                                 {"text": "a", "start": 0.0, "duration": per * 0.7},
                                 {"text": "b", "start": per * 0.7, "duration": per * 0.3},
                             ]})
        gaps = n * sentences_per_segment * tts.GAP_MS / 1000
        return enriched, total, gaps
    return fake, calls


def _script():
    return {"segments": [{"narration": f"line {i}."} for i in range(5)]}


def test_a_story_inside_the_window_is_synthesised_once(monkeypatch):
    fake, calls = _fake_pass([34.0])
    monkeypatch.setattr(tts, "_synthesize_pass", fake)
    out = tts.synthesize_all(_script())
    assert len(calls) == 1
    assert tts.active_rate() == tts.TTS_RATE
    assert sum(s["duration"] for s in out) == pytest.approx(35.0)


def test_a_short_story_is_repaired_instead_of_failing(monkeypatch):
    """The regression. This used to raise on the spot and cost three slots."""
    fake, calls = _fake_pass([29.2, 34.6])
    monkeypatch.setattr(tts, "_synthesize_pass", fake)
    out = tts.synthesize_all(_script())
    assert len(calls) == 2
    assert calls[0] == tts.TTS_RATE
    assert tts._rate_pct(calls[1]) < 0          # second pass slowed down
    assert tts.active_rate() == calls[1]        # and the record will say so
    assert sum(s["duration"] for s in out) == pytest.approx(35.0)


def test_the_repair_is_recorded_as_a_degradation(monkeypatch):
    from agent import resilience
    resilience.reset()
    fake, _ = _fake_pass([40.7, 35.4])
    monkeypatch.setattr(tts, "_synthesize_pass", fake)
    tts.synthesize_all(_script())
    stages = [d["stage"] for d in resilience.degradations()]
    assert "tts-rate" in stages


def test_an_unrepairable_story_raises_the_retire_error(monkeypatch):
    fake, _ = _fake_pass([8.0, 10.0])
    monkeypatch.setattr(tts, "_synthesize_pass", fake)
    with pytest.raises(tts.NarrationLengthError) as excinfo:
        tts.synthesize_all(_script())
    assert "retired" in str(excinfo.value)


def test_a_first_repair_that_lands_just_outside_gets_a_second(monkeypatch):
    """edge-tts rate is not exactly linear in duration: a correction aimed at
    35s can land at 29.6s. One more pass, re-aimed from what the first one
    actually produced, saves a researched story that would otherwise retire."""
    fake, calls = _fake_pass([27.0, 29.6, 34.2])
    monkeypatch.setattr(tts, "_synthesize_pass", fake)
    out = tts.synthesize_all(_script())
    assert len(calls) == 3
    assert tts._rate_pct(calls[2]) < tts._rate_pct(calls[1]) < 0
    assert sum(s["duration"] for s in out) == pytest.approx(35.0)


def test_the_repair_stops_once_the_rate_is_pinned_at_its_clamp(monkeypatch):
    """A script so short that even the slowest natural rate cannot save it is
    not re-synthesised again at the same clamped rate — that would only
    repeat identical audio."""
    fake, calls = _fake_pass([8.0, 10.0, 12.0])
    monkeypatch.setattr(tts, "_synthesize_pass", fake)
    with pytest.raises(tts.NarrationLengthError):
        tts.synthesize_all(_script())
    assert len(calls) == 2
    assert tts._rate_pct(calls[1]) == tts.ADAPTIVE_RATE_MIN_PCT


def test_the_retire_error_is_still_a_runtime_error():
    """Every existing handler that catches RuntimeError keeps working."""
    assert issubclass(tts.NarrationLengthError, RuntimeError)


def test_each_run_starts_from_the_house_rate(monkeypatch):
    """A rate chosen for one story must not leak into the next video."""
    monkeypatch.setattr(tts, "_active_rate", "-15%")
    fake, calls = _fake_pass([34.0])
    monkeypatch.setattr(tts, "_synthesize_pass", fake)
    tts.synthesize_all(_script())
    assert calls[0] == tts.TTS_RATE


def test_the_opening_cut_survives_the_final_rescale(monkeypatch):
    """The second bug found on 2026-09-17. The shot plan was built twice — once
    with the opening cut, then again after scaling WITHOUT it — and the second
    one is what gets rendered. The early cut therefore never reached a video."""
    fake, _ = _fake_pass([34.0])
    monkeypatch.setattr(tts, "_synthesize_pass", fake)
    out = tts.synthesize_all(_script())
    first, second = out[0], out[1]
    # The first segment's opening sentence is long enough to be split, so the
    # opening segment gains a shot the others do not.
    assert len(first["shots"]) > len(second["shots"])
    assert first["shots"][0]["duration"] == pytest.approx(tts.FIRST_CUT_SECONDS)


# ------------------------------------------------------- retirement accounting

def test_a_permanent_failure_retires_on_the_first_attempt(monkeypatch):
    recorded = {}
    monkeypatch.setattr(packet, "ledger_entry",
                        lambda sid: {"status": "queued", "attempts": 1})
    monkeypatch.setattr(packet, "record_status",
                        lambda story, status, note="", **kw: recorded.update(
                            status=status, note=note))
    packet.mark_failed({"story_id": "st-x"}, "NarrationLengthError: too short",
                       permanent=True)
    assert recorded["status"] == "failed"
    assert "retired without retry" in recorded["note"]


def test_an_ordinary_failure_still_returns_to_the_queue(monkeypatch):
    """A network blip must keep its retries — only script-level failures skip them."""
    recorded = {}
    monkeypatch.setattr(packet, "ledger_entry",
                        lambda sid: {"status": "queued", "attempts": 1})
    monkeypatch.setattr(packet, "record_status",
                        lambda story, status, note="", **kw: recorded.update(status=status))
    packet.mark_failed({"story_id": "st-x"}, "ConnectionError: blip")
    assert recorded["status"] == "proposed"


def test_a_published_story_is_never_marked_failed(monkeypatch):
    called = []
    monkeypatch.setattr(packet, "ledger_entry",
                        lambda sid: {"status": "published", "attempts": 1})
    monkeypatch.setattr(packet, "record_status",
                        lambda *a, **k: called.append(1))
    packet.mark_failed({"story_id": "st-x"}, "anything", permanent=True)
    assert called == []


def test_main_retires_only_deterministic_failures():
    """Pinned against the source so a refactor cannot quietly drop it.

    Two failures reproduce identically on every retry and are retired at
    once: a wrong-length narration, and a payoff line the research marked
    wrong with no correction (grounding.enforce_publication_policy says the
    story is marked failed; without `permanent` it went back to "proposed"
    and burned two more slots). Nothing else is retired early."""
    import inspect
    from agent import main
    src = inspect.getsource(main)
    assert ("permanent=isinstance(e, (tts.NarrationLengthError,\n"
            "                                             grounding.ContradictedFinalSegment))") in src
    assert '"tts_rate": tts.active_rate()' in src
