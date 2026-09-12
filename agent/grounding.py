"""
Fact-grounds each script against Wikipedia before it reaches TTS.

TWO CHECKS, NEITHER OF THEM A MODEL CALL

1. RESEARCH, done in advance. The weekly Claude task researches each story
   against real sources and records a per-claim verdict in the packet
   (agent/packet.py). That work reaches this module as script["verification"],
   and a CONTRADICTED or MISLEADING verdict there still rewrites the offending
   line before it is ever spoken — apply_corrections() is unchanged.

2. CORROBORATION, done at run time, here, with no key and no model. Each claim
   is matched against the Wikipedia article this module selects for the
   subject: a claim whose significant words and every one of its numbers appear
   in the article reads SUPPORTED; anything else reads SILENT. Lexical
   corroboration cannot prove a contradiction, so it never claims one — that
   verdict only ever comes from the researched packet.

WHAT WAS REMOVED AND WHY

verify_claims() used to send the article and the claims to Gemini. That call is
gone with every other Gemini call in this repository: it shared a key and a
model with the script generator, so the same `503 UNAVAILABLE` outage that
killed twenty-eight scheduled runs in late August could take out fact-checking
too, and a fact-check that fails open is worse than one that is simply honest
about its own reach.

Gemini's native Google Search grounding was never available here anyway —
verified against this project's own key: an identical request succeeds without
the google_search tool and returns 429 RESOURCE_EXHAUSTED with it, so the
grounding quota is zero rather than the key being rate limited. Wikipedia's
keyless API has done the source lookup since, and still does; source lookup
stays behind fetch_source() so swapping in a different provider later remains a
contained change.
"""
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from . import resilience, script_writer

WIKI_API = "https://en.wikipedia.org/w/api.php"
USER_AGENT = "faceless-youtube-agent/1.0 (fact-checking generated scripts)"
MAX_ARTICLE_CHARS = 12000

# The retry ladder for Wikipedia, as module constants so a test can shrink it
# rather than actually sleep through a minute of backoff.
#
# Five attempts from an 8-second base, equal-jittered: roughly 4, 8, 16 and 32
# seconds of waiting at worst, about a minute in total.
#
# The old ladder (three attempts from 0.5s) spent under two seconds in total,
# which is not a retry against a rate limiter at all — it is three requests in
# a row, and rapid successive requests are what earns the 429. Seen live on
# 2026-09-07: a second run minutes after the first was refused on its very
# first request, burned the whole ladder in under two seconds, and the
# fact-check degraded to "error" for that video.
#
# A minute of waiting is affordable here and nowhere else would be: the job's
# ceiling is 45 minutes against an ~11-minute run, and grounding is nowhere
# near the long pole.
RETRY_ATTEMPTS = 5
RETRY_BASE_DELAY_SECONDS = 8.0

def _safe_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


STOPWORDS = {
    "the", "a", "an", "of", "in", "on", "at", "for", "to", "and", "or", "is", "are",
    "was", "were", "with", "by", "from", "phenomenon", "effect", "case", "mystery",
    "incident", "disaster", "story", "event", "occurrence", "degradation"
}

DESCRIPTIVE_WORDS = STOPWORDS | {
    "calcification", "petrification", "mummification", "anomaly", "experiment",
    "conspiracy", "hoax", "legend", "myth", "process", "phenomena"
}


def _extract_meaningful_tokens(text: str) -> set[str]:
    words = re.findall(r"\b\w+\b", text.lower())
    return {w for w in words if w not in STOPWORDS}


def _score_title_relevance(title: str, subject: str) -> int:
    t_tokens = _extract_meaningful_tokens(title)
    s_tokens = _extract_meaningful_tokens(subject)
    return len(t_tokens & s_tokens)


