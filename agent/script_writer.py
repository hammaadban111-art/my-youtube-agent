"""
Generates a video script + per-segment visual keywords using the free
Gemini API tier. Output is strict JSON so the rest of the pipeline can
consume it without any manual step.
"""
import json
import random
import re
from google import genai
from . import benchmark, config, gemini_utils, history, predict, resilience


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


VISUAL_QUERY_PROMPT = """You are choosing stock-footage search queries for an
existing narration script about: {subject}

The script is finished and must not change. Only the search queries change.

{rule}

Return ONLY valid JSON, no markdown fences, an array with exactly {count}
entries, one per segment, in order:
[{{"visual_query": "3-6 words", "visual_fallback": "2-4 words"}}]

The segments, in order:
{segments}
"""

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


def regenerate_visual_queries(subject: str, narrations: list[str]) -> list[dict]:
    """Writes fresh anchored visual queries for an existing script.

    For re-rendering a video whose stored queries predate the anchored-visual
    fix — reusing those would faithfully reproduce the generic footage that fix
    removed. Costs ONE Gemini call (the narration itself is reused, so this is
    the only call a re-render ever makes), and validates the result against the
    same rules a fresh script has to pass."""
    client = genai.Client(api_key=config.GEMINI_API_KEY)
    segments_block = "\n".join(
        f"{i}. {n}" for i, n in enumerate(narrations))
    prompt = VISUAL_QUERY_PROMPT.format(
        subject=subject, rule=VISUAL_QUERY_RULE,
        count=len(narrations), segments=segments_block)

    attempt_prompt = prompt
    last_problems = None
    for attempt in range(1, 3):
        response = gemini_utils.call_with_retry(
            lambda model: client.models.generate_content(
                model=model, contents=attempt_prompt),
            label=f"regenerate_visual_queries (attempt {attempt})",
        )
        text = re.sub(r"^```(json)?|```$", "", (response.text or "").strip(),
                      flags=re.MULTILINE).strip()
        try:
            queries = json.loads(text)
            # Same repair as generate_script: a re-render must not be lost to
            # a query that is one word too long either.
            trimmed = trim_long_visual_queries(queries if isinstance(queries, list) else [])
            if trimmed:
                resilience.record_degradation(
                    "visual-query-length",
                    f"{len(trimmed)} regenerated visual query/queries were longer "
                    f"than {MAX_VISUAL_QUERY_WORDS} words: " + "; ".join(trimmed),
                    "trimmed to the leading words rather than failing the re-render",
                )
            problems = validate_visual_queries(queries, len(narrations))
        except json.JSONDecodeError as e:
            problems = [f"The response was not valid JSON ({e})."]

        if not problems:
            return [{"visual_query": q["visual_query"].strip(),
                     "visual_fallback": q["visual_fallback"].strip()} for q in queries]

        last_problems = problems
        print(f"      visual queries attempt {attempt} rejected: {'; '.join(problems)}")
        attempt_prompt = (
            prompt
            + "\n\nYour previous attempt was REJECTED for these specific reasons:\n"
            + "\n".join(f"- {p}" for p in problems)
            + "\nFix every one of them. Return the complete corrected JSON only."
        )

    raise RuntimeError(
        "Visual query regeneration failed validation twice: " + "; ".join(last_problems or []))


