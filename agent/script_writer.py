"""
The script CONTRACT: what a story has to look like before it may be rendered.

This module used to generate the script too, by calling Gemini once per
scheduled slot. It no longer generates anything. A Claude Cowork task writes a
week of stories in advance into content/weekly_story_packet.json and
agent/packet.py hands one to the pipeline; see that module for why.

What stayed is everything that was actually load-bearing: the validators. Every
rule below was written because a real run shipped something bad or threw a slot
away — the 12-word hook ceiling, the banned throat-clearing openers, the
subject-anchored visual_query, the segment_index that lets a correction reach
the right line, the two trimmers that repair a length overrun instead of losing
the video. A packet story has to clear exactly the same bar a generated script
did, so agent/packet.py validates against these same functions rather than
inventing a second, weaker standard.

PROMPT_TEMPLATE is kept, unused by any code path here, as the specification the
weekly Claude task writes against: it is the full statement of what a good
story for this channel is, and losing it would lose the reasoning behind every
rule below.
"""
import re

from . import config


PROMPT_TEMPLATE = """You are writing a narration script for a short faceless YouTube
video about: {niche}

This is a STORY, not a list of trivia. Write {segments} segments, meant to be
read aloud in about {length} seconds total (~150 words/min), that build a
single narrative arc. Every video is short, so every line has to earn its
place — no padding, no throat-clearing.

- Segment 1 is the HOOK, and it decides whether the video is watched at all.
  Almost half of viewers swipe away in the first 1-2 seconds, so the very
  first sentence must be a genuine PATTERN INTERRUPT: a concrete, specific,
  strange detail, or an open loop the viewer needs closed. Hard rules:
    * The first sentence is UNDER 12 WORDS. Count them.
    * The hook lands BEFORE any context. Do not set the scene first. Opening
      with "In 1923, in a small village in..." is exactly the failure mode —
      the strange thing comes first, the date and place come after.
    * No throat-clearing of any kind: no "Did you know", "Here's a bizarre
      fact", "Imagine this", "What if I told you", "Let me tell you about".
    * If the hook is a question, it must be one the viewer already cares
      about the instant they hear it — not one that only becomes interesting
      after an explanation. "Why did the room smell like almonds?" is dead on
      arrival; nobody cares yet. Prefer a flat strange statement over a
      question.
    * The hook must be factually TRUE and tagged as a factual_claim like any
      other assertion — a fabricated or exaggerated hook is not acceptable
      even though it is the most important line.
- The open loop the hook creates MUST STAY OPEN until the final segment. Do
  not answer it, explain it, or defuse it in segments 2, 3, or 4 — those
  escalate and complicate it. The payoff belongs to the last segment only.
- The middle segments ESCALATE, and about halfway through, drop in a sharp
  rhetorical question or a provocative one-line statement that re-hooks
  attention — a second jolt, not just a recap. Each segment should deepen the
  mystery or raise the stakes from the one before it, the way a story builds —
  not a series of disconnected facts that could be reordered without losing
  anything. Reference or build on what was just said.
- After the escalation, there is a TURN where the real explanation unfolds:
  give the REAL resolution, explanation, or most credible theory for the
  mystery — do not leave it hanging. If the historical record has an
  accepted explanation (even a boring one — "it was swamp gas," "the killer
  was caught two years later," "a documented structural failure") USE it.
  If the case is genuinely unsolved, present the single most credible
  leading theory, backed by real evidence or investigation, explained
  clearly enough the viewer understands WHY it's credible — not a vague
  gesture at "theories exist."
- The final segment lands that resolution with one satisfying closing line.
  It can still be surprising or mind-blowing even though it's resolved — the
  explanation itself, or a detail inside it, can BE the twist. Do not end on
  the cliché "and to this day, no one knows" unless the case is truly,
  provably still open with zero leading theory — that line is banned for
  every case that has an accepted explanation or a credible leading theory.

Writing style — this is the part that matters most:
- Short, punchy sentences. Vary sentence length for rhythm, but bias hard
  toward short.
- Concrete, vivid, specific imagery — a rusted hinge, a name carved in wood,
  a light that shouldn't be on — never vague or generic description.
- Strong, sharp word choice. Cut every word that isn't pulling weight. Modern,
  current, internet-native phrasing — the way a genuinely interesting friend
  tells you a wild true story they just found out about, not old-fashioned or
  overly literary language.
- Fast, punchy, modern energy — curious and captivating, not a scary
  late-night horror narrator. You're hyping up a wild true story, not
  trying to spook anyone. Avoid filler like "In this video" or "let's dive
  in."
- Family-friendly / PG, even for dark historical events: no gore, no
  graphic or disturbing physical detail. Lean into the mystery and
  intrigue of what happened, not shock value. The bar: a genuinely curious
  12-year-old and a 40-year-old should both find this equally gripping —
  broad, all-ages appeal, not a niche horror audience.
{ending_block}
{avoid_block}
HOOK SELECTION — do this before writing the script, and show your work:
Write THREE genuinely different candidate opening lines for this story. Not
three rewordings of the same sentence — three different angles into it (e.g.
one built on the strangest physical detail, one on the most incomplete/open
question, one on a number or fact that sounds impossible). Then score each
candidate 1-10 on these four criteria and pick the highest total:
  1. STOPPING POWER — would this stop a thumb mid-scroll? Is it strange
     enough that scrolling past feels like losing something?
  2. SPECIFICITY — a concrete detail, name, number or image, not a vague
     tease. "Every clock aboard had stopped at 10:25" beats "something
     strange happened aboard that ship".
  3. OPEN LOOP — does it create a question the viewer needs answered, that
     the video can hold open until the very end?
  4. NO CONTEXT REQUIRED — does it land instantly, with zero setup, to
     someone who has never heard of this case?
Then use the winning candidate, verbatim, as the first sentence of segment 1.

Return ONLY valid JSON, no markdown fences, in this exact shape:
{{
  "title": "clickable YouTube title, under 70 chars",
  "description": "2-3 sentence YouTube description with 3 relevant hashtags",
  "hook_candidates": [
    {{"text": "the candidate opening line, under 12 words",
      "stopping_power": 0, "specificity": 0, "open_loop": 0,
      "no_context_required": 0, "total": 0,
      "why": "one line on what this angle leads with"}}
  ],
  "hook_choice": {{
    "chosen_index": "0-based index into hook_candidates of the winner",
    "reason": "one sentence on why this one beat the other two"
  }},
  "topic_subject": "the BARE article title of the real-world subject (a proper noun where one exists), 1-4 words, with NO descriptive suffix (e.g. 'Lake Natron calcification phenomenon' is WRONG because search returned the Tibesti Mountains for it, 'Lake Natron' is RIGHT) — used to fact-check the script",
  "factual_claims": [
    {{"text": "one concrete, checkable factual assertion the narration makes
        (dates, names, places, numbers, outcomes), as a short standalone
        sentence — does not need to quote the segment verbatim",
      "segment_index": "0-based index into the segments array below, for the
        SINGLE segment this claim is drawn from — required, must be a real
        index, this is how a correction gets applied back to the right line"}}
  ],
  "segments": [
    {{"narration": "text to be spoken for this segment",
      "visual_query": "3-6 words anchoring shot to actual subject location/era/culture — see rule below",
      "visual_fallback": "2-4 words generic mood/scene fallback — see rule below"}}
  ]
}}

visual_query / visual_fallback rule: Stock footage libraries do not have literal narrative
props (a particular mask, a particular notebook), so do not ask for specific props. However,
they DO have real locations, eras, cultures, and landscape types, and those MUST be in
visual_query. Anchor the shot to the ACTUAL subject's place, era, or landscape (3-6 words).
Use visual_fallback for a 2-4 word generic scene/mood fallback when the anchored query yields no results.
Examples:
- For a story about a hot alkaline lake in Tanzania: "still lake" is WRONG (returns snowy alpine lakes);
  "east african salt flat lake" is RIGHT.
- For an Egyptian mummification segment: "ancient ruins" is WRONG (returns Greco-Roman columns);
  "egyptian tomb hieroglyphs" is RIGHT.

factual_claims / segment_index rule: segment_index must point at exactly the
segment that stated the claim, counting from 0 in the order segments appear
below. This is used to automatically fix that one line if the claim turns
out to be wrong, so an incorrect index would edit the wrong sentence.

The explanation/resolution segment(s) MUST contribute at least one
factual_claim each — the resolution is the part most likely to be wrong or
oversimplified if it goes ungrounded, so it needs to be fact-checked like
every setup detail, not asserted for free. This includes the FINAL segment
specifically: if it only restates or rephrases a fact already covered by an
earlier segment's claim, it doesn't need its own claim — but if it asserts
ANY new factual detail not already covered by an earlier claim, it MUST get
its own factual_claim tagged to it. A closing line is not exempt just for
being last.

hook rules recap (these are validated automatically, a violation is a failed
generation): exactly THREE entries in hook_candidates; hook_choice.chosen_index
must be a real index into it; and the chosen candidate's text must appear
VERBATIM as the opening sentence of segments[0].narration. The hook also needs
its own factual_claim tagged to segment_index 0.
"""