def _narrow_subject(subject: str) -> str:
    """Narrow a subject phrase by dropping trailing descriptive/stop words and keeping
    the leading proper-noun phrase.
    E.g. 'Lake Natron calcification phenomenon' -> 'Lake Natron'
    """
    if not subject:
        return ""
    words = subject.strip().split()
    if not words:
        return ""

    keep_len = len(words)
    while keep_len > 0:
        w_clean = re.sub(r'^[^\w]+|[^\w]+$', '', words[keep_len - 1])
        w_lower = w_clean.lower()
        if w_lower in DESCRIPTIVE_WORDS or (keep_len > 1 and w_clean.islower() and w_lower not in {"of", "the"}):
            keep_len -= 1
        else:
            break

    if 0 < keep_len < len(words):
        return " ".join(words[:keep_len])

    leading = []
    for w in words:
        w_clean = re.sub(r'^[^\w]+|[^\w]+$', '', w)
        if w_clean and (w_clean[0].isupper() or w_clean.lower() in {"of", "the", "and"}):
            leading.append(w)
        else:
            break
    if leading and len(leading) < len(words):
        return " ".join(leading)

    return subject


def _extract_entities(text: str) -> list[str]:
    """Extract key proper noun entities from claim text to search on Wikipedia.
    E.g. 'Photographer Nick Brandt captured rock-like corpses at Lake Natron'
    -> ['Nick Brandt', 'Lake Natron']
    """
    if not text:
        return []
    multi_word = re.findall(r'\b[A-Z][a-zA-Z0-9\'-]+(?:\s+[A-Z][a-zA-Z0-9\'-]+)+\b', text)
    if multi_word:
        return multi_word
    words = text.split()
    singles = []
    for i, w in enumerate(words):
        clean = re.sub(r'^[^\w]+|[^\w]+$', '', w)
        if clean and clean[0].isupper() and clean.lower() not in STOPWORDS:
            if i > 0:
                singles.append(clean)
    return singles


def _wiki_get(params: dict) -> dict:
    url = f"{WIKI_API}?{urllib.parse.urlencode({**params, 'format': 'json'})}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


class NonRetryableWikiError(Exception):
    """Raised for non-transient HTTP errors (e.g. 404, 400) that should not be retried."""


def _is_transient_wiki_error(e: Exception) -> bool:
    """Returns True for transient network / rate-limiting errors worth retrying."""
    if isinstance(e, urllib.error.HTTPError):
        # Wikipedia rate limiting (HTTP 429: Too Many Requests) and 5xx server errors
        # bit us on rapid requests across multiple claims/subjects.
        return e.code == 429 or (500 <= e.code < 600)
    if isinstance(e, (urllib.error.URLError, TimeoutError, ConnectionError, OSError)):
        return True
    return False


def _wiki_get_with_retry(params: dict) -> dict:
    def _call():
        try:
            return _wiki_get(params)
        except Exception as e:
            if isinstance(e, urllib.error.HTTPError) and not _is_transient_wiki_error(e):
                raise NonRetryableWikiError(str(e)) from e
            raise

    return resilience.retry(
        _call,
        label="wiki_get",
        attempts=RETRY_ATTEMPTS,
        base_delay=RETRY_BASE_DELAY_SECONDS,
        dont_retry_on=(NonRetryableWikiError,),
    )


