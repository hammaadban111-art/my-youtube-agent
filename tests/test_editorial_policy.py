"""Fact-check publication policy, experiment metadata, and the opening cut.

Three changes from 2026-09-09, all of them about the same underlying gap: the
pipeline measured things it then did nothing with.

  - 31 of 113 published videos carried a contradicted claim. Nearly all were
    auto-corrected, but nothing distinguished "contradicted and fixed" from
    "contradicted and shipped", and the final segment — the payoff line — got
    no special treatment.
  - Every video used one voice, one structure and one title shape, and no field
    recorded which, so no format question was answerable afterwards.
  - The median retention curve's steepest fall is in the first 1-2 seconds, and
    the opening was a single held shot across exactly that moment.
"""
import pytest

from agent import benchmark, grounding, packet, tts


# ------------------------------------------------- fact-check publication policy

def _report(verdicts, final_index=4, status="checked"):
    return {"status": status, "final_segment_index": final_index,
            "verdicts": verdicts}


def _verdict(index, verdict="CONTRADICTED", correction=None):
    return {"segment_index": index, "verdict": verdict,
            "correction": correction, "claim": f"claim about segment {index}",
            "note": "the article says otherwise"}


def test_a_corrected_contradiction_is_not_a_problem():
    """apply_corrections rewrote the narration; the corrected sentence is what
    gets spoken, so there is nothing left to report."""
    report = _report([_verdict(1, correction="the corrected sentence")])
    assert grounding.enforce_publication_policy(report) == []


def test_an_uncorrected_contradiction_off_the_payoff_is_reported_not_blocked():
    report = _report([_verdict(1)])
    problems = grounding.enforce_publication_policy(report)
    assert len(problems) == 1
    assert problems[0]["segment_index"] == 1


def test_an_uncorrected_contradiction_in_the_final_segment_blocks_publication():
    """The closing line is the answer the whole video promised. A wrong one is
    the version viewers remember, so this costs a slot rather than credibility."""
    report = _report([_verdict(4)])
    with pytest.raises(grounding.ContradictedFinalSegment) as excinfo:
        grounding.enforce_publication_policy(report)
    assert "final segment" in str(excinfo.value)


def test_a_corrected_final_segment_still_publishes():
    report = _report([_verdict(4, correction="the corrected payoff")])
    assert grounding.enforce_publication_policy(report) == []


def test_a_misleading_verdict_does_not_block():
    """MISLEADING goes through apply_corrections too, and is a weaker claim
    than CONTRADICTED. Only an uncorrected contradiction is a blocker."""
    report = _report([_verdict(4, verdict="MISLEADING")])
    assert grounding.enforce_publication_policy(report) == []


def test_a_failed_grounding_run_is_not_a_contradiction():
    """No article, or a network failure, means nothing was checked. That is
    already recorded as a degradation; it must not read as a false claim."""
    assert grounding.enforce_publication_policy(
        {"status": "error", "verdicts": []}) == []


def test_uncorrected_contradictions_ignores_supported_claims():
    report = _report([_verdict(1, verdict="SUPPORTED"),
                      _verdict(2, verdict="SILENT"),
                      _verdict(3)])
    assert [v["segment_index"]
            for v in grounding.uncorrected_contradictions(report)] == [3]


# ----------------------------------------------------- experiment metadata

def test_declared_editorial_fields_are_carried_through():
    story = {"editorial": {"series": "Solved", "experiment": "title_style",
                           "arm": "absurd-number", "voice": "en-US-AriaNeural"}}
    out = packet.editorial_metadata(story)
    assert out["series"] == "Solved"
    assert out["arm"] == "absurd-number"
    assert out["voice"] == "en-US-AriaNeural"


def test_an_absent_editorial_block_stays_absent():
    """Not defaulted to 'standard'. 114 historical videos are not a control
    group for an experiment nobody ran."""
    assert packet.editorial_metadata({}) == {}
    assert packet.editorial_metadata({"editorial": None}) == {}