# One in this many videos gets the question-ending variant, decided here in
# Python rather than left to the model's judgment of "occasional" — a model
# asked to be sparing about something with no external counter tends to drift
# toward doing it every time (or never), same failure mode as "sometimes use
# emoji". Roughly 1-in-3 keeps it noticeable without becoming a tic.
QUESTION_ENDING_PROBABILITY = 0.35

STATEMENT_ENDING_BLOCK = ""

QUESTION_ENDING_BLOCK = """
- For THIS video, close the final segment on a genuine, specific question
  instead of a declarative line — but only if one actually fits. The
  question must be EARNED by the resolution you just gave, not bolted on:
  it should point at a real, specific unresolved edge of the story (a detail
  the accepted explanation doesn't cover, a choice one specific person made,
  what a specific piece of evidence really implies) — not a generic
  engagement-bait line. Banned: "What do you think?", "What would you have
  done?", "Let me know in the comments", or any question that isn't actually
  about the case. If no genuine question grows naturally out of THIS
  specific resolution, use a strong declarative closing line instead — a
  forced question is worse than none."""

HOOK_MAX_WORDS = 12
REQUIRED_HOOK_CANDIDATES = 3
# Openers that are pure throat-clearing — the model is told to avoid these,
# this is the backstop that actually catches it.
BANNED_OPENERS = (
    "did you know", "here's a bizarre", "here is a bizarre", "imagine",
    "what if i told you", "let me tell you", "in this video", "picture this",
)
# Bare generic queries that lack a specific subject anchor (location, era, culture).
# These return irrelevant B-roll on stock libraries and are banned as primary visual_query.
MAX_VISUAL_QUERY_WORDS = 6