def _rank_hits(hits: list[dict], subject: str, narrowed: str | None = None) -> list[dict]:
    """Filter and rank candidate search hits.

    Candidate hits must pass a floor requirement of token overlap >= 1 (non-stopwords).
    Hits are sorted descending by:
    1. Exact case-insensitive title match against subject (score 2) or narrowed subject (score 1).
    2. Token overlap score descending (meaningful non-stopwords overlap).
    3. Raw word overlap score descending: count of original subject/search words (including
       stopwords) contained in the candidate title. This tie-breaker distinguishes:
       - "Tunguska event" (raw_word_score 2) over "Tunguska" (raw_word_score 1) for subject 'Tunguska event',
       - "Dyatlov Pass incident" (raw_word_score 3) over "Chivruay Pass incident" (raw_word_score 2) for subject 'Dyatlov Pass incident',
       - "Lead Masks Case" (raw_word_score 3) over "Face masks during the COVID-19 pandemic" (raw_word_score 1) for subject 'Lead masks case',
       - "Roanoke Colony" (raw_word_score 2) over "American Horror Story: Roanoke" (raw_word_score 1) for subject 'Roanoke Colony disappearance'.
    4. Wikipedia's own search result order as the final tie-break.
    """
    if not hits:
        return []

    s_clean = subject.strip().lower()
    n_clean = narrowed.strip().lower() if narrowed else ""

    s_tokens = _extract_meaningful_tokens(subject)
    n_tokens = _extract_meaningful_tokens(n_clean) if n_clean else set()

    s_all_words = set(re.findall(r"\b\w+\b", subject.lower()))
    if n_clean:
        s_all_words.update(re.findall(r"\b\w+\b", n_clean.lower()))

    candidates = []
    for idx, hit in enumerate(hits):
        title = hit.get("title", "")
        if not title:
            continue
        t_clean = title.strip().lower()
        t_tokens = _extract_meaningful_tokens(title)

        # Floor requirement: token-overlap score >= 1
        s_overlap = len(t_tokens & s_tokens)
        n_overlap = len(t_tokens & n_tokens) if n_tokens else 0
        meaningful_score = max(s_overlap, n_overlap)
        if meaningful_score < 1:
            continue

        # 1. Exact match score
        if t_clean == s_clean:
            exact_score = 2
        elif n_clean and t_clean == n_clean:
            exact_score = 1
        else:
            exact_score = 0

        # 3. Raw word overlap score (including stopwords)
        t_all_words = set(re.findall(r"\b\w+\b", title.lower()))
        raw_word_score = len(s_all_words & t_all_words)

        # Key tuple for descending sort (reverse=True):
        # (exact_score, meaningful_score, raw_word_score, -idx)
        key = (exact_score, meaningful_score, raw_word_score, -idx)
        candidates.append((key, hit))

    candidates.sort(key=lambda c: c[0], reverse=True)
    return [hit for key, hit in candidates]


def fetch_source(subject: str, max_articles: int = 3) -> list[tuple[str, str]] | None:
    """Finds matching Wikipedia articles that pass the title-relevance gate.
    Returns list of (title, text) pairs.
    Caps each article at MAX_ARTICLE_CHARS and total text at 3 * MAX_ARTICLE_CHARS.
    """
    if not subject:
        return None

    narrowed = _narrow_subject(subject)
    if narrowed == subject:
        narrowed = None

    def _get_relevant_hits(search_term: str) -> list[dict]:
        res = _wiki_get_with_retry({
            "action": "query", "list": "search", "srsearch": search_term, "srlimit": 5
        })
        hits = res.get("query", {}).get("search", [])
        return _rank_hits(hits, subject=subject, narrowed=narrowed)

    hits = _get_relevant_hits(subject)
    if not hits and narrowed:
        hits = _get_relevant_hits(narrowed)

    if not hits:
        return None

    hits = hits[:max_articles]

    sources = []
    total_chars = 0
    max_total = 3 * MAX_ARTICLE_CHARS

    # MediaWiki REFUSES to batch whole-article extracts. Asking for several
    # pipe-separated titles with prop=extracts&explaintext (no exintro) and
    # exlimit=max comes back 200 OK with this warning:
    #   "exlimit" was too large for a whole article extracts request, lowered to 1.
    # and every page but one has an empty "extract". Measured on the real API:
    # titles="Dyatlov Pass incident|Chivruay Pass incident|Devil's Pass" returned
    # Chivruay with 2033 chars and the other two with 0 — so the batched version
    # silently dropped the top-ranked article and grounded the script against the
    # wrong incident. exlimit>1 is only honoured alongside exintro (intro only).
    #
    # So: one full-text call for the primary (best-ranked) article, and ONE
    # batched intro-only call for the remaining supporting articles. Two requests
    # regardless of article count, which still answers the rate-limit (HTTP 429)
    # problem that motivated batching, without losing the article that matters.
    titles = [h["title"] for h in hits if h.get("title")]
    if titles:
        primary_res = _wiki_get_with_retry({
            "action": "query",
            "prop": "extracts",
            "explaintext": "1",
            "titles": titles[0],
        })
        query_res = primary_res.get("query", {})
        pages = dict(query_res.get("pages", {}))

        if len(titles) > 1:
            supporting_res = _wiki_get_with_retry({
                "action": "query",
                "prop": "extracts",
                "explaintext": "1",
                "exintro": "1",
                "exlimit": "max",
                "titles": "|".join(titles[1:]),
            })
            supporting_query = supporting_res.get("query", {})
            pages.update(supporting_query.get("pages", {}))
            for key in ("normalized", "redirects"):
                merged = list(query_res.get(key, [])) + list(supporting_query.get(key, []))
                if merged:
                    query_res = {**query_res, key: merged}

        norm_map = {}
        for n in query_res.get("normalized", []):
            if isinstance(n, dict) and n.get("from") and n.get("to"):
                norm_map[n["from"]] = n["to"]
                norm_map[n["from"].lower()] = n["to"]
        for r in query_res.get("redirects", []):
            if isinstance(r, dict) and r.get("from") and r.get("to"):
                norm_map[r["from"]] = r["to"]
                norm_map[r["from"].lower()] = r["to"]

        pages_by_title = {}
        if isinstance(pages, dict):
            page_list = [p for p in pages.values() if isinstance(p, dict)]
            for page in page_list:
                t = page.get("title")
                ext = page.get("extract", "")
                if t:
                    pages_by_title[t] = ext
                    pages_by_title[t.lower()] = ext
            # Fallback for single-title queries where the response page object lacks a 'title' key
            if not pages_by_title and len(titles) == 1 and len(page_list) == 1:
                single_ext = page_list[0].get("extract", "")
                pages_by_title[titles[0]] = single_ext
                pages_by_title[titles[0].lower()] = single_ext

        for hit in hits:
            title = hit["title"]
            target_title = norm_map.get(title) or norm_map.get(title.lower()) or title
            extract = (
                pages_by_title.get(title)
                or pages_by_title.get(title.lower())
                or pages_by_title.get(target_title)
                or pages_by_title.get(target_title.lower(), "")
            )
            if extract:
                extract_cap = extract[:MAX_ARTICLE_CHARS]
                if total_chars + len(extract_cap) > max_total:
                    extract_cap = extract_cap[:max_total - total_chars]
                if extract_cap:
                    sources.append((title, extract_cap))
                    total_chars += len(extract_cap)
                if total_chars >= max_total:
                    break

    return sources if sources else None


