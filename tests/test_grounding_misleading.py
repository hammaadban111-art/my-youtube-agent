import agent.grounding as grounding
from agent.grounding import apply_corrections, ground_script


class MockResponse:
    def __init__(self, text):
        self.text = text


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
    monkeypatch.setattr(
        grounding,
        "verify_claims",
        lambda claims, sources, article=None, segments=None: [
            {
                "claim": "Animals turn to rock",
                "segment_index": 0,
                "verdict": "MISLEADING",
                "note": "Sensationalized phrasing",
                "correction": "Corrected line",
            }
        ],
    )

    script = {
        "topic_subject": "Lake Natron",
        "factual_claims": [{"text": "Animals turn to rock", "segment_index": 0}],
        "segments": [{"narration": "The water turns animals to solid rock."}],
    }
    report = ground_script(script)
    assert report["misleading"] == 1
    assert len(report["accuracy_flags"]) == 1
    assert report["accuracy_flags"][0]["claim"] == "Animals turn to rock"
    assert report["accuracy_flags"][0]["segment_index"] == 0
    assert report["accuracy_flags"][0]["verdict"] == "MISLEADING"
    assert report["accuracy_flags"][0]["note"] == "Sensationalized phrasing"


def test_multi_article_fetch_all_titles_reach_prompt(monkeypatch):
    """Multi-article fetch: three hits are requested and all three titles reach the prompt text."""
    monkeypatch.setattr(
        grounding,
        "fetch_source",
        lambda subject: [
            ("Title Alpha", "Content A"),
            ("Title Beta", "Content B"),
            ("Title Gamma", "Content C"),
        ],
    )

    prompt_captured = []

    class DummyModels:
        def generate_content(self, model, contents):
            prompt_captured.append(contents)
            return MockResponse('{"verdicts": []}')

    class DummyClient:
        def __init__(self, api_key=None):
            self.models = DummyModels()

    monkeypatch.setattr(grounding.genai, "Client", DummyClient)
    monkeypatch.setattr(grounding.gemini_utils, "call_with_retry", lambda fn, label="": fn())

    script = {
        "topic_subject": "Multi Topic",
        "factual_claims": [{"text": "Claim 1", "segment_index": 0}],
        "segments": [{"narration": "Narration 1"}],
    }
    ground_script(script)

    assert len(prompt_captured) == 1
    prompt = prompt_captured[0]
    assert "REFERENCE ARTICLE (Title Alpha):" in prompt
    assert "REFERENCE ARTICLE (Title Beta):" in prompt
    assert "REFERENCE ARTICLE (Title Gamma):" in prompt


def test_verbatim_narration_appears_in_verifier_prompt(monkeypatch):
    """The verbatim narration of the tagged segment actually appears in the prompt sent to the verifier."""
    monkeypatch.setattr(grounding, "fetch_source", lambda subject: [("Title Alpha", "Content A")])

    prompt_captured = []

    class DummyModels:
        def generate_content(self, model, contents):
            prompt_captured.append(contents)
            return MockResponse('{"verdicts": []}')

    class DummyClient:
        def __init__(self, api_key=None):
            self.models = DummyModels()

    monkeypatch.setattr(grounding.genai, "Client", DummyClient)
    monkeypatch.setattr(grounding.gemini_utils, "call_with_retry", lambda fn, label="": fn())

    exact_narration = "The soda lake calcifies animal corpses preserving them like statues."
    script = {
        "topic_subject": "Lake Natron",
        "factual_claims": [{"text": "Animals become statues", "segment_index": 0}],
        "segments": [{"narration": exact_narration}],
    }
    ground_script(script)

    assert len(prompt_captured) == 1
    assert f"Verbatim Narration: {exact_narration}" in prompt_captured[0]


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