def trim_long_visual_queries(entries: list) -> list[str]:
    """Shortens any visual_query longer than MAX_VISUAL_QUERY_WORDS, in place.

    Returns a note per query trimmed, for the caller to record as a degradation.

    This exists because on 2026-08-13 a whole scheduled slot was thrown away
    over two words. Gemini returned 'lake hillier western australia pink water
    aerial' (7 words) twice running, validation rejected both attempts, and
    generate_script raised - no video, ~3 minutes of CI and two Gemini calls
    of a 20/day allowance spent for nothing.

    That trade is backwards. The word limit exists because long queries return
    nothing on stock libraries, and agent/visuals.py ALREADY handles a query
    that returns nothing: rung 1 is visual_query, rung 2 is visual_fallback,
    and NoRelevantResults falls straight through between them. So an over-long
    query costs, at worst, one rung of a ladder built for exactly this. Losing
    the slot costs the whole video.

    It is the same asymmetry this codebase already settled for duplicate
    topics (see generate_script below): a wrongly-blocked run costs the slot
    AND leaves nothing to show for it, so the cautious-looking choice is the
    expensive one. Trimming keeps the leading words, which is where the model
    puts the subject anchor - 'lake hillier western australia pink water' is
    still a good query.

    Deliberately only shortens. A query with too FEW words cannot be repaired
    by inventing terms, so that stays a real validation failure for the
    corrective re-ask to fix."""
    notes = []
    for idx, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        original = (entry.get("visual_query") or "").strip()
        words = original.split()
        if len(words) <= MAX_VISUAL_QUERY_WORDS:
            continue
        trimmed = " ".join(words[:MAX_VISUAL_QUERY_WORDS])
        entry["visual_query"] = trimmed
        notes.append(f"segment {idx}: {original!r} -> {trimmed!r}")
    return notes


MAX_TOPIC_SUBJECT_WORDS = 4