# A claim is corroborated when this share of its significant words is present
# in the reference article. Not 100%: a claim is a sentence written for
# narration, and the article is an encyclopaedia — "the ship was found adrift
# with nobody aboard" and "the vessel was discovered unmanned" are the same
# fact in different words, and demanding every token would report almost
# everything as SILENT. Two thirds is high enough that an unrelated article
# (the Tibesti Mountains problem of 2026-08-04) cannot clear it.
CORROBORATION_THRESHOLD = 2 / 3
# Numbers are the exception: they are exact by nature and a wrong one is the
# most common way a generated line goes subtly wrong, so EVERY number in the
# claim must appear or the claim is not corroborated at all.
_NUMBER_RE = re.compile(r"\d[\d,]*")


def _claim_tokens(text: str) -> set[str]:
    """The words in a claim that carry its meaning."""
    words = re.findall(r"[A-Za-z][A-Za-z'-]*", text or "")
    return {w.lower() for w in words
            if len(w) > 2 and w.lower() not in DESCRIPTIVE_WORDS}


def _numbers(text: str) -> set[str]:
    return {n.replace(",", "") for n in _NUMBER_RE.findall(text or "")}


def corroborate(claims: list[dict], sources, segments: list[dict] = None) -> list[dict]:
    """Checks each claim against the reference articles, lexically.

    Returns the same verdict shape verify_claims() always returned, so
    ground_script(), the video record and the dashboard all consume it
    unchanged — but every verdict is either SUPPORTED or SILENT, because
    word overlap can show that an article covers a claim and can never show
    that it refutes one. Refutation comes from the researched packet.

    ``segments`` is accepted for call compatibility and for the segment index
    in the result.  It is deliberately *not* folded into the matching tokens:
    unioning a precise claim with a broad narration line lets easy filler
    words inflate overlap until an unsupported claim looks corroborated.
    """
    if not claims:
        return []

    if isinstance(sources, str):
        sources_list = [(sources, "")]
    elif isinstance(sources, tuple) and len(sources) == 2:
        sources_list = [sources]
    elif isinstance(sources, list):
        sources_list = sources
    else:
        sources_list = []
    if not sources_list:
        return []

    corpus = " ".join(text for _, text in sources_list).lower()
    corpus_numbers = _numbers(corpus)

    verdicts = []
    for claim in claims:
        idx = _safe_int(claim.get("segment_index"))
        text = claim.get("text", "") or ""
        tokens = _claim_tokens(text)
        numbers = _numbers(text)
        if not tokens:
            share = 0.0
        else:
            share = len([t for t in tokens if t in corpus]) / len(tokens)
        numbers_ok = numbers.issubset(corpus_numbers)

        supported = share >= CORROBORATION_THRESHOLD and numbers_ok
        if supported:
            note = (f"{share:.0%} of the claim's wording appears in the "
                    f"reference article" +
                    (f", including {len(numbers)} number(s)" if numbers else ""))
        elif not numbers_ok:
            missing = sorted(numbers - corpus_numbers)
            note = ("the article does not carry "
                    + ", ".join(missing[:3]) + " from this claim")
        else:
            note = (f"only {share:.0%} of the claim's wording appears in the "
                    "reference article")

        verdicts.append({
            "claim": text,
            "segment_index": claim.get("segment_index"),
            "verdict": "SUPPORTED" if supported else "SILENT",
            "note": note,
            "correction": None,
            "method": "wikipedia-lexical",
        })
    return verdicts


