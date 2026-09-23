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

from agent import cadence, config, editorial, history, packet  # noqa: E402

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


# How many stories whose slot has already PASSED unpublished are carried into
# the next packet. One day of slots: enough to absorb ordinary scheduler lag and
# a missed run or two, while a queue further behind than that is a problem for a
# human rather than something to hide by carrying an ever-growing backlog.
MAX_OVERDUE_CARRY = cadence.SLOTS_PER_DAY


def carry_overdue(existing_path: str, window: list[datetime],
                  taken_subjects: list[str], taken_titles: set[str]) -> list[dict]:
    """Stories from the previous packet whose slot is BEFORE the new window and
    that are still waiting to publish, oldest first, at most MAX_OVERDUE_CARRY.

    WHY. The queue drains FIFO and GitHub starts runs hours late, so a packet is
    usually a slot or two behind when its replacement is written. Those stories
    used to vanish with the old packet: 2026-W39 skipped six fully researched
    ones ("falls outside the window so the new packet cannot carry it"). Kept
    here with their original slots, they are simply the first ones the next
    runs publish.

    A carried story whose subject or title a new draft now uses is dropped
    instead — the new draft is the newer decision, and keeping both would fail
    the packet's own duplicate check and refuse to write the week at all."""
    if not window or not os.path.exists(existing_path):
        return []
    try:
        with open(existing_path) as f:
            previous = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    first = cadence.slot_id(window[0])
    overdue = []
    for story in packet.stories(previous):
        slot = str((story.get("slot") or {}).get("utc") or "")
        if not slot or slot >= first:
            continue
        if packet.status_of(story) not in packet.SELECTABLE:
            continue
        entry = packet.ledger_entry(str(story.get("story_id") or ""))
        if entry.get("attempts", 0) >= packet.MAX_ATTEMPTS:
            continue
        subject = str(story.get("topic_subject") or "")
        title = str(story.get("title") or "").strip().lower()
        clash = history.is_duplicate_subject(subject, taken_subjects)
        if clash or title in taken_titles:
            print(f"  not carrying overdue {story.get('story_id')}: a new story "
                  f"now covers {clash or story.get('title')!r}")
            continue
        overdue.append(story)
    overdue.sort(key=lambda s: s["slot"]["utc"])
    if len(overdue) > MAX_OVERDUE_CARRY:
        dropped = overdue[:-MAX_OVERDUE_CARRY]
        print(f"  WARNING: {len(overdue)} stories are overdue; carrying the newest "
              f"{MAX_OVERDUE_CARRY}. Not carried: "
              + ", ".join(str(s.get("story_id")) for s in dropped))
        overdue = overdue[-MAX_OVERDUE_CARRY:]
    return overdue


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
          count: int, existing_path: str,
          editorial_brief: dict | None = None) -> dict:
    window = cadence.next_slots(start_after, count)
    inherited = carry_forward(existing_path, window)

    if editorial_brief:
        missing_rationale = [
            str(draft.get("topic_subject") or "untitled draft")
            for draft in drafts
            if len(str(draft.get("editorial_rationale") or "").strip())
            < packet.MIN_EDITORIAL_RATIONALE_CHARS
        ]
        if missing_rationale:
            names = ", ".join(repr(name) for name in missing_rationale[:3])
            remainder = "" if len(missing_rationale) <= 3 else ", ..."
            raise SystemExit(
                "Every newly drafted story needs an editorial_rationale of at "
                f"least {packet.MIN_EDITORIAL_RATIONALE_CHARS} characters. "
                f"Missing: {names}{remainder}")

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

    overdue = carry_overdue(
        existing_path, window,
        [str(s.get("topic_subject") or "") for s in stories],
        {str(s.get("title") or "").strip().lower() for s in stories})
    stories = overdue + stories

    built = {
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
        # Stories whose slot had already passed unpublished; they sit before
        # the window and are published first. See carry_overdue().
        "carried_overdue": [s["slot"]["utc"] for s in overdue],
        "stories": stories,
    }
    if editorial_brief:
        # Keep only provenance here. The full brief stays a separately readable
        # source file; this proves which evidence the Cowork session had when it
        # drafted this packet without bloating every workflow read.
        built["editorial_brief"] = dict(editorial_brief)
    return built


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
    brief = built.get("editorial_brief") or {}
    if brief:
        lines[3:3] = [
            f"- Editorial brief: `{brief.get('path', '?')}` "
            f"({brief.get('usable_records', '?')} usable records; "
            f"SHA `{str(brief.get('sha256', ''))[:12]}`)",
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
        ]
        rationale = str(story.get("editorial_rationale") or "").strip()
        if rationale:
            lines += ["Editorial rationale:", "", rationale, ""]
        lines += ["Narration:", ""]
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
    parser.add_argument(
        "--editorial-brief", default=str(editorial.BRIEF_JSON_PATH),
        help="fresh weekly feedback JSON; required for a new packet",
    )
    args = parser.parse_args()

    with open(args.drafts) as f:
        drafts = json.load(f)
    if not isinstance(drafts, list):
        raise SystemExit("The drafts file must be a JSON array of stories.")

    start_after = (datetime.strptime(args.start_after, "%Y-%m-%dT%H:%M:%SZ")
                   .replace(tzinfo=timezone.utc)
                   if args.start_after else datetime.now(timezone.utc))

    try:
        brief = editorial.brief_provenance(args.editorial_brief)
    except editorial.EditorialBriefError as exc:
        raise SystemExit(f"Cannot assemble packet: {exc}") from exc

    built = build(drafts, packet_id=args.packet_id, start_after=start_after,
                  count=args.count, existing_path=args.out,
                  editorial_brief=brief)

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
          f"({len(built['carried_forward'])} carried forward, "
          f"{len(built['carried_overdue'])} overdue carried in front).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
