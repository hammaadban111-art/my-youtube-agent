#!/usr/bin/env python3
"""
Turns a list of drafted stories into the canonical weekly story packet.

The weekly Claude task does the part that needs judgment — research, angle,
script, sources, fact-check. This does the part that must not be done by hand:
working out which slot each story belongs to, minting stable ids, carrying
forward the stories a previous packet planned and nothing has published yet,
and refusing to write the file at all if the result would not pass
agent/packet.validate_packet.

Getting the slot arithmetic wrong is the failure that matters. A packet with a
gap in it is a scheduled run with nothing to publish, and a packet that
re-uses a slot another packet already filled is a story silently dropped. Both
are arithmetic, so both are done here rather than in prose.

INPUT  a JSON array of drafted stories, in publishing order. Each one carries
       everything except the slot: topic_subject, title, description,
       hook_candidates, hook_choice, factual_claims, segments, research,
       thumbnail_prompt, metadata.
OUTPUT content/weekly_story_packet.json (canonical, machine-read by the
       pipeline) and content/weekly_story_packet.md (the same thing a human
       can skim).

Usage:
  python scripts/assemble_packet.py drafts.json --packet-id 2026-W37
  python scripts/assemble_packet.py drafts.json --packet-id 2026-W38 \
      --start-after 2026-09-09T15:15:00Z --count 28
"""
import argparse
import json
import os
import re
import sys
import unicodedata
from datetime import datetime, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from agent import cadence, config, history, packet  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
MARKDOWN_PATH = os.path.join(ROOT, "content", "weekly_story_packet.md")


def slug(text: str) -> str:
    """A short, stable, ASCII handle for a subject, for the story id.

    Accent-folded rather than stripped: "Göbekli Tepe" has to survive as
    "gobekli-tepe" and not as "gbekli-tepe", because the id is what the ledger
    is keyed on forever."""
    folded = unicodedata.normalize("NFKD", text or "")
    ascii_only = folded.encode("ascii", "ignore").decode("ascii")
    words = re.findall(r"[A-Za-z0-9]+", ascii_only.lower())
    return "-".join(words[:3]) or "story"


def carry_forward(existing_path: str, window: list[datetime]) -> dict:
    """Stories a previous packet planned for slots inside the NEW window that
    nothing has published, keyed by slot id.

    A packet is a plan, not a promise about who wrote it. When a fresh week
    overlaps the tail of the previous one — which it does every time, because
    the week is generated mid-week — the stories already researched for those
    slots are kept exactly as they are. Re-researching them would waste the
    work and, worse, would give the same slot two different stories depending
    on which file won."""
    if not os.path.exists(existing_path):
        return {}
    try:
        with open(existing_path) as f:
            previous = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    wanted = {cadence.slot_id(s) for s in window}
    kept = {}
    for story in packet.stories(previous):
        slot = (story.get("slot") or {}).get("utc")
        if slot in wanted and packet.status_of(story) in packet.SELECTABLE:
            kept[slot] = story
    return kept


def stamp(story: dict, slot: datetime, packet_id: str) -> dict:
    stamped = dict(story)
    stamped["story_id"] = f"st-{cadence.slot_id(slot)}-{slug(story.get('topic_subject', ''))}"
    stamped["packet_id"] = packet_id
    stamped["status"] = "proposed"
    stamped["slot"] = {
        "utc": cadence.slot_id(slot),
        "iso": slot.isoformat().replace("+00:00", "Z"),
        "local": cadence.describe(slot),
    }
    return stamped


