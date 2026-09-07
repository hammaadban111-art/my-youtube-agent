import agent.grounding as grounding
from agent.grounding import apply_corrections, ground_script


def test_misleading_verdict_with_correction_rewrites_narration():
    """A MISLEADING verdict with a correction rewrites the tagged segment's narration."""
    script = {
        "segments": [
            {"narration": "Animals turn to solid rock in the lake."},
            {"narration": "Another segment."},
        ]
    }
    report = {
        "verdicts": [
            {
                "claim": "Lake turns animals to stone",
                "segment_index": 0,
                "verdict": "MISLEADING",
                "note": "Natural calcification, not turning to rock",
                "correction": "Animals calcify in the soda lake's alkaline water.",
            }
        ]
    }
    updated = apply_corrections(script, report)
    assert updated["segments"][0]["narration"] == "Animals calcify in the soda lake's alkaline water."
    assert updated["segments"][1]["narration"] == "Another segment."
    assert report["corrections_applied"] == 1
    assert report["misleading_corrections_applied"] == 1


def test_misleading_counted_and_accuracy_flags_populated(monkeypatch):
    """misleading is counted and accuracy_flags is populated with claim/segment_index/verdict/note."""
    monkeypatch.setattr(grounding, "fetch_source", lambda subject: [("Lake Natron", "Alkaline lake text...")])
    # The MISLEADING verdict now arrives from the researched packet rather than
    # from a model call at run time. The lexical pass can only ever say
    # SUPPORTED or SILENT; apply_research is what lets a real finding override
    # it, and this is the test that the override reaches the report.
    script = {
        "topic_subject": "Lake Natron",
        "factual_claims": [{"text": "Animals turn to rock", "segment_index": 0}],
        "segments": [{"narration": "The water turns animals to solid rock."}],
        "verification": [
            {
                "claim": "Animals turn to rock",
                "segment_index": 0,
                "verdict": "MISLEADING",
                "note": "Sensationalized phrasing",
                "correction": "Corrected line",
            }
        ],
    }
    report = ground_script(script)
    assert report["misleading"] == 1
    assert len(report["accuracy_flags"]) == 1
    assert report["accuracy_flags"][0]["claim"] == "Animals turn to rock"
    assert report["accuracy_flags"][0]["segment_index"] == 0
    assert report["accuracy_flags"][0]["verdict"] == "MISLEADING"
    assert report["accuracy_flags"][0]["note"] == "Sensationalized phrasing"


def test_every_fetched_article_is_searched_for_corroboration(monkeypatch):
    """All three fetched articles form the corpus, not just the first.

    The old version of this test asserted three article titles reached a
    prompt. There is no prompt any more, so it asserts the thing that
    actually mattered underneath: a claim corroborated only by the THIRD
    article still reads SUPPORTED."""
    monkeypatch.setattr(
        grounding,
        "fetch_source",
        lambda subject, max_articles=3: [
            ("Title Alpha", "wholly unrelated content about bridges"),
            ("Title Beta", "wholly unrelated content about pottery"),
            ("Title Gamma", "the Somerton man was found on Glenelg beach in 1948"),
        ],
    )

    script = {
        "topic_subject": "Multi Topic",
        "factual_claims": [
            {"text": "The Somerton man was found on Glenelg beach in 1948",
             "segment_index": 0}],
        "segments": [{"narration": "A body on Glenelg beach, 1948."}],
    }
    report = ground_script(script)

    assert report["claims_checked"] == 1
    assert report["supported"] == 1
    assert report["verdicts"][0]["method"] == "wikipedia-lexical"


def test_a_claim_the_article_does_not_carry_reads_silent_not_supported(monkeypatch):
    """The Tibesti Mountains failure, in one assertion.

    On 2026-08-04 a Lake Natron script was grounded against an article about a
    mountain range in Chad and the dashboard rendered "Facts verified". Nothing
    lexical can be fooled that way: an article that shares none of the claim's
    words corroborates none of it."""
    monkeypatch.setattr(
        grounding, "fetch_source",
        lambda subject, max_articles=3: [
            ("Tibesti Mountains",
             "The Tibesti Mountains are a range in the central Sahara.")])

    script = {
        "topic_subject": "Lake Natron",
        "factual_claims": [
            {"text": "Lake Natron calcifies animal carcasses in its alkaline water",
             "segment_index": 0}],
        "segments": [{"narration": "The lake calcifies whatever falls in."}],
    }
    report = ground_script(script)

    assert report["supported"] == 0
    assert report["silent"] == 1
    # Zero coverage against the only article found is exactly the state that
    # must never render as verified.
    assert report["unverified_source"] is True


def test_a_wrong_number_is_never_corroborated(monkeypatch):
    """Numbers are matched exactly. Word overlap alone cannot carry a claim
    whose figure the article does not contain — that is the most common way a
    written line goes subtly wrong."""
    article = ("The Somerton man was found on Glenelg beach near Adelaide "
               "in 1948, and the case remains unsolved.")
    monkeypatch.setattr(grounding, "fetch_source",
                        lambda subject, max_articles=3: [("Tamam Shud", article)])

    script = {
        "topic_subject": "Tamam Shud",
        "factual_claims": [
            {"text": "The Somerton man was found on Glenelg beach near Adelaide in 1963",
             "segment_index": 0}],
        "segments": [{"narration": "Found at Glenelg in 1963."}],
    }
    report = ground_script(script)

    assert report["supported"] == 0
    assert "1963" in report["verdicts"][0]["note"]


def test_researched_support_promotes_a_silent_claim(monkeypatch):
    """A claim the run's own article does not mention, but the week's research
    checked against a source chosen for it, is not reported as uncovered."""
    monkeypatch.setattr(grounding, "fetch_source",
                        lambda subject, max_articles=3: [("Something Else", "nothing relevant")])

    script = {
        "topic_subject": "Lead Masks",
        "factual_claims": [{"text": "Two bodies were found on Vintem Hill",
                            "segment_index": 0}],
        "segments": [{"narration": "Two men, lead masks, one hill."}],
        "verification": [{"segment_index": 0, "verdict": "SUPPORTED",
                          "note": "confirmed against the case file",
                          "source": "https://en.wikipedia.org/wiki/Lead_Masks_Case"}],
    }
    report = ground_script(script)

    assert report["supported"] == 1
    assert report["verdicts"][0]["method"] == "claude-research"
    assert report["verifier"] == "wikipedia-lexical+claude-research"


def test_silent_verdict_rewrites_nothing():
    """A SILENT verdict rewrites nothing, even if a correction string is present."""
    script = {"segments": [{"narration": "Original narration line."}]}
    report = {
        "verdicts": [
            {
                "claim": "Some detail",
                "segment_index": 0,
                "verdict": "SILENT",
                "note": "Article is silent on this detail",
                "correction": "This should not be applied",
            }
        ]
    }
    updated = apply_corrections(script, report)
    assert updated["segments"][0]["narration"] == "Original narration line."
    assert report.get("corrections_applied", 0) == 0
