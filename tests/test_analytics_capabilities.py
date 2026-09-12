"""Guards the analytics capability probe added 2026-09-09.

PLAN.md records impressions and CTR as unavailable — `Unknown identifier
(impressions)` — tested live and honestly. But that test ran when the channel
was about two days old and the Analytics API was returning no rows for ANY
metric, which is a degenerate condition to draw a permanent conclusion from.
Impressions are also the one number that would settle whether this channel's
view ceiling is a distribution problem or a content problem.

So the conclusion gets re-asked rather than inherited, and these tests pin the
one rule that makes the answer trustworthy: the probe reports what the API
actually said, and nothing is ever collected on an assumption.
"""
import json

import pytest

from agent import youtube_stats


class _Analytics:
    def __init__(self, behaviour):
        self._behaviour = behaviour

    def reports(self):
        return self

    def query(self, **kw):
        metrics, dimensions = kw.get("metrics"), kw.get("dimensions")
        outcome = self._behaviour(metrics, dimensions)
        class _Req:
            def execute(_self):
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome
        return _Req()


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(youtube_stats, "CAPABILITIES_PATH",
                        str(tmp_path / "analytics_capabilities.json"))
    monkeypatch.setattr(youtube_stats, "_credentials", lambda scopes: object())


def _install(monkeypatch, behaviour):
    monkeypatch.setattr(youtube_stats, "build",
                        lambda *a, **k: _Analytics(behaviour))


def test_an_unknown_metric_is_recorded_as_unavailable_with_the_api_text(monkeypatch):
    def behaviour(metrics, dimensions):
        if metrics == "impressions":
            return Exception("badRequest: Unknown identifier (impressions)")
        return {"rows": [[1]]}
    _install(monkeypatch, behaviour)

    found = youtube_stats.probe_analytics_capabilities()
    impressions = found["metrics"]["impressions"]
    assert impressions["available"] is False
    # The API's own words, not our paraphrase — that is what makes a later
    # reader able to tell "cannot have it" from "our token lost a scope".
    assert "Unknown identifier (impressions)" in impressions["reason"]


def test_a_metric_that_returns_rows_is_available(monkeypatch):
    _install(monkeypatch, lambda m, d: {"rows": [["ADVERTISING", 12]]})
    found = youtube_stats.probe_analytics_capabilities()
    assert found["metrics"]["trafficSources"]["available"] is True
    assert found["metrics"]["trafficSources"]["rows"] == 1


def test_an_accepted_query_with_no_rows_is_not_called_available(monkeypatch):
    """Different from a rejected query, and recoverable: it can start returning
    rows once the channel has enough data. Must not be filed as 'impossible'."""
    _install(monkeypatch, lambda m, d: {"rows": []})
    found = youtube_stats.probe_analytics_capabilities()
    entry = found["metrics"]["estimatedMinutesWatched"]
    assert entry["available"] is False
    assert "no rows" in entry["reason"]


def test_a_dead_analytics_client_fails_every_probe_rather_than_guessing(monkeypatch):
    def explode(*a, **k):
        raise RuntimeError("invalid_scope")
    monkeypatch.setattr(youtube_stats, "build", explode)
    found = youtube_stats.probe_analytics_capabilities()
    assert set(found["metrics"]) == set(youtube_stats.CAPABILITY_PROBES)
    assert all(not e["available"] for e in found["metrics"].values())
    assert all("invalid_scope" in e["reason"] for e in found["metrics"].values())


def test_the_probe_result_is_persisted(monkeypatch):
    _install(monkeypatch, lambda m, d: {"rows": [[1]]})
    youtube_stats.probe_analytics_capabilities()
    with open(youtube_stats.CAPABILITIES_PATH) as f:
        saved = json.load(f)
    assert saved["metrics"]["impressions"]["available"] is True
    assert saved["probed_at"]


def test_never_probed_means_never_collect():
    """The safety rule: unknown is not permission. A metric we have never
    successfully read is one we do not collect and do not invent."""
    assert youtube_stats.analytics_capabilities()["metrics"] == {}
    for name in youtube_stats.CAPABILITY_PROBES:
        assert youtube_stats.capability_available(name) is False


def test_capability_available_reads_the_persisted_answer(monkeypatch):
    _install(monkeypatch, lambda m, d: (
        {"rows": [[1]]} if m == "impressions" else Exception("nope")))
    youtube_stats.probe_analytics_capabilities()
    assert youtube_stats.capability_available("impressions") is True
    assert youtube_stats.capability_available("demographics") is False


def test_a_corrupt_capabilities_file_denies_rather_than_crashes(monkeypatch, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{oops")
    monkeypatch.setattr(youtube_stats, "CAPABILITIES_PATH", str(bad))
    assert youtube_stats.analytics_capabilities()["metrics"] == {}
    assert youtube_stats.capability_available("impressions") is False