# Kept under its historical name so nothing that imported it breaks; the
# signature is the old one minus the `article` positional nobody passed.
def verify_claims(claims: list[dict], sources, article: str = None,
                  segments: list[dict] = None) -> list[dict]:
    if isinstance(sources, str):
        sources = (sources, article or "")
    return corroborate(claims, sources, segments=segments)


def _claim_key(index: int | None, claim: object) -> tuple[int | None, str]:
    """Stable identity for a claim within a segment.

    Several claims may belong to one narration segment. Segment index alone is
    therefore not a key; using it was how the first researched verdict got
    copied onto every later claim in that segment.
    """
    return index, " ".join(str(claim or "").lower().split())


def _research_index(researched: list[dict]) -> tuple[dict, dict]:
    exact, by_segment = {}, {}
    for entry in researched:
        if not isinstance(entry, dict):
            continue
        idx = _safe_int(entry.get("segment_index"))
        if idx is None:
            continue
        key = _claim_key(idx, entry.get("claim"))
        if key[1]:
            exact[key] = entry
        by_segment.setdefault(idx, []).append(entry)
    return exact, by_segment


def apply_research(verdicts: list[dict], researched: list[dict]) -> list[dict]:
    """Overlays the packet's researched verdicts onto the lexical ones.

    The two look at different things and the merge respects that:

      * CONTRADICTED / MISLEADING always wins. Only the research can reach that
        conclusion, it arrives with the corrected line attached, and it is the
        verdict that changes what gets spoken.
      * A researched SUPPORTED promotes a lexical SILENT, because "the article
        this module happened to pick does not mention it" is not evidence
        against a claim that was checked against a source chosen for it.
      * A lexical SUPPORTED is never demoted by a researched SILENT: the run
        found the words in a real article, and that stands on its own.
    """
    if not researched:
        return verdicts
    exact, by_segment = _research_index(researched)

    merged = []
    for v in verdicts:
        idx = _safe_int(v.get("segment_index"))
        research = exact.get(_claim_key(idx, v.get("claim")))
        # Old packets sometimes omitted the redundant claim text.  That is
        # only safe to use when there is exactly one verdict for the segment;
        # otherwise guessing would corrupt another claim's verdict.
        if research is None and idx is not None:
            candidates = by_segment.get(idx, [])
            if len(candidates) == 1:
                research = candidates[0]
        if not research:
            merged.append(v)
            continue
        verdict = research.get("verdict")
        if verdict in ("CONTRADICTED", "MISLEADING"):
            merged.append({
                "claim": v.get("claim"),
                "segment_index": v.get("segment_index"),
                "verdict": verdict,
                "note": research.get("note", ""),
                "correction": research.get("correction"),
                "method": "claude-research",
                "source": research.get("source", ""),
            })
        elif verdict == "SUPPORTED" and v.get("verdict") == "SILENT":
            merged.append({**v, "verdict": "SUPPORTED",
                           "note": research.get("note", "")
                                   or "checked against a researched source",
                           "method": "claude-research",
                           "source": research.get("source", "")})
        else:
            merged.append(v)
    return merged