def test_unknown_editorial_fields_are_dropped_from_the_record():
    out = packet.editorial_metadata({"editorial": {"series": "Solved",
                                                   "nonsense": "x"}})
    assert out == {"series": "Solved"}


def test_an_unknown_field_is_reported_by_validation():
    problems = packet.validate_editorial({"editorial": {"vibe": "spooky"}}, "story 1")
    assert any("not a recognised field" in p for p in problems)


def test_an_arm_without_an_experiment_is_rejected():
    """An arm with nothing to compare against records a variation that can
    never be analysed — the failure only shows up weeks later."""
    problems = packet.validate_editorial({"editorial": {"arm": "b"}}, "story 1")
    assert any("must be set together" in p for p in problems)


def test_an_experiment_without_an_arm_is_rejected():
    problems = packet.validate_editorial(
        {"editorial": {"experiment": "voice"}}, "story 1")
    assert any("must be set together" in p for p in problems)


def test_a_matched_experiment_and_arm_validate():
    assert packet.validate_editorial(
        {"editorial": {"experiment": "voice", "arm": "aria"}}, "s") == []


def test_editorial_must_be_an_object():
    assert packet.validate_editorial({"editorial": "Solved"}, "s")


def test_no_editorial_block_is_valid():
    assert packet.validate_editorial({}, "s") == []


# --------------------------------------------------------------- voice arm

def test_a_story_may_choose_its_own_voice():
    assert tts.voice_for({"editorial": {"voice": "en-US-AriaNeural"}}) == "en-US-AriaNeural"


def test_a_story_without_a_voice_uses_the_configured_default():
    from agent import config
    assert tts.voice_for({}) == config.VOICE
    assert tts.voice_for({"editorial": {}}) == config.VOICE


# -------------------------------------------------------------- opening cut

def test_the_opening_shot_is_cut_inside_the_drop_window():
    """The steepest median drop is at 2-6% of the video — about 1-2 seconds on
    a 35s Short — and the opening used to be one held shot across it."""
    sentences = [{"start": 0.0, "duration": 2.8}, {"start": 2.8, "duration": 2.0}]
    shots = tts._plan_shots(sentences, 4.8, is_opening=True)
    assert shots[0]["duration"] == pytest.approx(tts.FIRST_CUT_SECONDS)
    assert len(shots) == 3


def test_only_the_first_segment_gets_the_opening_cut():
    """The measurement points at the start of the VIDEO. Re-cutting every
    segment would change the pacing of the whole edit instead."""
    sentences = [{"start": 0.0, "duration": 2.8}, {"start": 2.8, "duration": 2.0}]
    assert len(tts._plan_shots(sentences, 4.8)) == 2


def test_a_short_opening_is_left_alone():
    """There is already a cut inside the window; adding one would only make a
    flicker."""
    sentences = [{"start": 0.0, "duration": 1.5}, {"start": 1.5, "duration": 2.0}]
    shots = tts._plan_shots(sentences, 3.5, is_opening=True)
    assert len(shots) == 2
    assert shots[0]["duration"] == pytest.approx(1.5)


def test_shots_still_cover_the_whole_segment_with_no_gaps():
    """The invariant the whole shot planner exists to keep."""
    sentences = [{"start": 0.0, "duration": 3.4}, {"start": 3.4, "duration": 2.0}]
    shots = tts._plan_shots(sentences, 5.4, is_opening=True)
    assert shots[0]["start"] == 0.0
    for a, b in zip(shots, shots[1:]):
        assert a["start"] + a["duration"] == pytest.approx(b["start"])
    assert shots[-1]["start"] + shots[-1]["duration"] == pytest.approx(5.4)


def test_an_empty_segment_still_yields_one_covering_shot():
    assert tts._plan_shots([], 5.0, is_opening=True) == [
        {"start": 0.0, "duration": 5.0}]


# ------------------------------------------------------- category classifier