def generate_script() -> dict:
    client = genai.Client(api_key=config.GEMINI_API_KEY)

    use_question_ending = random.random() < QUESTION_ENDING_PROBABILITY
    ending_block = QUESTION_ENDING_BLOCK if use_question_ending else STATEMENT_ENDING_BLOCK

    recent_titles = history.load_recent_titles()
    avoid_block = ""
    if recent_titles:
        bullets = "\n".join(f"- {t}" for t in recent_titles)
        avoid_block = (
            "\nDo NOT repeat these topics/angles — they were already covered "
            f"in recent videos:\n{bullets}\nPick a genuinely different story.\n"
        )

    # B2 — duplicate topic detection. Two videos went up 3.5h apart on "Tamam
    # Shud case" under titles that share not one word, so the titles above
    # could never have caught it. The SUBJECTS are listed separately and, once
    # this is off its hold date, checked against the returned script for real.
    recent_subjects = history.load_recent_subjects() if history.detection_active() else []
    if recent_subjects:
        subject_bullets = "\n".join(f"- {s}" for s in recent_subjects)
        avoid_block += (
            "\nThese exact SUBJECTS have already been covered. Do not write "
            "about any of them again, under any title or angle:\n"
            f"{subject_bullets}\n"
        )

    # Performance biasing is part of the self-improving stage, so it stays off
    # until the channel has enough real history to draw a signal from.
    active, _ = predict.self_improve_active()
    if active:
        winners = predict.top_performers()
        if winners:
            lines = "\n".join(
                f"- {w['title']} ({w['views']} views)" for w in winners
            )
            avoid_block += (
                "\nThese past videos performed best on this channel:\n"
                f"{lines}\nLean toward the kind of subject matter, hook style, "
                "and pacing that made those work — without reusing their topics.\n"
            )

    # What the wider niche says about content TYPES, from the recurring scan.
    # Deliberately added alongside this channel's own results rather than in
    # place of them, and phrased as a tilt rather than an instruction: it is
    # measured against other channels' audiences, not ours, and its between-
    # category spread is about the same size as its within-category spread. It
    # can say which kinds of story tend to over-perform their channel's usual
    # numbers; it cannot say what any single video will do. Runs regardless of
    # the self-improving gate, since it is not learned from our own data and so
    # cannot be poisoned by a throttled window the way that gate exists to
    # prevent.
    ranked = benchmark.category_ranking()
    if ranked:
        strong = [c for c in ranked if c["multiplier"] > 1]
        weak = [c for c in ranked if c["multiplier"] < 1]
        parts = []
        if strong:
            parts.append("tend to OVER-perform: "
                         + ", ".join(f"{c['category'].replace('_', ' ')} "
                                     f"({c['multiplier']:.1f}x)" for c in strong))
        if weak:
            parts.append("tend to UNDER-perform: "
                         + ", ".join(f"{c['category'].replace('_', ' ')} "
                                     f"({c['multiplier']:.1f}x)" for c in weak))
        if parts:
            avoid_block += (
                "\nAcross this niche generally, compared with videos from "
                "channels of similar size, these kinds of story:\n  "
                + "\n  ".join(parts)
                + "\nTreat this as a mild tilt when the story is otherwise a "
                  "toss-up, not a rule — it is a niche-wide average, not a "
                  "prediction about any one video.\n"
            )

    prompt = PROMPT_TEMPLATE.format(
        niche=config.NICHE,
        segments=config.NUM_SCRIPT_SEGMENTS,
        length=config.VIDEO_LENGTH_SECONDS,
        ending_block=ending_block,
        avoid_block=avoid_block,
    )

    # Two shots at a *valid* script, not just a parseable one: attempt 2 is a
    # corrective re-ask that names what was wrong, since a bad hook or a
    # malformed payload is usually fixable when the model is told precisely
    # what failed. Capped at 2 because Gemini's free tier allows 20 calls/day
    # and the pipeline runs 4x/day - an unbounded retry loop could eat the
    # whole quota on one bad day.
    attempt_prompt = prompt
    last_error = None
    for attempt in range(1, 3):
        # Free tier as of mid-2026: Flash/Flash-Lite models are free, no card
        # needed. Avoid "-pro" model names, those require billing.
        response = gemini_utils.call_with_retry(
            lambda model: client.models.generate_content(
                model=model, contents=attempt_prompt),
            label=f"generate_script (attempt {attempt})",
        )
        text = (response.text or "").strip()
        # Strip accidental ```json fences if the model adds them anyway
        text = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()

        try:
            data = json.loads(text)
            # Repaired BEFORE validation, so a trimmable query never costs a
            # retry (the free tier allows 20 Gemini calls/day) and never costs
            # the slot. Anything trimming cannot fix still fails validation
            # below and still gets the corrective re-ask.
            trimmed = trim_long_visual_queries(data.get("segments") or [])
            if trimmed:
                resilience.record_degradation(
                    "visual-query-length",
                    f"{len(trimmed)} visual query/queries were longer than "
                    f"{MAX_VISUAL_QUERY_WORDS} words: " + "; ".join(trimmed),
                    "trimmed to the leading words rather than losing the slot",
                )
            # Same repair-before-validation rule, for the other length check
            # that was still costing whole slots. See trim_long_topic_subject.
            trimmed_subject = trim_long_topic_subject(data)
            if trimmed_subject:
                resilience.record_degradation(
                    "topic-subject-length",
                    f"topic_subject was longer than {MAX_TOPIC_SUBJECT_WORDS} "
                    f"words: {trimmed_subject}",
                    "trimmed to the leading words rather than losing the slot",
                )
            problems = validate_script(data)
        except json.JSONDecodeError as e:
            data, problems = None, [f"The response was not valid JSON ({e})."]

        # The rejection half of B2: the prompt asked for a new subject, this
        # checks whether it got one. Kept apart from the structural problems
        # because the two failures are not equally serious — see below.
        repeat = None
        if data and not problems and history.detection_active():
            pub_subs = history.published_subjects()
            if pub_subs:
                repeat = history.is_duplicate_subject(
                    data.get("topic_subject", ""), pub_subs)
                if repeat:
                    problems.append(
                        f"topic_subject '{data.get('topic_subject')}' is the same "
                        f"subject as the already-published '{repeat}'. Pick a "
                        "completely different subject — not another angle on this one."
                    )

        if not problems:
            if attempt > 1:
                print(f"      script valid on attempt {attempt}")
            chosen = (data.get("hook_choice") or {}).get("chosen_index")
            print(f"      hook: {_first_sentence(data['segments'][0]['narration'])!r} "
                  f"(candidate {chosen} of {len(data.get('hook_candidates') or [])})")
            return data

        last_error = problems
        print(f"      script attempt {attempt} rejected: {'; '.join(problems)}")
        # A duplicate subject on the LAST attempt degrades rather than raises.
        # Duplicate detection is a heuristic on a 1-4 word string; a false
        # positive that raises here silently halts the channel for that slot,
        # which this project has already decided is the worse failure (see
        # .github/workflows/tests.yml on why the suite is not an upload gate).
        # A duplicate that slips through costs 1,600 quota units and a repeat;
        # a wrongly-blocked run costs the slot AND leaves nothing to show for
        # it. So the repeat is published, loudly, on the dashboard.
        if repeat and attempt == 2:
            resilience.record_degradation(
                "script-duplicate-topic",
                f"both attempts returned '{data.get('topic_subject')}', the same "
                f"subject as the already-published '{repeat}'",
                "published the repeat rather than losing the slot — flagged here instead",
            )
            print(f"      WARNING: publishing a repeat of {repeat!r} — "
                  "two attempts both returned it")
            return data
        attempt_prompt = (
            prompt
            + "\n\nYour previous attempt was REJECTED for these specific reasons:\n"
            + "\n".join(f"- {p}" for p in problems)
            + "\nFix every one of them. Return the complete corrected JSON only."
        )

    raise RuntimeError(
        "Script generation failed validation twice: " + "; ".join(last_error or [])
    )


if __name__ == "__main__":
    script = generate_script()
    out_path = f"{config.WORKDIR}/script.json"
    with open(out_path, "w") as f:
        json.dump(script, f, indent=2)
    print(f"Script written to {out_path}")
