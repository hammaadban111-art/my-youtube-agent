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
import urllib.parse
import urllib.request
from google import genai
from . import config, gemini_utils

WIKI_API = "https://en.wikipedia.org/w/api.php"
USER_AGENT = "faceless-youtube-agent/1.0 (fact-checking generated scripts)"
MAX_ARTICLE_CHARS = 12000

VERIFY_PROMPT = """You are fact-checking a short video script against a reference article.

REFERENCE ARTICLE ({title}):
{article}

CLAIMS MADE IN THE SCRIPT (each tagged with the segment_index it came from):
{claims}

For each claim, decide whether the reference article SUPPORTS it, CONTRADICTS
it, or is SILENT on it (the article simply doesn't cover that detail — this is
normal and is not a failure).

Only mark CONTRADICTED when the article states something genuinely incompatible
with the claim, not merely different wording or extra detail.

Return ONLY valid JSON, no markdown fences. Echo each claim's segment_index
back unchanged — it identifies which line of narration to fix, so it must
exactly match the segment_index given for that claim above:
{{
  "verdicts": [
    {{"claim": "...", "segment_index": 0, "verdict": "SUPPORTED|CONTRADICTED|SILENT",
      "note": "brief reason", "correction": "corrected fact, or null"}}
  ]
}}
"""


def _wiki_get(params: dict) -> dict:
    url = f"{WIKI_API}?{urllib.parse.urlencode({**params, 'format': 'json'})}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def fetch_source(subject: str) -> tuple[str, str] | None:
    """Finds the best-matching Wikipedia article. Returns (title, text)."""
    if not subject:
        return None
    hits = _wiki_get({
        "action": "query", "list": "search", "srsearch": subject, "srlimit": 1
    })["query"]["search"]
    if not hits:
        return None

    title = hits[0]["title"]
    pages = _wiki_get({
        "action": "query", "prop": "extracts", "explaintext": "1", "titles": title
    })["query"]["pages"]
    extract = next(iter(pages.values())).get("extract", "")
    if not extract:
        return None
    return title, extract[:MAX_ARTICLE_CHARS]


def verify_claims(claims: list[dict], title: str, article: str) -> list[dict]:
    if not claims:
        return []
    client = genai.Client(api_key=config.GEMINI_API_KEY)
    prompt = VERIFY_PROMPT.format(
        title=title,
        article=article,
        claims="\n".join(
            f"- [segment_index {c.get('segment_index')}] {c.get('text', '')}"
            for c in claims
        ),
    )
    response = gemini_utils.call_with_retry(
        lambda: client.models.generate_content(model="gemini-flash-latest", contents=prompt),
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

    try:
        source = fetch_source(subject)
        if not source:
            return {"source": "wikipedia", "status": "no_source_found",
                    "subject": subject, "claims_checked": 0, "contradicted": 0,
                    "verdicts": []}

        title, article = source
        verdicts = verify_claims(claims, title, article)
        contradicted = [v for v in verdicts if v.get("verdict") == "CONTRADICTED"]
        return {
            "source": "wikipedia",
            "status": "checked",
            "subject": subject,
            "article": title,
            "article_url": f"https://en.wikipedia.org/wiki/{urllib.parse.quote(title.replace(' ', '_'))}",
            "claims_checked": len(verdicts),
            "supported": sum(1 for v in verdicts if v.get("verdict") == "SUPPORTED"),
            "silent": sum(1 for v in verdicts if v.get("verdict") == "SILENT"),
            "contradicted": len(contradicted),
            "verdicts": verdicts,
        }
    except Exception as e:  # noqa: BLE001 - reported, never fatal
        return {"source": "wikipedia", "status": "error", "subject": subject,
                "error": f"{type(e).__name__}: {e}",
                "claims_checked": 0, "contradicted": 0, "verdicts": []}


def apply_corrections(script: dict, report: dict) -> dict:
    """Rewrites the narration for each contradicted claim's segment.

    Uses the segment_index Gemini attached to the claim at generation time,
    rather than matching the claim's text against the narration: the claim is
    Gemini's own paraphrase of the fact, not a quote from the script, so it
    routinely shares no substring with the line it was drawn from — that was
    the bug (corrections_applied stuck at 0 on a real run: 2026-07-30,
    video K5-7-HTo38M, "footprints... into the attic" claim vs. narration
    "footsteps leading straight into his attic"). A structural index survives
    paraphrasing; string matching can't.
    """
    corrections = [
        v for v in report.get("verdicts", [])
        if v.get("verdict") == "CONTRADICTED" and v.get("correction")
    ]
    if not corrections:
        return script

    segments = script.get("segments", [])
    applied = 0
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

    report["corrections_applied"] = applied
    if skipped:
        report["corrections_skipped"] = skipped
    return script
