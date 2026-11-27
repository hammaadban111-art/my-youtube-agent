"""agent/length_check.py: speak a packet's stories when it lands, not on the day.

The incident: 2026-11-23 06:07Z, Coelacanth ran 45.7s at +8% and 40.3s at the
+25% clamp against a 35s video, so its slot was lost two days after the packet
holding it was committed.
"""
from datetime import datetime, timezone

import pytest

from agent import config, length_check, packet, tts

NOW = datetime(2026, 11, 21, 15, 30, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _setup(monkeypatch):
    monkeypatch.setattr(config, "VIDEO_LENGTH_SECONDS", 35)
    monkeypatch.setattr(tts, "_active_rate", None)
    statuses = {"st-published": "published"}
    monkeypatch.setattr(packet, "status_of",
                        lambda s: statuses.get(s.get("story_id"), "proposed"))


def _story(story_id, subject, slot_utc):
    return {"story_id": story_id, "topic_subject": subject,
            "slot": {"utc": slot_utc},
            "segments": [{"narration": subject + " narration."}]}


def _synth_by_subject(lengths):
    """A fake synthesize_all: each subject's spoken length, or an exception."""
    def synth(script):
        value = lengths[script["topic_subject"]]
        if isinstance(value, Exception):
            raise value
        return [{"source_duration": value, "duration": 35.0}]
    return synth


def test_the_coelacanth_case_fails_days_before_its_slot():
    pk = {"stories": [_story("st-coel", "Coelacanth", "2026-11-23T0607Z"),
                      _story("st-fine", "Lake Kaindy", "2026-11-23T1107Z")]}
    synth = _synth_by_subject({
        "Coelacanth": tts.NarrationLengthError("Narration is 40.3s ..."),
        "Lake Kaindy": 35.4})
    rows = {r["story_id"]: r["verdict"]
            for r in length_check.check_packet(pk, now=NOW, synth=synth)}
    assert rows == {"st-coel": "too-long-or-short", "st-fine": "ok"}


def test_a_story_that_only_just_fits_is_a_warning_not_a_failure():
    pk = {"stories": [_story("st-edge", "Edge", "2026-11-23T0607Z")]}
    # 40.0 / 35 = 1.143: inside the 1.15 limit today, but within the margin
    # that day-to-day voice drift can push over it.
    [row] = length_check.check_packet(pk, now=NOW,
                                      synth=_synth_by_subject({"Edge": 40.0}))
    assert row["verdict"] == "at-risk"


def test_past_and_published_stories_are_not_spoken():
    pk = {"stories": [_story("st-past", "Past", "2026-11-20T0607Z"),
                      _story("st-published", "Done", "2026-11-24T0607Z"),
                      _story("st-next", "Next", "2026-11-24T1107Z")]}
    rows = length_check.check_packet(pk, now=NOW,
                                     synth=_synth_by_subject({"Next": 34.0}))
    assert [r["story_id"] for r in rows] == ["st-next"]


def test_a_voice_outage_is_reported_but_never_blames_the_story():
    pk = {"stories": [_story("st-x", "X", "2026-11-23T0607Z")]}
    synth = _synth_by_subject({"X": RuntimeError("edge-tts failed on every voice")})
    [row] = length_check.check_packet(pk, now=NOW, synth=synth)
    assert row["verdict"] == "unchecked"


def test_the_run_goes_red_only_for_a_wrong_length_story(monkeypatch, capsys):
    pk = {"stories": [_story("st-coel", "Coelacanth", "2026-12-23T0607Z"),
                      _story("st-x", "X", "2026-12-23T1107Z")]}
    monkeypatch.setattr(packet, "load_packet", lambda: pk)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.setattr(tts, "synthesize_all", _synth_by_subject({
        "Coelacanth": tts.NarrationLengthError("too long"),
        "X": RuntimeError("outage")}))
    assert length_check.main() == 1
    out = capsys.readouterr().out
    assert "::error title=Story will fail on its day%3A Coelacanth::" in out

    monkeypatch.setattr(tts, "synthesize_all", _synth_by_subject({
        "Coelacanth": 35.0, "X": RuntimeError("outage")}))
    assert length_check.main() == 0


def test_the_real_workdir_is_restored_even_when_synthesis_fails():
    before = config.WORKDIR
    pk = {"stories": [_story("st-x", "X", "2026-11-23T0607Z")]}
    length_check.check_packet(pk, now=NOW, synth=_synth_by_subject(
        {"X": tts.NarrationLengthError("too long")}))
    assert config.WORKDIR == before
