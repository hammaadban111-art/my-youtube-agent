import urllib.error
import agent.grounding as grounding
from agent.grounding import ground_script, fetch_source
from agent.script_writer import validate_script


class MockResponse:
    def __init__(self, text):
        self.text = text


def test_tibesti_mountains_regression_rejected_and_narrowed_retry_used(monkeypatch):
    """The real regression: searching 'Lake Natron calcification phenomenon' returning
    'Tibesti Mountains' first must NOT use 'Tibesti Mountains'; narrowed retry ('Lake Natron')
    is issued and its article reaches the verifier.
    """
    searches_made = []

    def mock_wiki_get(params):
        action = params.get("action")
        if action == "query" and params.get("list") == "search":
            srsearch = params.get("srsearch")
            searches_made.append(srsearch)
            if srsearch == "Lake Natron calcification phenomenon":
                # Wikipedia returned an irrelevant article first
                return {"query": {"search": [{"title": "Tibesti Mountains"}]}}
            elif srsearch == "Lake Natron":
                return {"query": {"search": [{"title": "Lake Natron"}]}}
            return {"query": {"search": []}}
        elif action == "query" and params.get("prop") == "extracts":
            title = params.get("titles")
            if title == "Lake Natron":
                return {"query": {"pages": {"123": {"extract": "Lake Natron is a mineral-rich soda lake in Tanzania."}}}}
            elif title == "Tibesti Mountains":
                return {"query": {"pages": {"456": {"extract": "The Tibesti Mountains are a mountain range in Chad."}}}}
            return {"query": {"pages": {}}}
        return {}

    monkeypatch.setattr(grounding, "_wiki_get", mock_wiki_get)

    sources = fetch_source("Lake Natron calcification phenomenon")
    assert sources is not None
    assert len(sources) == 1
    assert sources[0][0] == "Lake Natron"
    assert "Lake Natron" in searches_made
    assert "Tibesti Mountains" not in [s[0] for s in sources]


def test_subject_with_no_relevant_hit_returns_no_source_found(monkeypatch):
    """A subject with no relevant hit at all returns the no_source_found report
    rather than grounding against an unrelated article.
    """
    def mock_wiki_get(params):
        if params.get("list") == "search":
            return {"query": {"search": [{"title": "Tibesti Mountains"}]}}
        return {"query": {"pages": {}}}

    monkeypatch.setattr(grounding, "_wiki_get", mock_wiki_get)

    script = {
        "topic_subject": "Lake Natron calcification phenomenon",
        "factual_claims": [{"text": "Lake turns animals to stone", "segment_index": 0}],
        "segments": [{"narration": "Animals turn to stone."}],
    }
    report = ground_script(script)
    assert report["status"] == "no_source_found"
    assert report["claims_checked"] == 0
    assert report["source_relevant"] is False
    assert report["unverified_source"] is False


def test_all_silent_verdicts_sets_unverified_source(monkeypatch):
    """All-SILENT verdicts set unverified_source True and coverage 0.0."""
    monkeypatch.setattr(grounding, "fetch_source", lambda subject, max_articles=3: [("Lake Natron", "Lake text")])
    monkeypatch.setattr(
        grounding,
        "verify_claims",
        lambda claims, sources, article=None, segments=None: [
            {"claim": "Claim 1", "segment_index": 0, "verdict": "SILENT", "note": "Uncovered"},
            {"claim": "Claim 2", "segment_index": 1, "verdict": "SILENT", "note": "Uncovered"},
        ],
    )

    script = {
        "topic_subject": "Lake Natron",
        "factual_claims": [
            {"text": "Claim 1", "segment_index": 0},
            {"text": "Claim 2", "segment_index": 1},
        ],
        "segments": [
            {"narration": "Narration 1"},
            {"narration": "Narration 2"},
        ],
    }
    report = ground_script(script)
    assert report["status"] == "checked"
    assert report["unverified_source"] is True
    assert report["coverage"] == 0.0
    assert report["claims_checked"] == 2
    assert report["silent"] == 2


def test_mixed_result_leaves_unverified_source_false(monkeypatch):
    """A mixed result (some SUPPORTED) leaves unverified_source False."""
    monkeypatch.setattr(grounding, "fetch_source", lambda subject, max_articles=3: [("Lake Natron", "Lake text")])
    monkeypatch.setattr(
        grounding,
        "verify_claims",
        lambda claims, sources, article=None, segments=None: [
            {"claim": "Claim 1", "segment_index": 0, "verdict": "SUPPORTED", "note": "Confirmed"},
            {"claim": "Claim 2", "segment_index": 1, "verdict": "SILENT", "note": "Uncovered"},
        ],
    )

    script = {
        "topic_subject": "Lake Natron",
        "factual_claims": [
            {"text": "Claim 1", "segment_index": 0},
            {"text": "Claim 2", "segment_index": 1},
        ],
        "segments": [
            {"narration": "Narration 1"},
            {"narration": "Narration 2"},
        ],
    }
    report = ground_script(script)
    assert report["status"] == "checked"
    assert report["unverified_source"] is False
    assert report["coverage"] == 0.5
    assert report["supported"] == 1
    assert report["silent"] == 1


