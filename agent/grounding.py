"""
Fact-grounds each script against Wikipedia before it reaches TTS.

Gemini's native Google Search grounding is not available on the free tier —
verified against this project's own API key: an identical request succeeds
without the google_search tool and returns 429 RESOURCE_EXHAUSTED with it,
so the grounding quota is zero rather than the key being rate limited.
Enabling it requires billing, so we use Wikipedia's keyless API instead.

Source lookup is behind fetch_source(), so swapping in a different provider
later (including paid Gemini grounding) is a contained change.
"""
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from google import genai
from . import config, gemini_utils, resilience

WIKI_API = "https://en.wikipedia.org/w/api.php"
USER_AGENT = "faceless-youtube-agent/1.0 (fact-checking generated scripts)"
MAX_ARTICLE_CHARS = 12000

VERIFY_PROMPT = """You are fact-checking a short video script against reference articles.

REFERENCE ARTICLES:
{articles}

CLAIMS MADE IN THE SCRIPT (each tagged with segment_index and verbatim narration):
{claims}

For each claim, decide whether the reference article SUPPORTS it, CONTRADICTS
it, MISLEADS / is MISLEADING, or is SILENT on it (the article simply doesn't cover that detail — this is normal and is not a failure).

A claim is MISLEADING when it is technically adjacent to the source but overstates, sensationalizes, or strips the context that makes it true — specifically including:
* a different physical mechanism dressed up as vivid language (describing sodium-carbonate preservation, i.e. natural mummification, as turning animals "to stone" or "to solid rock"),
* presenting deliberately staged, arranged or artistic material as something found naturally (a photographer's posed carcass photographs described as bodies "lining the shoreline"),
* a real number or event given without the qualifier the source attaches to it.

For any claim that is CONTRADICTED or MISLEADING, you MUST provide a "correction": a rewritten narration line that keeps the narrative energy but is accurate. For SUPPORTED or SILENT claims, set "correction" to null.

Return ONLY valid JSON, no markdown fences. Echo each claim's segment_index back unchanged — it identifies which line of narration to fix, so it must exactly match the segment_index given for that claim above:
{{
  "verdicts": [
    {{"claim": "...", "segment_index": 0, "verdict": "SUPPORTED|CONTRADICTED|MISLEADING|SILENT",
      "note": "brief reason", "correction": "corrected narration line, or null"}}
  ]
}}
"""


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
        attempts=3,
        base_delay=0.5,
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


def verify_claims(claims: list[dict], sources, article: str = None, segments: list[dict] = None) -> list[dict]:
    """Fact-checks claims against reference articles and verbatim segment narration.
    Accepts list of (title, text) pairs or legacy (title, article) signature."""
    if not claims:
        return []

    if isinstance(sources, str):
        sources_list = [(sources, article or "")]
    elif isinstance(sources, tuple) and len(sources) == 2:
        sources_list = [sources]
    elif isinstance(sources, list):
        sources_list = sources
    else:
        sources_list = []

    if not sources_list:
        return []

    client = genai.Client(api_key=config.GEMINI_API_KEY)
    articles_block = "\n\n".join(
        f"REFERENCE ARTICLE ({t}):\n{a}" for t, a in sources_list
    )

    claims_formatted = []
    for c in claims:
        idx = _safe_int(c.get("segment_index"))
        narration = ""
        if segments and idx is not None and 0 <= idx < len(segments):
            narration = segments[idx].get("narration", "")

        entry = f"- [segment_index {c.get('segment_index')}] Claim: {c.get('text', '')}"
        if narration:
            entry += f"\n  Verbatim Narration: {narration}"
        claims_formatted.append(entry)

    prompt = VERIFY_PROMPT.format(
        articles=articles_block,
        claims="\n".join(claims_formatted),
    )
    response = gemini_utils.call_with_retry(
        lambda model: client.models.generate_content(model=model, contents=prompt),
        label="verify_claims",
    )
    text = re.sub(r"^```(json)?|```$", "", response.text.strip(), flags=re.MULTILINE).strip()
    return json.loads(text).get("verdicts", [])


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
                    "subject": subject, "source_relevant": False, "coverage": 0.0,
                    "unverified_source": False, "claims_checked": 0, "contradicted": 0,
                    "misleading": 0, "accuracy_flags": [],
                    "final_segment_grounded": False, "verdicts": []}

        # Primary title for report header URL
        title = sources[0][0] if isinstance(sources, list) else sources[0]
        verdicts = verify_claims(claims, sources, segments=segments)

        # Per-claim source lookup for uncovered (SILENT) claims.
        # Bound the extra work: at most 3 extra article fetches and one extra Gemini call per script.
        # This runs 4x/day on a free tier.
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
                second_verdicts = verify_claims(silent_claims, extra_sources, segments=segments)
                second_map = {}
                for sv in second_verdicts:
                    idx = _safe_int(sv.get("segment_index"))
                    cl = sv.get("claim")
                    if idx is not None:
                        second_map[(idx, cl)] = sv
                        second_map[idx] = sv

                for i, v in enumerate(verdicts):
                    if v.get("verdict") == "SILENT":
                        idx = _safe_int(v.get("segment_index"))
                        cl = v.get("claim")
                        sv = second_map.get((idx, cl)) or (second_map.get(idx) if idx is not None else None)
                        if sv and sv.get("verdict") != "SILENT":
                            verdicts[i] = sv

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
        return {
            "source": "wikipedia",
            "status": "checked",
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
            "verdicts": verdicts,
        }
    except Exception as e:  # noqa: BLE001 - reported, never fatal
        return {"source": "wikipedia", "status": "error", "subject": subject,
                "error": f"{type(e).__name__}: {e}",
                "source_relevant": False, "coverage": 0.0, "unverified_source": False,
                "claims_checked": 0, "contradicted": 0, "misleading": 0,
                "accuracy_flags": [], "verdicts": []}


def apply_corrections(script: dict, report: dict) -> dict:
    """Rewrites the narration for each contradicted or misleading claim's segment.

    Uses the segment_index Gemini attached to the claim at generation time,
    rather than matching the claim's text against the narration: the claim is
    Gemini's own paraphrase of the fact, not a quote from the script, so it
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
        applied += 1
        if v.get("verdict") == "MISLEADING":
            misleading_applied += 1

    report["corrections_applied"] = applied
    report["misleading_corrections_applied"] = misleading_applied
    if skipped:
        report["corrections_skipped"] = skipped
    return script