def build(drafts: list, *, packet_id: str, start_after: datetime,
          count: int, existing_path: str) -> dict:
    window = cadence.next_slots(start_after, count)
    inherited = carry_forward(existing_path, window)

    # Drafts fill the slots nothing has already claimed, in order.
    free = [s for s in window if cadence.slot_id(s) not in inherited]
    if len(drafts) < len(free):
        raise SystemExit(
            f"{len(free)} slot(s) in this window need a story and only "
            f"{len(drafts)} draft(s) were supplied. Write "
            f"{len(free) - len(drafts)} more; a packet with a gap is a "
            f"scheduled run with nothing to publish.")
    if len(drafts) > len(free):
        raise SystemExit(
            f"{len(drafts)} drafts were supplied but only {len(free)} slot(s) "
            f"in this window are free ({len(inherited)} are already planned "
            f"and unpublished). Trim the drafts rather than overwriting work.")

    stories = []
    for slot in window:
        key = cadence.slot_id(slot)
        if key in inherited:
            stories.append(inherited[key])
        else:
            stories.append(stamp(drafts.pop(0), slot, packet_id))

    return {
        "schema_version": packet.SCHEMA_VERSION,
        "packet_id": packet_id,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generated_by": os.getenv("PACKET_GENERATED_BY", "claude-opus-5"),
        "niche": config.NICHE,
        "cadence": {
            "slots_per_day": cadence.SLOTS_PER_DAY,
            "slot_times_utc": [f"{h:02d}:{cadence.SLOT_MINUTE_UTC:02d}Z"
                               for h in cadence.SLOT_HOURS_UTC],
            "slot_times_local": [cadence.local(s).strftime("%H:%M IST")
                                 for s in window[:cadence.SLOTS_PER_DAY]],
            "timezone": "Asia/Kolkata",
        },
        "window": {
            "first_slot": cadence.slot_id(window[0]),
            "last_slot": cadence.slot_id(window[-1]),
            "first_slot_local": cadence.describe(window[0]),
            "last_slot_local": cadence.describe(window[-1]),
            "slot_count": len(window),
        },
        "carried_forward": sorted(inherited),
        "stories": stories,
    }


def markdown(built: dict) -> str:
    lines = [
        f"# Weekly story packet `{built['packet_id']}`",
        "",
        f"- Generated {built['generated_at']} by `{built['generated_by']}`",
        f"- Niche: {built['niche']}",
        f"- {built['window']['slot_count']} slots, "
        f"{built['window']['first_slot_local']} through "
        f"{built['window']['last_slot_local']}",
        f"- Cadence: {built['cadence']['slots_per_day']} uploads/day at "
        + ", ".join(built["cadence"]["slot_times_local"]),
        "",
        "The JSON alongside this file is what the pipeline actually reads; "
        "this is the readable copy.",
        "",
    ]
    for story in built["stories"]:
        research = story.get("research") or {}
        lines += [
            f"## {story['slot']['local']} — {story['topic_subject']}",
            "",
            f"**{story['title']}**  ",
            f"`{story['story_id']}` · status `{story['status']}`",
            "",
            research.get("summary", ""),
            "",
            "Narration:",
            "",
        ]
        for i, seg in enumerate(story.get("segments", [])):
            lines.append(f"{i + 1}. {seg['narration']}  ")
            lines.append(f"   *footage:* `{seg['visual_query']}` "
                         f"(fallback `{seg['visual_fallback']}`)")
        sources = research.get("sources") or []
        if sources:
            lines += ["", "Sources:", ""]
            lines += [f"- [{s.get('title','source')}]({s.get('url','')})" for s in sources]
        lines += ["", f"Thumbnail: {story.get('thumbnail_prompt','')}", ""]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("drafts", help="JSON array of drafted stories, in order")
    parser.add_argument("--packet-id", required=True)
    parser.add_argument("--start-after",
                        help="ISO-8601 UTC; slots start at the first one AFTER "
                             "this. Defaults to now.")
    parser.add_argument("--count", type=int, default=cadence.SLOTS_PER_WEEK,
                        help=f"slots to fill (default {cadence.SLOTS_PER_WEEK}, "
                             "one full week)")
    parser.add_argument("--out", default=packet.PACKET_PATH)
    args = parser.parse_args()

    with open(args.drafts) as f:
        drafts = json.load(f)
    if not isinstance(drafts, list):
        raise SystemExit("The drafts file must be a JSON array of stories.")

    start_after = (datetime.strptime(args.start_after, "%Y-%m-%dT%H:%M:%SZ")
                   .replace(tzinfo=timezone.utc)
                   if args.start_after else datetime.now(timezone.utc))

    built = build(drafts, packet_id=args.packet_id, start_after=start_after,
                  count=args.count, existing_path=args.out)

    prior = sorted(set(history.published_subjects())
                   | set(packet.published_story_subjects()))
    problems = packet.validate_packet(built, published_subjects=prior)
    if problems:
        print(f"REFUSING to write the packet: {len(problems)} problem(s)")
        for p in problems:
            print(f"  - {p}")
        return 1

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(built, f, indent=2, ensure_ascii=False)
        f.write("\n")
    with open(MARKDOWN_PATH, "w") as f:
        f.write(markdown(built))

    print(f"Wrote {os.path.relpath(args.out, ROOT)}: {len(built['stories'])} "
          f"stories, {built['window']['first_slot_local']} through "
          f"{built['window']['last_slot_local']} "
          f"({len(built['carried_forward'])} carried forward).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