def test_the_classifier_sees_natural_phenomena_as_science():
    """The 2026-09-09 coverage failure: 67 of 114 videos matched nothing, and
    the misses were overwhelmingly geology and natural phenomena — the
    categories the niche scan rates highest."""
    for title in ("The Antarctic Glacier That Bleeds Bright Red",
                  "The Silent Lake That Erased an Entire Valley Overnight",
                  "The Earth's Eternal Lightning Storm Solved",
                  "The 1838 Submarine That Literally Ate Its Crew"):
        assert benchmark.categorize(title) == "science_nature", title


def test_the_classifier_sees_physical_history_as_historical():
    for title in ("The 1,600-Year-Old Iron Pillar That Refuses to Rust",
                  "A Metal Scroll Lists 64 Treasures Nobody Has Found"):
        assert benchmark.categorize(title) == "historical", title


def test_coverage_is_reported_rather_than_assumed():
    report = benchmark.categorization_coverage([
        "The Antarctic Glacier That Bleeds Bright Red",
        "Something with no matching keyword whatsoever",
    ])
    assert report["total"] == 2
    assert report["classified"] == 1
    assert report["coverage"] == 0.5


def test_empty_input_reports_zero_coverage_not_a_crash():
    assert benchmark.categorization_coverage([])["coverage"] == 0.0


# ---------------------------------------------------------- title diversity

def test_the_historical_monoculture_would_be_rejected():
    """83.7% of the first 129 published videos opened with 'The'. The gate
    exists so the next packet cannot quietly go back to that."""
    titles = ["The Day Something Happened"] * 108 + [f"A Thing {i}" for i in range(21)]
    problems = packet.validate_title_diversity(titles)
    assert problems and "open with 'the'" in problems[0]


def test_a_varied_packet_passes():
    """2026-W38's real shape: its worst opener is 10 of 28 (36%)."""
    titles = (["A Thing"] * 10 + ["Someone Did"] * 2 + ["The Thing"] * 2
              + ["One Man"] * 2 + ["Two Girls"] * 2
              + [f"Unique Opener{i} Here" for i in range(10)])
    assert packet.validate_title_diversity(titles) == []


def test_a_small_recovery_packet_is_not_judged():
    """Two stories sharing an opener is not evidence of a monoculture."""
    assert packet.validate_title_diversity(["The A", "The B", "The C"]) == []


def test_openers_are_case_and_quote_folded():
    titles = ['"The" one', "the two", "The three", "A four", "B five",
              "C six", "D seven", "E eight"]
    counts = packet.title_opener_counts(titles)
    assert counts["the"] == 3


def test_the_whole_packet_is_judged_not_the_unpublished_remainder():
    """The regression caught on 2026-09-12 before it shipped. Judging only the
    stories still to publish means the sample shrinks as the week drains, so an
    untouched packet passes on Monday and fails on Friday — and a gate that
    starts failing mid-week blocks every remaining slot over a decision nobody
    can change any more.

    Real numbers from 2026-W38: 10/28 (36%) as authored, but 5/12 (42%) across
    the unpublished tail."""
    as_authored = ["A one", "A two", "A three", "A four", "A five",
                   "A six", "A seven", "A eight", "A nine", "A ten"] + [
        f"Word{i} rest of it" for i in range(18)]
    assert packet.validate_title_diversity(as_authored) == []

    unpublished_tail = ["A one", "A two", "A three", "A four", "A five",
                        "B", "C", "D", "E", "F", "G", "H"]
    # The tail alone WOULD trip the limit — which is exactly why the caller
    # must pass the whole packet.
    assert packet.validate_title_diversity(unpublished_tail)


def test_blank_titles_do_not_count_as_an_opener():
    titles = ["", "   ", "A real title", "Another real one", "Third one here",
              "Fourth one here", "Fifth one here", "Sixth one here",
              "Seventh one here", "Eighth one here"]
    assert packet.validate_title_diversity(titles) == []