def trim_long_topic_subject(data: dict) -> str | None:
    """Shortens a topic_subject longer than MAX_TOPIC_SUBJECT_WORDS, in place.

    Returns a note describing the trim, or None if nothing needed trimming.

    Exactly the trade trim_long_visual_queries settled, applied to the other
    field that was still throwing a slot away over word count. A topic_subject
    of 'Lake Natron calcification phenomenon' failed validation, burned the
    corrective re-ask, and — when the second attempt came back long too —
    raised out of generate_script, losing the video, the CI minutes and two
    Gemini calls out of a 20/day free-tier allowance.

    Trimming to the LEADING words is not a guess: the prompt already tells the
    model to put the bare article title first and strip the descriptive suffix,
    and its own worked example is 'Lake Natron calcification phenomenon' ->
    'Lake Natron'. Taking the first four words performs that instruction rather
    than inventing anything, and the downstream consumers all improve from it —
    grounding.py searches Wikipedia with this string (a bare article title is
    what actually resolves), history.py compares it for duplicate detection,
    and upload.py turns it into tags.

    Deliberately only shortens. A missing or empty topic_subject cannot be
    repaired by inventing one, so that stays a real validation failure for the
    corrective re-ask to fix."""
    subject = (data.get("topic_subject") or "").strip()
    words = subject.split()
    if len(words) <= MAX_TOPIC_SUBJECT_WORDS:
        return None
    trimmed = " ".join(words[:MAX_TOPIC_SUBJECT_WORDS])
    data["topic_subject"] = trimmed
    return f"{subject!r} -> {trimmed!r}"


BANNED_GENERIC_QUERIES = {
    "dark background", "fog", "ancient ruins", "still lake",
    "old photographs", "abstract dark", "candle in dark room", "stormy ocean",
}


def _first_sentence(text: str) -> str:
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return parts[0].strip() if parts else text.strip()


def validate_script(data: dict) -> list[str]:
    """Structural + hook-quality checks. Returns a list of human-readable
    problems (empty means valid) which is fed back to the model verbatim on a
    re-ask, so the retry is corrective rather than just another dice roll."""
    problems = []

    topic_subject = (data.get("topic_subject") or "").strip()
    if not topic_subject:
        problems.append("topic_subject is missing or empty.")
    else:
        words = topic_subject.split()
        if len(words) > MAX_TOPIC_SUBJECT_WORDS:
            problems.append(
                f"topic_subject ('{topic_subject}') is {len(words)} words — "
                f"must be at most {MAX_TOPIC_SUBJECT_WORDS} words and use the bare "
                "article title (no descriptive suffix)."
            )

    segments = data.get("segments") or []
    if not segments:
        problems.append("segments is empty — at least one segment is required.")
        return problems

    opening = _first_sentence(segments[0].get("narration", ""))
    word_count = len(opening.split())
    if word_count > HOOK_MAX_WORDS:
        problems.append(
            f"The first sentence of segment 0 is {word_count} words "
            f"('{opening}') — it must be under {HOOK_MAX_WORDS} words."
        )
    low = opening.lower()
    for banned in BANNED_OPENERS:
        if low.startswith(banned):
            problems.append(
                f"The hook opens with banned throat-clearing '{banned}...'. "
                "Open on the strange detail itself instead."
            )
            break

    candidates = data.get("hook_candidates") or []
    if len(candidates) != REQUIRED_HOOK_CANDIDATES:
        problems.append(
            f"hook_candidates has {len(candidates)} entries — exactly "
            f"{REQUIRED_HOOK_CANDIDATES} are required."
        )
    else:
        choice = data.get("hook_choice") or {}
        try:
            idx = int(choice.get("chosen_index"))
        except (TypeError, ValueError):
            idx = None
        if idx is None or not (0 <= idx < len(candidates)):
            problems.append(
                f"hook_choice.chosen_index ({choice.get('chosen_index')!r}) is "
                "not a valid index into hook_candidates."
            )
        else:
            chosen = (candidates[idx].get("text") or "").strip().rstrip(".!?")
            if chosen and chosen.lower() not in opening.lower():
                problems.append(
                    f"The chosen hook ('{chosen}') does not appear verbatim as "
                    f"the opening sentence of segment 0 ('{opening}')."
                )

    claims = data.get("factual_claims") or []
    if not any(str(c.get("segment_index")) == "0" for c in claims):
        problems.append(
            "No factual_claim is tagged to segment_index 0 — the hook makes a "
            "factual assertion and must be fact-checkable like any other line."
        )

    for idx, seg in enumerate(segments):
        vq = (seg.get("visual_query") or "").strip()
        vf = (seg.get("visual_fallback") or "").strip()
        if not vq:
            problems.append(f"Segment {idx} is missing a visual_query.")
        else:
            wc = len(vq.split())
            if not (3 <= wc <= 6):
                problems.append(
                    f"Segment {idx} visual_query ('{vq}') is {wc} words — "
                    "must be 3-6 words."
                )
            if vq.lower() in BANNED_GENERIC_QUERIES:
                problems.append(
                    f"Segment {idx} visual_query ('{vq}') is a bare banned generic query. "
                    "Anchor the query to the specific location, era, or culture instead."
                )
        if not vf:
            problems.append(f"Segment {idx} is missing a visual_fallback.")

    return problems


