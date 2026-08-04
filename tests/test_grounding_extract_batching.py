"""Guards the article-extract fetch against MediaWiki's silent exlimit downgrade.

Asking for several pipe-separated titles with prop=extracts&explaintext and no
exintro returns 200 OK with the warning '"exlimit" was too large for a whole
article extracts request, lowered to 1' and empty extracts for every page but
one — measured on the real API, where a batched fetch of
"Dyatlov Pass incident|Chivruay Pass incident|Devil's Pass" came back with only
Chivruay populated (2033 chars) and the top-ranked article empty. The script was
then grounded against the wrong incident with nothing reporting a problem.
"""
import agent.grounding as grounding


SEARCH_HITS = [
    {"title": "Dyatlov Pass incident"},
    {"title": "Chivruay Pass incident"},
    {"title": "Devil's Pass"},
]


def _fake_wiki(calls, whole_article_batch_is_lossy=True):
    """Stands in for _wiki_get, reproducing MediaWiki's real behaviour: a
    whole-article extracts request for several titles silently serves only one."""
    def _call(params):
        calls.append(params)
        if params.get("list") == "search":
            return {"query": {"search": SEARCH_HITS}}

        titles = params["titles"].split("|")
        intro_only = params.get("exintro")
        pages = {}
        for i, title in enumerate(titles):
            if len(titles) > 1 and not intro_only and whole_article_batch_is_lossy and i != len(titles) - 1:
                extract = ""       # the silently-dropped pages
            elif intro_only:
                extract = f"intro of {title}"
            else:
                extract = f"full text of {title}"
            # Keyed by a per-title id, as the real API keys pages by pageid — two
            # responses merged on colliding keys would lose an article.
            pages[str(abs(hash(title)))] = {"title": title, "extract": extract}
        return {"query": {"pages": pages}}
    return _call


def test_primary_article_is_fetched_whole_not_lost_to_the_batch(monkeypatch):
    """The top-ranked article must come back with its full text, not an empty
    extract, even though MediaWiki refuses to batch whole-article extracts."""
    calls = []
    monkeypatch.setattr(grounding, "_wiki_get", _fake_wiki(calls))

    sources = grounding.fetch_source("Dyatlov Pass incident")

    assert sources, "fetch_source returned nothing for a subject with a real article"
    assert sources[0][0] == "Dyatlov Pass incident"
    assert sources[0][1] == "full text of Dyatlov Pass incident"


def test_no_whole_article_request_is_ever_batched(monkeypatch):
    """A multi-title extracts request without exintro is exactly the call
    MediaWiki downgrades, so the code must never issue one."""
    calls = []
    monkeypatch.setattr(grounding, "_wiki_get", _fake_wiki(calls))

    grounding.fetch_source("Dyatlov Pass incident")

    extract_calls = [c for c in calls if c.get("prop") == "extracts"]
    assert extract_calls, "no extract call was made at all"
    for call in extract_calls:
        if len(call["titles"].split("|")) > 1:
            assert call.get("exintro"), (
                f"batched whole-article extract request issued for {call['titles']!r} — "
                'MediaWiki lowers exlimit to 1 and serves empty extracts'
            )


def test_supporting_articles_still_arrive_in_one_batched_call(monkeypatch):
    """The rate-limit fix must survive: supporting articles are intro-only and
    share a single request, so an article count of N costs 2 requests, not N."""
    calls = []
    monkeypatch.setattr(grounding, "_wiki_get", _fake_wiki(calls))

    sources = grounding.fetch_source("Dyatlov Pass incident")

    titles = [t for t, _ in sources]
    assert titles == ["Dyatlov Pass incident", "Chivruay Pass incident", "Devil's Pass"]
    extract_calls = [c for c in calls if c.get("prop") == "extracts"]
    assert len(extract_calls) == 2, f"expected 2 extract requests, got {len(extract_calls)}"
