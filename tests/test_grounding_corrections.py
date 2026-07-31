from agent.grounding import apply_corrections


def test_apply_corrections_by_structural_index_despite_disjoint_claim_text():
    """Guards against claim substring matching failure by verifying corrections apply via structural segment index."""
    script = {
        "segments": [
            {"narration": "First segment untouched."},
            {"narration": "footsteps leading straight into his attic"},
            {"narration": "Third segment untouched."},
        ]
    }
    report = {
        "verdicts": [
            {
                "verdict": "CONTRADICTED",
                "segment_index": 1,
                "claim": "completely distinct paraphrase with no overlapping vocabulary",
                "correction": "footprints leading to the roof",
            }
        ]
    }

    result = apply_corrections(script, report)
    assert result["segments"][1]["narration"] == "footprints leading to the roof"
    assert report.get("corrections_applied") == 1


def test_apply_corrections_out_of_range_index_skipped():
    """Guards against crashes when a verdict provides an out-of-range segment index."""
    script = {
        "segments": [
            {"narration": "Segment zero narration."},
            {"narration": "Segment one narration."},
        ]
    }
    report = {
        "verdicts": [
            {
                "verdict": "CONTRADICTED",
                "segment_index": 99,
                "claim": "Some claim",
                "correction": "Replacement narration",
            }
        ]
    }

    result = apply_corrections(script, report)
    assert report.get("corrections_skipped") == 1
    assert result["segments"][0]["narration"] == "Segment zero narration."
    assert result["segments"][1]["narration"] == "Segment one narration."


def test_apply_corrections_invalid_segment_index_types():
    """Guards against errors when segment index is non-numeric or None."""
    script = {
        "segments": [
            {"narration": "Segment zero narration."}
        ]
    }
    report = {
        "verdicts": [
            {
                "verdict": "CONTRADICTED",
                "segment_index": None,
                "claim": "Claim 1",
                "correction": "Correction 1",
            },
            {
                "verdict": "CONTRADICTED",
                "segment_index": "invalid_index",
                "claim": "Claim 2",
                "correction": "Correction 2",
            },
        ]
    }

    result = apply_corrections(script, report)
    assert report.get("corrections_skipped", 0) >= 1
    assert result["segments"][0]["narration"] == "Segment zero narration."


def test_apply_corrections_supported_verdict_ignored():
    """Guards against modifying segment narration when verdict is SUPPORTED."""
    script = {
        "segments": [
            {"narration": "Unchanged supported narration."}
        ]
    }
    report = {
        "verdicts": [
            {
                "verdict": "SUPPORTED",
                "segment_index": 0,
                "claim": "True claim",
                "correction": "Should not be applied",
            }
        ]
    }

    result = apply_corrections(script, report)
    assert result["segments"][0]["narration"] == "Unchanged supported narration."
    assert "corrections_applied" not in report
