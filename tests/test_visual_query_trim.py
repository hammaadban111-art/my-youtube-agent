"""An over-long visual_query must never cost a scheduled slot.

On 2026-08-13 the 01:07 run died with:

    RuntimeError: Script generation failed validation twice: Segment 0
    visual_query ('lake hillier western australia pink water aerial') is 7
    words — must be 3-6 words.; Segment 4 visual_query ('lake hillier pink
    water shoreline forest boundary') is 7 words — must be 3-6 words.

Two words over the limit, twice, and the whole video was lost. agent/visuals.py
already falls through to visual_fallback when a query returns nothing, so the
downstream cost of a long query is one rung of a ladder built for it. These
tests pin the repair, and pin the cases it must NOT paper over.
"""
from agent import script_writer


def _seg(query):
    return {"visual_query": query, "visual_fallback": "pink lake"}


def test_the_real_2026_08_13_queries_are_trimmed_not_rejected():
    segments = [
        _seg("lake hillier western australia pink water aerial"),
        _seg("lake hillier pink water shoreline forest boundary"),
    ]
    notes = script_writer.trim_long_visual_queries(segments)

    assert len(notes) == 2
    assert segments[0]["visual_query"] == "lake hillier western australia pink water"
    assert segments[1]["visual_query"] == "lake hillier pink water shoreline forest"
    # And the trimmed forms now pass the validator that rejected the originals.
    for seg in segments:
        assert 3 <= len(seg["visual_query"].split()) <= script_writer.MAX_VISUAL_QUERY_WORDS


def test_the_subject_anchor_survives_trimming():
    """Trimming keeps the LEADING words because that is where the model puts
    the subject. A query that loses its anchor would be worse than useless."""
    segments = [_seg("lake hillier western australia pink water aerial")]
    script_writer.trim_long_visual_queries(segments)
    assert segments[0]["visual_query"].startswith("lake hillier")


def test_queries_within_the_limit_are_untouched():
    segments = [_seg("lake hillier pink water"), _seg("aerial coastline dusk")]
    assert script_writer.trim_long_visual_queries(segments) == []
    assert segments[0]["visual_query"] == "lake hillier pink water"
    assert segments[1]["visual_query"] == "aerial coastline dusk"


def test_a_query_at_exactly_the_limit_is_untouched():
    at_limit = "one two three four five six"
    segments = [_seg(at_limit)]
    assert script_writer.trim_long_visual_queries(segments) == []
    assert segments[0]["visual_query"] == at_limit


def test_too_few_words_is_left_alone_for_the_re_ask():
    """Trimming only ever shortens. A two-word query cannot be repaired by
    inventing terms, so it stays a real validation failure."""
    segments = [_seg("pink lake")]
    assert script_writer.trim_long_visual_queries(segments) == []
    assert segments[0]["visual_query"] == "pink lake"
    problems = script_writer.validate_visual_queries(segments, 1)
    assert any("3-6 words" in p for p in problems)


def test_trimming_into_a_banned_generic_query_is_still_caught():
    """Validation runs AFTER the repair, so a trim that happens to produce a
    banned bare query is still rejected rather than smuggled through."""
    banned = sorted(script_writer.BANNED_GENERIC_QUERIES)[0]
    segments = [_seg(banned + " with several extra trailing words here")]
    script_writer.trim_long_visual_queries(segments)
    if segments[0]["visual_query"].lower() in script_writer.BANNED_GENERIC_QUERIES:
        problems = script_writer.validate_visual_queries(segments, 1)
        assert any("banned generic" in p for p in problems)


def test_non_dict_entries_do_not_crash_the_repair():
    entries = ["not a dict", None, _seg("one two three four five six seven")]
    notes = script_writer.trim_long_visual_queries(entries)
    assert len(notes) == 1
    assert entries[2]["visual_query"] == "one two three four five six"


def test_a_missing_query_is_left_for_validation():
    segments = [{"visual_fallback": "pink lake"}]
    assert script_writer.trim_long_visual_queries(segments) == []
    problems = script_writer.validate_visual_queries(segments, 1)
    assert any("missing a visual_query" in p for p in problems)


def test_a_script_validation_failure_reads_as_plain_english():
    """The dashboard must not show "Run agent: RuntimeError" for this. It did
    on the 2026-08-13 03:29 run, which is no use to a non-engineer."""
    from agent import ci_status
    log = (
        "  File \"/x/agent/script_writer.py\", line 600, in generate_script\n"
        "    raise RuntimeError(\n"
        "RuntimeError: Script generation failed validation twice: Segment 0 "
        "visual_query ('lake hillier western australia pink water aerial') is "
        "7 words - must be 3-6 words.\n"
    )
    summary = ci_status._extract_error(log, "Run agent")
    assert "RuntimeError" not in summary
    assert "script writer" in summary
    assert "No video was published" in summary


def test_our_own_alert_line_cannot_shadow_the_real_cause():
    """notify.alert() prints the exception CLASS with no message, after the
    traceback. A reverse scan used to match that first and report a bare
    "RuntimeError" while the real cause sat above it."""
    from agent import ci_status
    log = (
        "ValueError: the actual thing that went wrong, with detail\n"
        "[notify] Resend responded 200 for: [youtube-agent] Run failed: RuntimeError\n"
    )
    summary = ci_status._extract_error(log, "Run agent")
    assert "the actual thing that went wrong" in summary