def ground_script(script: dict) -> dict:
    """Checks the script's claims and returns a grounding report.

    Never raises: a grounding failure should downgrade the report to
    "unverified", not take down a pipeline whose actual job is publishing.
    """
    subject = script.get("topic_subject", "") or script.get("title", "")
    claims = script.get("factual_claims", []) or []
    segments = script.get("segments", []) or []
    final_idx = len(segments) - 1 if segments else None

    try:
        sources = fetch_source(subject)
        if not sources:
            return {"source": "wikipedia", "status": "no_source_found",
                    "verifier": "wikipedia-lexical", "subject": subject, "source_relevant": False, "coverage": 0.0,
                    "unverified_source": False, "claims_checked": 0, "contradicted": 0,
                    "misleading": 0, "accuracy_flags": [],
                    "final_segment_grounded": False, "verdicts": []}

        # Primary title for report header URL
        title = sources[0][0] if isinstance(sources, list) else sources[0]
        verdicts = corroborate(claims, sources, segments=segments)

        # Per-claim source lookup for uncovered (SILENT) claims. Bounded at
        # three extra article fetches per script: corroboration itself is free
        # now, but Wikipedia rate-limits (HTTP 429) on rapid successive calls,
        # and this runs four times a day against the same host.
        MAX_EXTRA_ARTICLE_FETCHES = 3
        extra_sources = []
        fetched_titles = {s[0].lower() for s in sources}
        extra_fetches = 0

        silent_verdicts = [v for v in verdicts if v.get("verdict") == "SILENT"]
        if silent_verdicts:
            silent_claims = []
            for v in silent_verdicts:
                s_idx = _safe_int(v.get("segment_index"))
                claim_text = v.get("claim", "")
                c_obj = next((c for c in claims if _safe_int(c.get("segment_index")) == s_idx and c.get("text") == claim_text), None)
                if not c_obj:
                    c_obj = next((c for c in claims if _safe_int(c.get("segment_index")) == s_idx), None)
                if c_obj and c_obj not in silent_claims:
                    silent_claims.append(c_obj)

            for c_obj in silent_claims:
                if extra_fetches >= MAX_EXTRA_ARTICLE_FETCHES:
                    break
                entities = _extract_entities(c_obj.get("text", ""))
                for entity in entities:
                    if extra_fetches >= MAX_EXTRA_ARTICLE_FETCHES:
                        break
                    if entity.lower() in fetched_titles:
                        continue
                    new_srcs = fetch_source(entity, max_articles=1)
                    if new_srcs:
                        for t, a in new_srcs:
                            if t.lower() not in fetched_titles:
                                extra_sources.append((t, a))
                                fetched_titles.add(t.lower())
                                extra_fetches += 1
                                if extra_fetches >= MAX_EXTRA_ARTICLE_FETCHES:
                                    break

            if extra_sources and silent_claims:
                second_verdicts = corroborate(silent_claims, extra_sources, segments=segments)
                second_map, second_by_segment = _research_index(second_verdicts)

                for i, v in enumerate(verdicts):
                    if v.get("verdict") == "SILENT":
                        idx = _safe_int(v.get("segment_index"))
                        sv = second_map.get(_claim_key(idx, v.get("claim")))
                        if sv is None and idx is not None:
                            candidates = second_by_segment.get(idx, [])
                            if len(candidates) == 1:
                                sv = candidates[0]
                        if sv and sv.get("verdict") != "SILENT":
                            verdicts[i] = sv

        # The researched verdicts from the packet are folded in last, so a
        # CONTRADICTED or MISLEADING finding survives whatever the lexical pass
        # said and still reaches apply_corrections() with its rewritten line.
        verdicts = apply_research(verdicts, script.get("verification") or [])

        contradicted = [v for v in verdicts if v.get("verdict") == "CONTRADICTED"]
        misleading = [v for v in verdicts if v.get("verdict") == "MISLEADING"]
        supported = [v for v in verdicts if v.get("verdict") == "SUPPORTED"]
        silent = [v for v in verdicts if v.get("verdict") == "SILENT"]

        claims_checked = len(verdicts)
        covered_count = len(supported) + len(contradicted) + len(misleading)
        coverage = (covered_count / claims_checked) if claims_checked > 0 else 0.0
        unverified_source = (claims_checked > 0 and coverage == 0.0)

        accuracy_flags = [
            {
                "claim": v.get("claim"),
                "segment_index": v.get("segment_index"),
                "verdict": v.get("verdict"),
                "note": v.get("note"),
            }
            for v in verdicts
            if v.get("verdict") in ("CONTRADICTED", "MISLEADING")
        ]

        final_verdicts = [
            v for v in verdicts
            if _safe_int(v.get("segment_index")) == final_idx
        ]
        researched = script.get("verification") or []
        return {
            "source": "wikipedia",
            "status": "checked",
            # Named so the record says how a claim was checked, not just that
            # it was. Old records carry no verifier field and are read as the
            # Gemini-era check they were.
            "verifier": ("wikipedia-lexical+claude-research" if researched
                         else "wikipedia-lexical"),
            "researched_claims": len(researched),
            "sources_cited": len(script.get("sources") or []),
            "subject": subject,
            "article": title,
            "article_url": f"https://en.wikipedia.org/wiki/{urllib.parse.quote(title.replace(' ', '_'))}",
            "source_relevant": True,
            "coverage": coverage,
            "unverified_source": unverified_source,
            "claims_checked": claims_checked,
            "supported": len(supported),
            "silent": len(silent),
            "contradicted": len(contradicted),
            "misleading": len(misleading),
            "accuracy_flags": accuracy_flags,
            "final_segment_grounded": len(final_verdicts) > 0,
            "final_segment_contradicted": any(
                v.get("verdict") in ("CONTRADICTED", "MISLEADING") for v in final_verdicts
            ),
            # Exposed so enforce_publication_policy() can identify a contradicted
            # PAYOFF line exactly, rather than inferring it from the ordering of
            # the verdict list. The last line is the answer the video promised,
            # and it is the one that gets a different rule.
            "final_segment_index": final_idx,
            "verdicts": verdicts,
        }
    except Exception as e:  # noqa: BLE001 - reported, never fatal
        return {"source": "wikipedia", "status": "error",
                "verifier": "wikipedia-lexical", "subject": subject,
                "error": f"{type(e).__name__}: {e}",
                "source_relevant": False, "coverage": 0.0, "unverified_source": False,
                "claims_checked": 0, "contradicted": 0, "misleading": 0,
                "accuracy_flags": [], "verdicts": []}


