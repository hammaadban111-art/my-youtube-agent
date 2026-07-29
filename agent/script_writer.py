"""
Generates a video script + per-segment visual keywords using the free
Gemini API tier. Output is strict JSON so the rest of the pipeline can
consume it without any manual step.
"""
import json
import re
from google import genai
from . import config, history, predict


PROMPT_TEMPLATE = """You are writing a narration script for a short faceless YouTube
video about: {niche}

This is a STORY, not a list of trivia. Write {segments} segments, meant to be
read aloud in about {length} seconds total (~150 words/min), that build a
single narrative arc. Every video is short, so every line has to earn its
place — no padding, no throat-clearing.

- Segment 1 is the HOOK: open with a strong, specific, concrete detail (a name,
  place, date, or image) that immediately raises a question the viewer needs
  answered. Never open with something generic like "Did you know..." or
  "Here's a bizarre fact."
- The middle segments ESCALATE, and about halfway through, drop in a sharp
  rhetorical question or a provocative one-line statement that re-hooks
  attention — a second jolt, not just a recap. Each segment should deepen the
  mystery or raise the stakes from the one before it, the way a story builds —
  not a series of disconnected facts that could be reordered without losing
  anything. Reference or build on what was just said.
- The final segment is a genuinely DRAMATIC closing beat: a twist, a chilling
  final image, or a question left deliberately unresolved. Do not just trail
  off or summarize — land the ending.

Writing style — this is the part that matters most:
- Short, punchy sentences. Vary sentence length for rhythm, but bias hard
  toward short.
- Concrete, vivid, specific imagery — a rusted hinge, a name carved in wood,
  a light that shouldn't be on — never vague or generic description.
- Strong, sharp word choice. Cut every word that isn't pulling weight.
- Eerie, gripping, cinematic tone — like a late-night storyteller, not a dry
  encyclopedia entry. Avoid filler like "In this video" or "let's dive in."
{avoid_block}
Return ONLY valid JSON, no markdown fences, in this exact shape:
{{
  "title": "clickable YouTube title, under 70 chars",
  "description": "2-3 sentence YouTube description with 3 relevant hashtags",
  "topic_subject": "the real-world event/case this is about, as it would be
    titled in an encyclopedia (e.g. 'Lead masks case') — used to fact-check
    the script, so name the actual subject, not a dramatised phrasing",
  "factual_claims": [
    "each concrete, checkable factual assertion the narration makes (dates,
     names, places, numbers, outcomes) as a short standalone sentence"
  ],
  "segments": [
    {{"narration": "text to be spoken for this segment",
      "visual_keywords": "2-4 words for a GENERIC, common stock-footage scene —
        see rule below"}}
  ]
}}

visual_keywords rule: stock footage libraries do not have literal shots of
specific narrative props (a particular mask, a particular notebook). Describe
a generic, commonly-filmed scene or mood that evokes the moment instead —
think "what B-roll actually exists" (fog over hills, old photographs, empty
courtroom, stormy ocean, candle in dark room) rather than the exact object in
the sentence (avoid things like "lead masks" or "evidence locker with masks").
"""


def generate_script() -> dict:
    client = genai.Client(api_key=config.GEMINI_API_KEY)

    recent_titles = history.load_recent_titles()
    avoid_block = ""
    if recent_titles:
        bullets = "\n".join(f"- {t}" for t in recent_titles)
        avoid_block = (
            "\nDo NOT repeat these topics/angles — they were already covered "
            f"in recent videos:\n{bullets}\nPick a genuinely different story.\n"
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

    prompt = PROMPT_TEMPLATE.format(
        niche=config.NICHE,
        segments=config.NUM_SCRIPT_SEGMENTS,
        length=config.VIDEO_LENGTH_SECONDS,
        avoid_block=avoid_block,
    )
    # Free tier as of mid-2026: Flash/Flash-Lite models are free, no card needed.
    # Avoid "-pro" model names, those require billing.
    response = client.models.generate_content(model="gemini-flash-latest", contents=prompt)
    text = response.text.strip()

    # Strip accidental ```json fences if the model adds them anyway
    text = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()

    data = json.loads(text)
    return data


if __name__ == "__main__":
    script = generate_script()
    out_path = f"{config.WORKDIR}/script.json"
    with open(out_path, "w") as f:
        json.dump(script, f, indent=2)
    print(f"Script written to {out_path}")
