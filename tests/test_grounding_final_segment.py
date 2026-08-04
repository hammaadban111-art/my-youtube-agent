import agent.grounding
from agent.grounding import ground_script


def test_final_segment_not_grounded_when_unverified(monkeypatch):
    """Guards against false positive final_segment_grounded when final segment claims are missing from verdicts."""
    monkeypatch.setattr(agent.grounding, "fetch_source", lambda subject: [("Some Title", "article text")])
    monkeypatch.setattr(
        agent.grounding,
        "verify_claims",
        lambda claims, sources, article=None, segments=None: [
            {"segment_index": 0, "verdict": "SUPPORTED"},
            {"segment_index": 1, "verdict": "SUPPORTED"},
        ],
    )

    script = {
        "topic_subject": "Ghost Ship",
        "segments": [{"narration": "seg0"}, {"narration": "seg1"}, {"narration": "seg2"}, {"narration": "seg3"}],
    }
    report = ground_script(script)
    assert report["final_segment_grounded"] is False


def test_final_segment_grounded_when_verified(monkeypatch):
    """Guards against missing final_segment_grounded flag when final segment is verified."""
    monkeypatch.setattr(agent.grounding, "fetch_source", lambda subject: [("Some Title", "article text")])
    monkeypatch.setattr(
        agent.grounding,
        "verify_claims",
        lambda claims, sources, article=None, segments=None: [
            {"segment_index": 3, "verdict": "SUPPORTED"},
        ],
    )

    script = {
        "topic_subject": "Ghost Ship",
        "segments": [{"narration": "seg0"}, {"narration": "seg1"}, {"narration": "seg2"}, {"narration": "seg3"}],
    }
    report = ground_script(script)
    assert report["final_segment_grounded"] is True


def test_final_segment_contradicted_flag(monkeypatch):
    """Guards against unflagged contradicted final segment when last segment has a CONTRADICTED verdict."""
    monkeypatch.setattr(agent.grounding, "fetch_source", lambda subject: [("Some Title", "article text")])
    monkeypatch.setattr(
        agent.grounding,
        "verify_claims",
        lambda claims, sources, article=None, segments=None: [
            {"segment_index": 3, "verdict": "CONTRADICTED"},
        ],
    )

    script = {
        "topic_subject": "Ghost Ship",
        "segments": [{"narration": "seg0"}, {"narration": "seg1"}, {"narration": "seg2"}, {"narration": "seg3"}],
    }
    report = ground_script(script)
    assert report["final_segment_contradicted"] is True


def test_final_segment_string_index_parsing(monkeypatch):
    """Guards against string segment_index breaking final segment detection."""
    monkeypatch.setattr(agent.grounding, "fetch_source", lambda subject: [("Some Title", "article text")])
    monkeypatch.setattr(
        agent.grounding,
        "verify_claims",
        lambda claims, sources, article=None, segments=None: [
            {"segment_index": "3", "verdict": "SUPPORTED"},
        ],
    )

    script = {
        "topic_subject": "Ghost Ship",
        "segments": [{"narration": "seg0"}, {"narration": "seg1"}, {"narration": "seg2"}, {"narration": "seg3"}],
    }
    report = ground_script(script)
    assert report["final_segment_grounded"] is True