def apply_corrections(script: dict, report: dict) -> dict:
    """Rewrites the narration for each contradicted or misleading claim's segment.

    Uses the segment_index the claim was written with, rather than matching the
    claim's text against the narration: the claim is a paraphrase of the fact,
    not a quote from the script, so it
    routinely shares no substring with the line it was drawn from — that was
    the bug (corrections_applied stuck at 0 on a real run: 2026-07-30,
    video K5-7-HTo38M, "footprints... into the attic" claim vs. narration
    "footsteps leading straight into his attic"). A structural index survives
    paraphrasing; string matching can't. MISLEADING corrections now flow
    through the same path.
    """
    corrections = [
        v for v in report.get("verdicts", [])
        if v.get("verdict") in ("CONTRADICTED", "MISLEADING") and v.get("correction")
    ]
    if not corrections:
        return script

    segments = script.get("segments", [])
    applied = 0
    misleading_applied = 0
    skipped = 0
    corrected_indices = set()
    for v in corrections:
        try:
            idx = int(v.get("segment_index"))
        except (TypeError, ValueError):
            skipped += 1
            continue
        if not (0 <= idx < len(segments)):
            skipped += 1
            continue
        segments[idx]["narration"] = v["correction"]
        corrected_indices.add(idx)
        applied += 1
        if v.get("verdict") == "MISLEADING":
            misleading_applied += 1

    report["corrections_applied"] = applied
    report["misleading_corrections_applied"] = misleading_applied
    if skipped:
        report["corrections_skipped"] = skipped
    if corrected_indices:
        # The old query described narration that is no longer spoken.  Rebuild
        # only the corrected segments from the now-final narration so Pexels
        # cannot fetch footage for the rejected factual claim.
        rebuilt = script_writer.derive_visual_queries(
            script.get("topic_subject", ""),
            [str(segment.get("narration", "")) for segment in segments],
        )
        for idx in corrected_indices:
            if idx < len(rebuilt):
                segments[idx]["visual_query"] = rebuilt[idx]["visual_query"]
                segments[idx]["visual_fallback"] = rebuilt[idx]["visual_fallback"]
        report["visual_queries_refreshed"] = len(corrected_indices)
    return script


