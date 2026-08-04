import agent.resilience as resilience
import agent.visuals as visuals
from agent.visuals import MIN_RELEVANCE_SCORE, NoRelevantResults, _search_videos, fetch_all
import pytest


class DummyResponse:
    def __init__(self, json_data):
        self._json_data = json_data

    def raise_for_status(self):
        pass

    def json(self):
        return self._json_data


def test_anchored_visual_query_tried_first(monkeypatch):
    """The anchored visual_query is tried first."""
    queries_tried = []

    def fake_fetch_segment_clips(query, out_paths, min_relevance=0):
        queries_tried.append((query, min_relevance))
        return out_paths

    monkeypatch.setattr(visuals, "fetch_segment_clips", fake_fake := fake_fetch_segment_clips)
    segments = [{
        "duration": 2.0,
        "visual_query": "east african salt flat lake",
        "visual_fallback": "fog",
    }]
    result = fetch_all(segments)
    assert len(queries_tried) == 1
    assert queries_tried[0][0] == "east african salt flat lake"
    assert queries_tried[0][1] == MIN_RELEVANCE_SCORE
    assert result[0]["clip_paths"] is not None


def test_failing_anchored_query_falls_through_and_degrades(monkeypatch):
    """A failing anchored query falls through to visual_fallback AND records a degradation."""
    resilience.reset()

    def fake_fetch_segment_clips(query, out_paths, min_relevance=0):
        if query == "niche mystery lead masks":
            raise NoRelevantResults("No relevant results")
        return out_paths

    monkeypatch.setattr(visuals, "fetch_segment_clips", fake_fetch_segment_clips)
    segments = [{
        "duration": 2.0,
        "visual_query": "niche mystery lead masks",
        "visual_fallback": "dark atmospheric",
    }]
    result = fetch_all(segments)
    assert result[0]["clip_paths"] is not None

    degs = resilience.degradations()
    assert len(degs) >= 1
    assert degs[0]["stage"] == "pexels"
    assert "used segment fallback query 'dark atmospheric'" in degs[0]["fallback"]


def test_legacy_visual_keywords_compatibility(monkeypatch):
    """An old-format segment carrying only visual_keywords still works."""
    queries_tried = []

    def fake_fetch_segment_clips(query, out_paths, min_relevance=0):
        queries_tried.append(query)
        return out_paths

    monkeypatch.setattr(visuals, "fetch_segment_clips", fake_fetch_segment_clips)
    segments = [{"duration": 2.0, "visual_keywords": "fog over hills"}]
    result = fetch_all(segments)
    assert queries_tried[0] == "fog over hills"
    assert result[0]["clip_paths"] is not None


def test_search_videos_rejects_zero_relevance_for_anchored_query(monkeypatch):
    """_search_videos rejects an all-zero-relevance result set for an anchored query and raises NoRelevantResults."""
    fake_json = {
        "videos": [
            {"id": 101, "url": "https://pexels.com/video/unrelated-clip-101/", "video_files": []}
        ]
    }
    monkeypatch.setattr("requests.get", lambda url, headers, params, timeout: DummyResponse(fake_json))

    with pytest.raises(NoRelevantResults):
        _search_videos("east african salt flat lake", per_page=15, min_relevance=MIN_RELEVANCE_SCORE)


def test_search_videos_fallback_query_exempt_from_relevance_floor(monkeypatch):
    """A FALLBACK_QUERIES query is exempt from the relevance floor and still returns results."""
    fake_json = {
        "videos": [
            {"id": 202, "url": "https://pexels.com/video/random-clip-202/", "video_files": []}
        ]
    }
    monkeypatch.setattr("requests.get", lambda url, headers, params, timeout: DummyResponse(fake_json))

    results = _search_videos("fog", per_page=15, min_relevance=MIN_RELEVANCE_SCORE)
    assert len(results) == 1
    assert results[0]["id"] == 202