def test_silent_claim_triggers_per_claim_lookup_and_upgrades_verdict(monkeypatch):
    """A SILENT claim naming a proper noun triggers per-claim lookup and re-verification,
    and a claim upgraded from SILENT to MISLEADING appears in accuracy_flags.
    """
    primary_fetch = True

    def mock_fetch_source(subject, max_articles=3):
        nonlocal primary_fetch
        if subject == "Lake Natron":
            return [("Lake Natron", "Lake Natron is an alkaline lake.")]
        elif subject == "Nick Brandt":
            return [("Nick Brandt", "Photographer Nick Brandt took staged photos of calcified animals.")]
        return None

    monkeypatch.setattr(grounding, "fetch_source", mock_fetch_source)

    verify_calls = []

    def mock_verify_claims(claims, sources, article=None, segments=None):
        verify_calls.append((claims, sources))
        if len(verify_calls) == 1:
            # Primary pass
            return [
                {"claim": "Lake Natron is soda lake", "segment_index": 0, "verdict": "SUPPORTED", "note": "OK"},
                {"claim": "Nick Brandt discovered corpses", "segment_index": 1, "verdict": "SILENT", "note": "Not in lake article"},
            ]
        else:
            # Second pass (per-claim lookup for Nick Brandt)
            return [
                {
                    "claim": "Nick Brandt discovered corpses",
                    "segment_index": 1,
                    "verdict": "MISLEADING",
                    "note": "Photos were staged by Nick Brandt, not discovered naturally",
                    "correction": "Photographer Nick Brandt placed preserved animal corpses along the shore for photographs.",
                }
            ]

    monkeypatch.setattr(grounding, "verify_claims", mock_verify_claims)

    script = {
        "topic_subject": "Lake Natron",
        "factual_claims": [
            {"text": "Lake Natron is soda lake", "segment_index": 0},
            {"text": "Nick Brandt discovered corpses", "segment_index": 1},
        ],
        "segments": [
            {"narration": "Lake Natron is a soda lake."},
            {"narration": "Nick Brandt discovered corpses."},
        ],
    }
    report = ground_script(script)

    assert len(verify_calls) == 2
    assert report["misleading"] == 1
    assert report["silent"] == 0
    assert report["unverified_source"] is False
    assert len(report["accuracy_flags"]) == 1
    assert report["accuracy_flags"][0]["claim"] == "Nick Brandt discovered corpses"
    assert report["accuracy_flags"][0]["verdict"] == "MISLEADING"


def test_extra_fetch_bound_max_3_respected(monkeypatch):
    """The extra-fetch bound (max 3) is respected when many claims are SILENT."""
    fetched_subjects = []

    def mock_fetch_source(subject, max_articles=3):
        fetched_subjects.append(subject)
        if subject == "Lake Natron":
            return [("Lake Natron", "Lake text")]
        return [(f"Article {subject}", f"Text for {subject}")]

    monkeypatch.setattr(grounding, "fetch_source", mock_fetch_source)
    monkeypatch.setattr(
        grounding,
        "verify_claims",
        lambda claims, sources, article=None, segments=None: [
            {"claim": f"Claim about {c['text']}", "segment_index": i, "verdict": "SILENT", "note": "Uncovered"}
            for i, c in enumerate(claims)
        ],
    )

    script = {
        "topic_subject": "Lake Natron",
        "factual_claims": [
            {"text": "Claim about Nick Brandt", "segment_index": 0},
            {"text": "Claim about Ol Doinyo Lengai", "segment_index": 1},
            {"text": "Claim about Great Rift Valley", "segment_index": 2},
            {"text": "Claim about Mount Kilimanjaro", "segment_index": 3},
            {"text": "Claim about Lake Magadi", "segment_index": 4},
        ],
        "segments": [{"narration": f"Seg {i}"} for i in range(5)],
    }
    report = ground_script(script)

    extra_fetches = [s for s in fetched_subjects if s != "Lake Natron"]
    assert len(extra_fetches) <= 3