# ---------------------------------------------------------- publication policy

class ContradictedFinalSegment(RuntimeError):
    """The payoff line contradicts the source and could not be corrected."""


def uncorrected_contradictions(report: dict) -> list[dict]:
    """Verdicts that were CONTRADICTED and had no usable correction to apply.

    A contradicted claim WITH a correction is not a problem: apply_corrections
    rewrites that segment's narration and refreshes its visual query, so the
    thing that reaches the channel is the corrected sentence. A contradicted
    claim WITHOUT a correction is different — nothing rewrote it, and it is
    still in the script."""
    return [
        v for v in report.get("verdicts") or []
        if v.get("verdict") == "CONTRADICTED" and not v.get("correction")
    ]


def enforce_publication_policy(report: dict) -> list[dict]:
    """THE POLICY, in one place, for a channel whose only durable asset is
    being believed.

    Measured 2026-09-09 across 113 published videos: 31 carried a contradicted
    claim and 28 a misleading one. Nearly all were auto-corrected — that is what
    apply_corrections is for — but nothing in the pipeline distinguished
    "contradicted and fixed" from "contradicted and shipped anyway", and nothing
    treated the final segment as special.

    The rules, in order of severity:

    1. An UNCORRECTED contradiction in the FINAL segment BLOCKS publication.
       The last line is the payoff — it is the answer the whole video promises —
       and a wrong one is the version viewers remember and repeat. Raising here
       costs one slot; publishing it costs credibility that no later slot buys
       back. The story is marked failed and the next run takes the next story.

    2. An UNCORRECTED contradiction anywhere else is recorded as a degradation
       and alerted on, but does not block. The surrounding segments are
       supporting detail, the run has already paid for research and a render,
       and a channel that refuses to publish over one unfixable supporting claim
       stops publishing.

    3. A CORRECTED contradiction is not a problem at all and is not reported
       here. The correction is the fix.

    Returns the non-blocking problems for the caller to record. Raises
    ContradictedFinalSegment for rule 1."""
    if report.get("status") != "checked":
        # Grounding did not run (no article, network failure). That is already
        # visible as a degradation elsewhere; it is not a contradiction.
        return []

    uncorrected = uncorrected_contradictions(report)
    if not uncorrected:
        return []

    final_index = report.get("final_segment_index")
    blocking = [
        v for v in uncorrected
        if (final_index is not None and v.get("segment_index") == final_index)
        or (final_index is None and report.get("final_segment_contradicted")
            and v is uncorrected[-1])
    ]
    if blocking:
        claims = "; ".join(str(v.get("claim") or "")[:160] for v in blocking)
        raise ContradictedFinalSegment(
            "The final segment contradicts the source and no correction was "
            f"available: {claims}. Refusing to publish — the closing line is "
            "the one viewers remember. This slot is skipped; the story is "
            "marked failed and the next run takes the next story.")
    return uncorrected
