from agent.script_writer import validate_script


def _valid_base_script():
    return {
        "segments": [
            {
                "narration": "In 1923 a strange light appeared.",
                "visual_query": "east african salt flat lake",
                "visual_fallback": "fog",
            }
        ],
        "hook_candidates": [
            {"text": "In 1923 a strange light appeared", "stopping_power": 9, "specificity": 9, "open_loop": 9, "no_context_required": 9, "total": 36, "why": "strange detail"},
            {"text": "Candidate two sentence text here", "stopping_power": 8, "specificity": 8, "open_loop": 8, "no_context_required": 8, "total": 32, "why": "angle two"},
            {"text": "Candidate three sentence text here", "stopping_power": 7, "specificity": 7, "open_loop": 7, "no_context_required": 7, "total": 28, "why": "angle three"},
        ],
        "hook_choice": {"chosen_index": 0, "reason": "highest total"},
        "factual_claims": [{"text": "Strange light appeared in 1923", "segment_index": 0}],
    }


def test_bare_banned_generic_visual_query_reported():
    """A script whose visual_query is a bare banned generic is reported as a problem."""
    script = _valid_base_script()
    script["segments"][0]["visual_query"] = "dark background"
    problems = validate_script(script)

    assert any("bare banned generic query" in p for p in problems)


def test_invalid_word_count_visual_query_reported():
    """A 2-word or 7-word visual_query is reported as a problem."""
    script_2w = _valid_base_script()
    script_2w["segments"][0]["visual_query"] = "dark room"
    problems_2w = validate_script(script_2w)
    assert any("2 words — must be 3-6 words" in p for p in problems_2w)

    script_7w = _valid_base_script()
    script_7w["segments"][0]["visual_query"] = "one two three four five six seven"
    problems_7w = validate_script(script_7w)
    assert any("7 words — must be 3-6 words" in p for p in problems_7w)


def test_missing_visual_fallback_reported():
    """A missing visual_fallback is reported as a problem."""
    script = _valid_base_script()
    script["segments"][0].pop("visual_fallback")
    problems = validate_script(script)

    assert any("missing a visual_fallback" in p for p in problems)