VISUAL_QUERY_RULE = """visual_query / visual_fallback rule: Stock footage libraries do not have literal narrative
props (a particular mask, a particular notebook), so do not ask for specific props. However,
they DO have real locations, eras, cultures, and landscape types, and those MUST be in
visual_query. Anchor the shot to the ACTUAL subject's place, era, or landscape (3-6 words).
Use visual_fallback for a 2-4 word generic scene/mood fallback when the anchored query yields no results.
Examples:
- For a story about a hot alkaline lake in Tanzania: "still lake" is WRONG (returns snowy alpine lakes);
  "east african salt flat lake" is RIGHT.
- For an Egyptian mummification segment: "ancient ruins" is WRONG (returns Greco-Roman columns);
  "egyptian tomb hieroglyphs" is RIGHT."""


def validate_visual_queries(queries: list, segment_count: int) -> list[str]:
    """The same visual-query rules validate_script() enforces, applied to a
    bare list of query pairs. Kept separate so a re-render can re-ask for just
    the queries without regenerating (and re-fact-checking) a whole script."""
    problems = []
    if not isinstance(queries, list) or len(queries) != segment_count:
        return [f"Expected exactly {segment_count} entries, got "
                f"{len(queries) if isinstance(queries, list) else type(queries).__name__}."]
    for idx, entry in enumerate(queries):
        if not isinstance(entry, dict):
            problems.append(f"Entry {idx} is not an object.")
            continue
        vq = (entry.get("visual_query") or "").strip()
        vf = (entry.get("visual_fallback") or "").strip()
        if not vq:
            problems.append(f"Entry {idx} is missing a visual_query.")
        else:
            wc = len(vq.split())
            if not (3 <= wc <= 6):
                problems.append(
                    f"Entry {idx} visual_query ('{vq}') is {wc} words — must be 3-6 words.")
            if vq.lower() in BANNED_GENERIC_QUERIES:
                problems.append(
                    f"Entry {idx} visual_query ('{vq}') is a bare banned generic query. "
                    "Anchor the query to the specific location, era, or culture instead.")
        if not vf:
            problems.append(f"Entry {idx} is missing a visual_fallback.")
    return problems


# Words that describe the shape of a sentence rather than its subject. Dropped
# when a visual query has to be rebuilt from narration text.
_QUERY_STOPWORDS = frozenset("""
a an and are as at be been but by can could did do does for from had has have
he her him his how i if in into is it its me my no not of on one or our out
over said she so than that the their them then there these they this to too
until up was we were what when where which who why will with would you your
""".split())


def derive_visual_queries(subject: str, narrations: list[str]) -> list[dict]:
    """Rebuilds anchored visual queries for an existing script, without a model.

    Only used by scripts/reupload_video.py, for re-rendering a video whose
    stored queries predate the anchored-visual fix — reusing those would
    faithfully reproduce the generic footage that fix removed.

    This used to be one Gemini call. It is now arithmetic on text the repo
    already holds, which is strictly better for the job: the rule the model was
    being asked to follow ("anchor the shot to the subject's place, era or
    landscape") is satisfied by literally putting the subject in front of the
    segment's own strongest nouns, and a re-render is no longer one model
    outage away from being impossible.

    Produces MAX_VISUAL_QUERY_WORDS words at most and three at least — the same
    3-6 window validate_visual_queries enforces — by taking up to three words
    of the subject and topping up from the narration's own longest non-filler
    words, which are where its concrete nouns are."""
    subject_words = [w for w in re.findall(r"[A-Za-z0-9]+", subject or "")][:3]
    out = []
    for narration in narrations:
        words = re.findall(r"[A-Za-z0-9]+", narration or "")
        salient = sorted(
            {w.lower() for w in words
             if len(w) > 3 and w.lower() not in _QUERY_STOPWORDS},
            key=lambda w: (-len(w), w),
        )
        query = [w.lower() for w in subject_words]
        for word in salient:
            if len(query) >= MAX_VISUAL_QUERY_WORDS:
                break
            if word not in query:
                query.append(word)
        # Below three words nothing above could fix it — pad from the subject
        # itself so the query still names what the video is about.
        while len(query) < 3:
            # `(subject or "history").split()[0]` raised IndexError for a
            # whitespace-only subject, which is truthy but splits to nothing.
            query.append(((subject or "").split() or ["history"])[0].lower())
        out.append({
            "visual_query": " ".join(query[:MAX_VISUAL_QUERY_WORDS]),
            "visual_fallback": " ".join(query[:2]) or "archive footage",
        })
    return out