def test_topic_subject_word_count_validation():
    """A topic_subject with more than 4 words is reported as a problem by validate_script."""
    script = {
        "topic_subject": "Lake Natron calcification phenomenon in Tanzania",
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
    problems = validate_script(script)
    assert any("topic_subject" in p and "at most 4 words" in p for p in problems)


def test_http_429_produces_error_status_not_no_source_found(monkeypatch):
    """A _wiki_get that raises HTTPError 429 must NOT produce a no_source_found report —
    the report's status must be 'error' and it must not claim the subject has no source.
    """
    def mock_wiki_get(params):
        raise urllib.error.HTTPError(
            url="https://en.wikipedia.org/w/api.php",
            code=429,
            msg="Too Many Requests",
            hdrs={},
            fp=None,
        )

    monkeypatch.setattr(grounding, "_wiki_get", mock_wiki_get)

    script = {
        "topic_subject": "Tunguska event",
        "factual_claims": [{"text": "Explosion in Siberia", "segment_index": 0}],
        "segments": [{"narration": "Explosion in Siberia."}],
    }
    report = ground_script(script)
    assert report["status"] == "error"
    assert report["status"] != "no_source_found"
    assert "429" in report["error"] or "HTTPError" in report["error"]


def test_transient_429_retries_and_succeeds(monkeypatch):
    """A transient HTTP 429 followed by success on retry ends up with the correct article."""
    calls = 0

    def mock_wiki_get(params):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise urllib.error.HTTPError(
                url="https://en.wikipedia.org/w/api.php",
                code=429,
                msg="Too Many Requests",
                hdrs={},
                fp=None,
            )
        action = params.get("action")
        if action == "query" and params.get("list") == "search":
            return {"query": {"search": [{"title": "Tunguska event"}]}}
        elif action == "query" and params.get("prop") == "extracts":
            return {"query": {"pages": {"100": {"title": "Tunguska event", "extract": "The Tunguska event was a massive explosion in Russia."}}}}
        return {}

    monkeypatch.setattr(grounding, "_wiki_get", mock_wiki_get)

    sources = fetch_source("Tunguska event")
    assert sources is not None
    assert len(sources) == 1
    assert sources[0][0] == "Tunguska event"
    assert calls > 1


def test_genuine_empty_search_result_produces_no_source_found(monkeypatch):
    """A genuine empty search result (200 OK, zero hits) still produces no_source_found."""
    def mock_wiki_get(params):
        if params.get("list") == "search":
            return {"query": {"search": []}}
        return {}

    monkeypatch.setattr(grounding, "_wiki_get", mock_wiki_get)

    script = {
        "topic_subject": "Nonexistent Fake Subject 12345",
        "factual_claims": [{"text": "Fake claim", "segment_index": 0}],
        "segments": [{"narration": "Fake narration."}],
    }
    report = ground_script(script)
    assert report["status"] == "no_source_found"


def test_extract_fetch_costs_two_calls_regardless_of_article_count(monkeypatch):
    """N articles cost 2 requests, not N: one whole-article call for the primary
    source plus one batched intro-only call for the rest.

    Not one single call for everything — MediaWiki answers a multi-title
    whole-article extracts request with '"exlimit" was too large for a whole
    article extracts request, lowered to 1' and empty extracts for all but one
    page. See tests/test_grounding_extract_batching.py."""
    extract_calls = []

    def mock_wiki_get(params):
        action = params.get("action")
        if action == "query" and params.get("list") == "search":
            return {
                "query": {
                    "search": [
                        {"title": "Hessdalen lights"},
                        {"title": "Hessdalen"},
                        {"title": "Hessdalen AMS"},
                    ]
                }
            }
        elif action == "query" and params.get("prop") == "extracts":
            extract_calls.append(params)
            return {
                "query": {
                    "pages": {
                        "1": {"title": "Hessdalen lights", "extract": "Unexplained lights in Hessdalen valley."},
                        "2": {"title": "Hessdalen", "extract": "Hessdalen is a village in Norway."},
                        "3": {"title": "Hessdalen AMS", "extract": "Automatic measurement station."},
                    }
                }
            }
        return {}

    monkeypatch.setattr(grounding, "_wiki_get", mock_wiki_get)

    sources = fetch_source("Hessdalen lights", max_articles=3)
    assert sources is not None
    assert len(sources) == 3
    assert len(extract_calls) == 2
    assert extract_calls[0].get("titles") == "Hessdalen lights"
    assert not extract_calls[0].get("exintro"), "the primary source must be whole-article"
    assert extract_calls[1].get("titles") == "Hessdalen|Hessdalen AMS"
    assert extract_calls[1].get("exintro") == "1"
    assert extract_calls[1].get("exlimit") == "max"


def test_tunguska_event_selects_correct_article(monkeypatch):
    """Subject 'Tunguska event' with hits [Tunguska, Tunguska event] selects 'Tunguska event'."""
    def mock_wiki_get(params):
        action = params.get("action")
        if action == "query" and params.get("list") == "search":
            return {
                "query": {
                    "search": [
                        {"title": "Tunguska"},
                        {"title": "Tunguska event"},
                    ]
                }
            }
        elif action == "query" and params.get("prop") == "extracts":
            return {
                "query": {
                    "pages": {
                        "1": {"title": "Tunguska", "extract": "Tunguska region."},
                        "2": {"title": "Tunguska event", "extract": "1908 explosion in Siberia."},
                    }
                }
            }
        return {}

    monkeypatch.setattr(grounding, "_wiki_get", mock_wiki_get)

    sources = fetch_source("Tunguska event")
    assert sources is not None
    assert sources[0][0] == "Tunguska event"


def test_dyatlov_pass_incident_selects_correct_article(monkeypatch):
    """Subject 'Dyatlov Pass incident' with hits [Chivruay Pass incident, Dyatlov Pass incident]
    selects 'Dyatlov Pass incident'.
    This test catches the Round 6 bug where 'Chivruay Pass incident' was wrongly picked due to unranked search hits.
    """
    def mock_wiki_get(params):
        action = params.get("action")
        if action == "query" and params.get("list") == "search":
            return {
                "query": {
                    "search": [
                        {"title": "Chivruay Pass incident"},
                        {"title": "Dyatlov Pass incident"},
                    ]
                }
            }
        elif action == "query" and params.get("prop") == "extracts":
            return {
                "query": {
                    "pages": {
                        "1": {"title": "Chivruay Pass incident", "extract": "1973 pass incident."},
                        "2": {"title": "Dyatlov Pass incident", "extract": "1959 pass incident."},
                    }
                }
            }
        return {}

    monkeypatch.setattr(grounding, "_wiki_get", mock_wiki_get)

    sources = fetch_source("Dyatlov Pass incident")
    assert sources is not None
    assert sources[0][0] == "Dyatlov Pass incident"


def test_lead_masks_case_selects_correct_article(monkeypatch):
    """Subject 'Lead masks case' with hits [Face masks during the COVID-19 pandemic, Lead Masks Case]
    selects 'Lead Masks Case'.
    """
    def mock_wiki_get(params):
        action = params.get("action")
        if action == "query" and params.get("list") == "search":
            return {
                "query": {
                    "search": [
                        {"title": "Face masks during the COVID-19 pandemic"},
                        {"title": "Lead Masks Case"},
                    ]
                }
            }
        elif action == "query" and params.get("prop") == "extracts":
            return {
                "query": {
                    "pages": {
                        "1": {"title": "Face masks during the COVID-19 pandemic", "extract": "COVID masks."},
                        "2": {"title": "Lead Masks Case", "extract": "1966 mystery in Brazil."},
                    }
                }
            }
        return {}

    monkeypatch.setattr(grounding, "_wiki_get", mock_wiki_get)

    sources = fetch_source("Lead masks case")
    assert sources is not None
    assert sources[0][0] == "Lead Masks Case"


def test_roanoke_colony_disappearance_selects_correct_article(monkeypatch):
    """Subject 'Roanoke Colony disappearance' with hits [American Horror Story: Roanoke, Roanoke Colony]
    selects 'Roanoke Colony'.
    """
    def mock_wiki_get(params):
        action = params.get("action")
        if action == "query" and params.get("list") == "search":
            return {
                "query": {
                    "search": [
                        {"title": "American Horror Story: Roanoke"},
                        {"title": "Roanoke Colony"},
                    ]
                }
            }
        elif action == "query" and params.get("prop") == "extracts":
            return {
                "query": {
                    "pages": {
                        "1": {"title": "American Horror Story: Roanoke", "extract": "TV series."},
                        "2": {"title": "Roanoke Colony", "extract": "Lost colony settlement."},
                    }
                }
            }
        return {}

    monkeypatch.setattr(grounding, "_wiki_get", mock_wiki_get)

    sources = fetch_source("Roanoke Colony disappearance")
    assert sources is not None
    assert sources[0][0] == "Roanoke Colony"


def test_voynich_manuscript_mystery_fetches_voynich_manuscript(monkeypatch):
    """Subject 'Voynich manuscript mystery' fetches 'Voynich manuscript' without regression."""
    def mock_wiki_get(params):
        action = params.get("action")
        if action == "query" and params.get("list") == "search":
            return {
                "query": {
                    "search": [
                        {"title": "Voynich manuscript"},
                    ]
                }
            }
        elif action == "query" and params.get("prop") == "extracts":
            return {
                "query": {
                    "pages": {
                        "1": {"title": "Voynich manuscript", "extract": "Illustrated codex."},
                    }
                }
            }
        return {}

    monkeypatch.setattr(grounding, "_wiki_get", mock_wiki_get)

    sources = fetch_source("Voynich manuscript mystery")
    assert sources is not None
    assert sources[0][0] == "Voynich manuscript"


