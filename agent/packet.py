"""
The weekly story packet: where scripts come from now.

WHAT REPLACED WHAT

Until 2026-09-08 every scheduled slot generated its own story at run time, by
calling Gemini from inside GitHub Actions. That is gone. A Claude Cowork task
now researches and writes a full week of stories ahead of time and commits them
to this repository as `content/weekly_story_packet.json`; a scheduled run reads
one story out of that file and renders it.

WHY

The generate-at-run-time design failed in the one way that costs a slot
outright: `503 UNAVAILABLE  This model is currently experiencing high demand`.
Twenty-eight scheduled runs died on it between 2026-08-24 and 2026-09-04, some
after burning a nine-minute retry ladder and both fallback models, and every
one of them published nothing. A model outage at 06:07 UTC cannot be retried
into success, and there is no second source of a story at that moment.

Writing the week in advance removes the dependency from the critical path
entirely. By the time a slot fires, its story has been researched, written,
fact-checked and committed for up to a week. The scheduled run does rendering,
uploading and bookkeeping — work that is either local or against YouTube, and
nothing else.

THERE IS NO FALLBACK GENERATOR, ON PURPOSE

If the packet is missing, malformed, or has no story for the slot, the run
fails loudly and publishes nothing. That is the correct outcome: the
alternative — inventing a story inside the workflow — is what this change
exists to remove, and a silently-invented story is exactly the thing nobody
would notice until it was on the channel.

TWO FILES, DIFFERENT JOBS

  content/weekly_story_packet.json  the PLAN. Written by the weekly Claude
      task, read by the pipeline, never rewritten by a scheduled run.
  content/story_history.json        the LEDGER. Append-only status for every
      story that has ever been planned: proposed -> queued -> published, or
      -> failed. Written by the pipeline, read by the next weekly task so it
      never re-proposes a story that already went out, and read here so a
      story can never be published twice.

Keeping them apart is deliberate. Four workflows commit to this repository and
two of them can overlap; a single file that both the planner and the publisher
rewrote would conflict on every run. The ledger is a dict keyed by story id,
which merges by union (scripts/resolve_data_conflicts.py) — the packet is
rewritten once a week by one writer and conflicts with nothing.
"""
import json
import os
import re
import sys
from datetime import datetime, timezone

from . import cadence, config, history, script_writer

ROOT = os.path.join(os.path.dirname(__file__), "..")
PACKET_PATH = os.path.join(ROOT, "content", "weekly_story_packet.json")
LEDGER_PATH = os.path.join(ROOT, "content", "story_history.json")
SCHEMA_VERSION = 1

# proposed  written by the weekly task, not yet used
# queued    a run has claimed it and is rendering it right now
# published it is on the channel; never selected again
# failed    it was claimed MAX_ATTEMPTS times without reaching the channel
# skipped   deliberately retired by a human or by the weekly task
STATUSES = ("proposed", "queued", "published", "failed", "skipped")
SELECTABLE = ("proposed", "queued")

# A story that has been claimed this many times without ever reaching YouTube
# is not a transient failure any more. Retrying it forever would wedge the
# queue behind one bad entry, so it is retired and the next story runs.
MAX_ATTEMPTS = 3

# How far behind its own slot a story may be published without comment. Beyond
# this the run still publishes — a researched story does not spoil, and losing
# it would be worse — but the lateness is recorded as a degradation so the
# dashboard shows the queue is running behind.
LATE_AFTER_HOURS = 26

# Lets a MANUAL run publish the next pending story without waiting for its
# slot. Scheduled runs never set it: a scheduled run outside every slot is a
# scheduler fault worth failing on, and letting one run early would drain the
# week ahead of schedule. A manual dispatch is the opposite case — somebody is
# recovering a slot that already went by, or checking the pipeline end to end,
# and refusing them the next story leaves no way to do either.
#
# Requires an explicit value rather than mere presence, the same rule
# agent/velocity.py's override uses, so a stray empty variable cannot turn it
# on by accident.
EARLY_CLAIM_ENV = "PACKET_ALLOW_EARLY"

# YouTube's own limits, checked here rather than discovered at upload time.
MAX_TITLE_CHARS = 100
MAX_DESCRIPTION_CHARS = 5000
MIN_TAGS = 3

MIN_SOURCES = 1
VERDICTS = ("SUPPORTED", "SILENT", "CONTRADICTED", "MISLEADING")
MIN_EDITORIAL_RATIONALE_CHARS = 20
EDITORIAL_BRIEF_COUNT_FIELDS = (
    "usable_records",
    "hook_retention_records",
    "hook_evidence_records",
)


class PacketError(RuntimeError):
    """Anything that means this run has no story it may publish.

    Raised, never swallowed: the whole point of the packet is that a missing
    or broken story stops the run instead of being papered over."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- the packet

def load_packet(path: str = None) -> dict:
    """Reads the packet, or raises PacketError saying exactly what is wrong.

    Every failure here ends the run, so every message has to tell a
    non-engineer what to do about it."""
    path = path or PACKET_PATH
    if not os.path.exists(path):
        raise PacketError(
            f"No story packet at {os.path.relpath(path, ROOT)}. The weekly "
            "Claude task writes it; run that task (or its GitHub Action) "
            "before this pipeline can publish anything. Nothing was generated "
            "here on purpose — this pipeline no longer writes its own stories.")
    try:
        with open(path) as f:
            packet = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise PacketError(
            f"The story packet at {os.path.relpath(path, ROOT)} could not be "
            f"read ({type(e).__name__}: {e}). It has to be repaired or "
            "regenerated; this run will not invent a story instead.") from e
    if not isinstance(packet, dict):
        raise PacketError("The story packet is not a JSON object.")
    if packet.get("schema_version") != SCHEMA_VERSION:
        raise PacketError(
            f"The story packet is schema_version {packet.get('schema_version')!r}, "
            f"but this code reads version {SCHEMA_VERSION}.")
    return packet


def stories(packet: dict) -> list[dict]:
    return [s for s in (packet.get("stories") or []) if isinstance(s, dict)]


def _slot_dt(story: dict) -> datetime | None:
    raw = ((story.get("slot") or {}).get("utc") or "").strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%dT%H%MZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _is_nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _validate_editorial_brief_provenance(brief: object) -> list[str]:
    """Validate compact provenance copied from the generated feedback brief."""
    if not isinstance(brief, dict):
        return ["packet.editorial_brief is not an object."]

    problems = []
    for field in ("path", "generated_at"):
        if not isinstance(brief.get(field), str) or not brief[field].strip():
            problems.append(f"packet.editorial_brief.{field} is missing or empty.")
    sha256 = brief.get("sha256")
    if not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", sha256):
        problems.append("packet.editorial_brief.sha256 is not a SHA-256 digest.")
    for field in EDITORIAL_BRIEF_COUNT_FIELDS:
        if not _is_nonnegative_int(brief.get(field)):
            problems.append(
                f"packet.editorial_brief.{field} is not a non-negative integer.")
    if not isinstance(brief.get("hook_evidence_sufficient"), bool):
        problems.append(
            "packet.editorial_brief.hook_evidence_sufficient is not a boolean.")
    return problems


def validate_packet(packet: dict, *, published_subjects: list[str] = None) -> list[str]:
    """Every problem with the packet, as plain sentences. Empty means valid.

    This is the gate the workflow runs BEFORE the pipeline touches anything, so
    a bad packet is caught while it is still only a file — not halfway through
    a render. It re-uses agent/script_writer.py's validators so a packet story
    has to clear exactly the same bar a generated script always had to."""
    problems = []

    for field in ("packet_id", "generated_at", "generated_by", "niche"):
        if not str(packet.get(field) or "").strip():
            problems.append(f"packet.{field} is missing or empty.")

    editorial_brief = packet.get("editorial_brief")
    has_editorial_brief = editorial_brief is not None
    if has_editorial_brief:
        problems.extend(_validate_editorial_brief_provenance(editorial_brief))
    current_packet_id = str(packet.get("packet_id") or "").strip()

    entries = stories(packet)
    if not entries:
        problems.append("The packet contains no stories.")
        return problems

    declared = (packet.get("window") or {}).get("slot_count")
    if declared is not None and declared != len(entries):
        problems.append(
            f"window.slot_count says {declared} but the packet holds "
            f"{len(entries)} stories.")

    seen_ids, seen_slots, seen_subjects, seen_titles = set(), set(), {}, set()
    prior = list(published_subjects or [])

    for i, story in enumerate(entries):
        where = f"story {i}"
        story_id = str(story.get("story_id") or "").strip()
        if not story_id:
            problems.append(f"{where} has no story_id.")
        else:
            where = f"story {story_id!r}"
            if story_id in seen_ids:
                problems.append(f"{where}: duplicate story_id.")
            seen_ids.add(story_id)

        # The LIVE status, not the packet's own word for it: the ledger is
        # written by the runs that actually happened, and it is what decides
        # whether the duplicate check below still applies to this story.
        status = status_of(story) if story_id else story.get("status")
        if story.get("status") not in STATUSES:
            problems.append(
                f"{where}: status {status!r} is not one of {', '.join(STATUSES)}.")

        # Only stories newly drafted for this packet owe an explanation. Kept
        # overlap stories were researched against the previous week's brief
        # and remain deliberately untouched.
        if (has_editorial_brief and current_packet_id
                and str(story.get("packet_id") or "").strip() == current_packet_id):
            rationale = str(story.get("editorial_rationale") or "").strip()
            if len(rationale) < MIN_EDITORIAL_RATIONALE_CHARS:
                problems.append(
                    f"{where}: editorial_rationale needs at least "
                    f"{MIN_EDITORIAL_RATIONALE_CHARS} characters for a newly drafted story.")

        slot = _slot_dt(story)
        if slot is None:
            problems.append(
                f"{where}: slot.utc is missing or not in YYYY-MM-DDTHHMMZ form.")
        else:
            key = cadence.slot_id(slot)
            if key in seen_slots:
                problems.append(f"{where}: two stories claim slot {key}.")
            seen_slots.add(key)
            if slot.hour not in cadence.SLOT_HOURS_UTC or slot.minute != cadence.SLOT_MINUTE_UTC:
                problems.append(
                    f"{where}: slot {key} is not one of the real publishing "
                    f"slots ({', '.join(f'{h:02d}:{cadence.SLOT_MINUTE_UTC:02d}Z' for h in cadence.SLOT_HOURS_UTC)}).")

        # The script contract — identical to the one a generated script had to
        # pass, so nothing downstream had to learn a second shape.
        for problem in script_writer.validate_script(story):
            problems.append(f"{where}: {problem}")

        segments = story.get("segments") or []
        if len(segments) != config.NUM_SCRIPT_SEGMENTS:
            problems.append(
                f"{where}: has {len(segments)} segments, the renderer expects "
                f"{config.NUM_SCRIPT_SEGMENTS}.")

        title = str(story.get("title") or "").strip()
        if not title:
            problems.append(f"{where}: title is missing.")
        elif len(title) > MAX_TITLE_CHARS:
            problems.append(
                f"{where}: title is {len(title)} characters, YouTube's limit "
                f"is {MAX_TITLE_CHARS}.")
        elif title.lower() in seen_titles:
            problems.append(f"{where}: title {title!r} is repeated in this packet.")
        seen_titles.add(title.lower())

        description = str(story.get("description") or "").strip()
        if not description:
            problems.append(f"{where}: description is missing.")
        elif len(description) > MAX_DESCRIPTION_CHARS:
            problems.append(
                f"{where}: description is {len(description)} characters, "
                f"YouTube's limit is {MAX_DESCRIPTION_CHARS}.")

        subject = str(story.get("topic_subject") or "").strip()
        if subject:
            # Against the rest of the packet...
            clash = history.is_duplicate_subject(subject, list(seen_subjects))
            if clash:
                problems.append(
                    f"{where}: subject {subject!r} repeats {clash!r}, already "
                    f"planned in this same packet (slot "
                    f"{seen_subjects.get(clash, '?')}).")
            # ...and, for stories that could still go out, against everything
            # the channel has ever published.
            #
            # Only for stories that could still go out — the ones due_stories()
            # can actually select. Everything else is settled:
            #
            #   published  necessarily matches a published subject, its OWN.
            #              Checking it reported the packet as broken from the
            #              moment its first video went live, which would fail
            #              daily.yml's pre-flight on every remaining slot in
            #              the week. Found exactly that way on 2026-09-07, by
            #              the first real upload off this packet.
            #   skipped    was skipped for being a duplicate in the first place.
            #   failed     is retired and will never be selected again.
            #
            # In each case the story is going nowhere, so a duplicate warning
            # about it is noise that breaks a gate protecting live slots.
            if status in SELECTABLE:
                published_clash = history.is_duplicate_subject(subject, prior)
                if published_clash:
                    problems.append(
                        f"{where}: subject {subject!r} has already been "
                        f"published as {published_clash!r}.")
            seen_subjects[subject] = cadence.slot_id(slot) if slot else "?"

        problems.extend(f"{where}: {p}" for p in _validate_research(story))

        if not str(story.get("thumbnail_prompt") or "").strip():
            problems.append(f"{where}: thumbnail_prompt is missing.")

        tags = (story.get("metadata") or {}).get("tags") or []
        if not isinstance(tags, list) or len(tags) < MIN_TAGS:
            problems.append(
                f"{where}: metadata.tags needs at least {MIN_TAGS} entries.")

    return problems


def _validate_research(story: dict) -> list[str]:
    """The half of a story that a generated script never had: where it came
    from, and whether somebody checked it."""
    problems = []
    research = story.get("research")
    if not isinstance(research, dict):
        return ["research is missing."]

    if not str(research.get("summary") or "").strip():
        problems.append("research.summary is missing.")

    sources = research.get("sources") or []
    if not isinstance(sources, list) or len(sources) < MIN_SOURCES:
        problems.append(f"research.sources needs at least {MIN_SOURCES} entry.")
    else:
        for j, source in enumerate(sources):
            if not isinstance(source, dict):
                problems.append(f"research.sources[{j}] is not an object.")
                continue
            if not str(source.get("title") or "").strip():
                problems.append(f"research.sources[{j}] has no title.")
            url = str(source.get("url") or "").strip()
            if not url.startswith("http"):
                problems.append(f"research.sources[{j}] has no usable url.")

    claims = story.get("factual_claims") or []
    verification = research.get("verification")
    if not isinstance(verification, list):
        problems.append("research.verification is missing.")
        return problems

    checked = set()
    for j, entry in enumerate(verification):
        if not isinstance(entry, dict):
            problems.append(f"research.verification[{j}] is not an object.")
            continue
        verdict = entry.get("verdict")
        if verdict not in VERDICTS:
            problems.append(
                f"research.verification[{j}] verdict {verdict!r} is not one of "
                f"{', '.join(VERDICTS)}.")
        idx = entry.get("segment_index")
        try:
            checked.add(int(idx))
        except (TypeError, ValueError):
            problems.append(
                f"research.verification[{j}] segment_index {idx!r} is not an integer.")
        if verdict in ("CONTRADICTED", "MISLEADING") and not str(
                entry.get("correction") or "").strip():
            # The pipeline rewrites the narration from this field. A verdict
            # that says the line is wrong and offers nothing to put in its
            # place would ship the wrong line.
            problems.append(
                f"research.verification[{j}] is {verdict} but carries no correction.")

    claimed = set()
    for c in claims:
        try:
            claimed.add(int(c.get("segment_index")))
        except (TypeError, ValueError):
            continue
    missing = sorted(claimed - checked)
    if missing:
        problems.append(
            "research.verification does not cover the claims from segment(s) "
            + ", ".join(str(m) for m in missing) + ".")
    return problems


# ---------------------------------------------------------------- the ledger

def _load_ledger() -> dict:
    if not os.path.exists(LEDGER_PATH):
        return {"schema_version": SCHEMA_VERSION, "stories": {}}
    try:
        with open(LEDGER_PATH) as f:
            ledger = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        # A damaged ledger must not stop a publish, but it must not silently
        # read as "nothing has ever been published" either — that would
        # republish the whole week. Loud, and fatal.
        raise PacketError(
            f"content/story_history.json is unreadable ({type(e).__name__}: {e}). "
            "Repair it before publishing — treating it as empty would republish "
            "stories that have already gone out.") from e
    ledger.setdefault("stories", {})
    return ledger


def _save_ledger(ledger: dict) -> None:
    os.makedirs(os.path.dirname(LEDGER_PATH), exist_ok=True)
    tmp = LEDGER_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(ledger, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, LEDGER_PATH)


def ledger_entry(story_id: str) -> dict:
    return _load_ledger()["stories"].get(story_id, {})


def status_of(story: dict) -> str:
    """The live status of a story: the ledger's word if it has one, otherwise
    whatever the packet proposed. The ledger always wins — it is written by the
    runs that actually happened."""
    entry = ledger_entry(str(story.get("story_id") or ""))
    return entry.get("status") or story.get("status") or "proposed"


def record_status(story: dict, status: str, note: str = "", **fields) -> dict:
    """Moves one story to `status` and appends to its durable history."""
    if status not in STATUSES:
        raise ValueError(f"unknown status {status!r}")
    story_id = str(story.get("story_id") or "").strip()
    if not story_id:
        raise ValueError("cannot record a status for a story with no story_id")

    ledger = _load_ledger()
    entry = ledger["stories"].setdefault(story_id, {
        "story_id": story_id,
        "history": [],
        "attempts": 0,
    })
    entry.setdefault("history", [])
    entry.setdefault("attempts", 0)
    entry["title"] = story.get("title", entry.get("title", ""))
    entry["topic_subject"] = story.get("topic_subject", entry.get("topic_subject", ""))
    slot = _slot_dt(story)
    if slot:
        entry["slot"] = cadence.slot_id(slot)
    entry["status"] = status
    entry.update(fields)
    if status == "queued":
        entry["attempts"] += 1
    entry["history"].append({
        "at": _iso(_now()),
        "status": status,
        "note": note[:300],
        "attempt": entry["attempts"],
    })
    _save_ledger(ledger)
    return entry


def published_story_subjects() -> list[str]:
    """Subjects the ledger says have gone out. Unioned with the channel's own
    records by the weekly task, so a re-proposal is impossible from either
    direction."""
    return [e.get("topic_subject", "") for e in _load_ledger()["stories"].values()
            if e.get("status") == "published" and e.get("topic_subject")]


# ------------------------------------------------------------- slot selection

def early_claim_allowed() -> bool:
    return os.getenv(EARLY_CLAIM_ENV, "").strip().lower() in {"1", "true", "yes"}


def due_stories(packet: dict, now: datetime = None,
                allow_early: bool = None) -> list[dict]:
    """Every story that is eligible to publish right now, oldest slot first.

    A plain FIFO queue over slots that have arrived. It is deliberately NOT
    "the story whose slot exactly matches this run": a run whose slot failed
    would then drop that story forever, and GitHub's scheduler is late often
    enough that exact matching loses slots on its own. Draining in slot order
    means a failed slot's story is simply the next one published, the planned
    order is preserved, and nothing researched is ever thrown away."""
    now = now or _now()
    if allow_early is None:
        allow_early = early_claim_allowed()
    horizon = (float("inf") if allow_early
               else now.timestamp() + cadence.SLOT_EARLY_MINUTES * 60)
    ledger = _load_ledger()["stories"]

    eligible = []
    for story in stories(packet):
        slot = _slot_dt(story)
        if slot is None or slot.timestamp() > horizon:
            continue
        entry = ledger.get(str(story.get("story_id") or ""), {})
        status = entry.get("status") or story.get("status") or "proposed"
        if status not in SELECTABLE:
            continue
        if entry.get("attempts", 0) >= MAX_ATTEMPTS:
            continue
        eligible.append((slot, story))
    return [s for _, s in sorted(eligible, key=lambda pair: pair[0])]


def select_story(packet: dict, now: datetime = None,
                 allow_early: bool = None) -> dict:
    """The one story this run should publish, or PacketError explaining why
    there isn't one."""
    now = now or _now()
    ready = due_stories(packet, now, allow_early=allow_early)
    if ready:
        return ready[0]

    upcoming = [s for s in stories(packet) if (_slot_dt(s) or now) > now]
    if upcoming:
        nxt = min(_slot_dt(s) for s in upcoming if _slot_dt(s))
        raise PacketError(
            "No story is due yet. The packet's next slot is "
            f"{cadence.describe(nxt)}. Nothing was published — this run "
            "started outside every planned slot.")
    raise PacketError(
        "The story packet is exhausted: every story in it has already been "
        "published, failed or skipped, and it holds nothing for a slot from "
        "now on. The weekly Claude task has not delivered a fresh week. "
        "Nothing was published, and no story was invented to fill the gap.")


def lateness_hours(story: dict, now: datetime = None) -> float:
    slot = _slot_dt(story)
    if slot is None:
        return 0.0
    return max(0.0, ((now or _now()) - slot).total_seconds() / 3600)


# ------------------------------------------------------- what the pipeline gets

def to_script(story: dict) -> dict:
    """The script dict the render pipeline has always consumed, plus the
    provenance fields the packet adds.

    The shape is unchanged from what the old generator returned — segments with
    narration/visual_query/visual_fallback, hook candidates, factual claims —
    so tts.py, visuals.py, assemble.py, upload.py and store.py needed no
    changes at all. `verification` and `sources` ride along for
    agent/grounding.py, which now checks against them instead of asking a model
    to re-derive them."""
    research = story.get("research") or {}
    return {
        "title": story.get("title", ""),
        "description": story.get("description", ""),
        "topic_subject": story.get("topic_subject", ""),
        "hook_candidates": story.get("hook_candidates", []),
        "hook_choice": story.get("hook_choice", {}),
        "factual_claims": story.get("factual_claims", []),
        "segments": [dict(s) for s in story.get("segments", [])],
        "thumbnail_prompt": story.get("thumbnail_prompt", ""),
        "metadata": story.get("metadata", {}),
        # Provenance. Carried on the script so it survives the checkpoint and
        # reaches _record_and_finish on a resumed run.
        "story_id": story.get("story_id", ""),
        "packet_id": story.get("packet_id", ""),
        "slot": story.get("slot", {}),
        "sources": research.get("sources", []),
        "verification": research.get("verification", []),
        "research_summary": research.get("summary", ""),
    }


def claim_script(now: datetime = None, allow_early: bool = None) -> dict:
    """Load, select, mark queued, return the script. The pipeline's one entry
    point into the packet.

    The duplicate check runs HERE and not only in the workflow's validation
    step, because the two ask different questions. Validation asks "was this
    packet sound when it was written"; this asks "is it still sound now, after
    everything published since". A week-old packet can name a subject a
    later run has since covered — from an earlier packet, a re-upload, or a
    manual publish — and that is the one case a pre-flight check cannot see.
    A duplicate is skipped rather than published; unlike the old generator,
    skipping costs nothing, because the next story is already written."""
    now = now or _now()
    if allow_early is None:
        allow_early = early_claim_allowed()
    packet = load_packet()
    already = history.published_subjects()

    if allow_early:
        print("      [packet] manual run: claiming the next pending story "
              "without waiting for its slot")

    story = None
    for candidate in due_stories(packet, now, allow_early=allow_early):
        clash = history.is_duplicate_subject(
            candidate.get("topic_subject", ""), already)
        if clash:
            print(f"      skipping {candidate.get('story_id')}: its subject "
                  f"{candidate.get('topic_subject')!r} is already published "
                  f"as {clash!r}")
            record_status(candidate, "skipped",
                          note=f"subject already published as {clash!r}")
            continue
        story = candidate
        break

    if story is None:
        # select_story raises the right message for "nothing due" vs
        # "exhausted"; reaching here having skipped everything is its own case.
        select_story(packet, now, allow_early=allow_early)
        raise PacketError(
            "Every story that is due has already been published under another "
            "entry, so there is nothing left to publish for this slot.")

    story = dict(story)
    story.setdefault("packet_id", packet.get("packet_id", ""))

    late = lateness_hours(story, now)
    record_status(story, "queued",
                  note=f"claimed by a run at {_iso(now)}"
                       + (f", {late:.1f}h after its slot" if late else "")
                       + (" (manual early claim)" if allow_early else ""))
    slot = _slot_dt(story)
    print(f"      story {story.get('story_id')} for slot "
          f"{cadence.describe(slot) if slot else 'unknown'}: "
          f"{story.get('topic_subject')!r}")
    return to_script(story)


def mark_published(script: dict, video_id: str) -> None:
    """Called once the video is on the channel. Idempotent by story id, so the
    resume path can call it for a video an earlier run uploaded."""
    if not script.get("story_id"):
        return
    record_status(script, "published", note=f"published as {video_id}",
                  video_id=video_id, published_at=_iso(_now()))


def mark_failed(script: dict, reason: str) -> None:
    if not script.get("story_id"):
        return
    entry = ledger_entry(script["story_id"])
    if entry.get("status") == "published":
        return
    status = "failed" if entry.get("attempts", 0) >= MAX_ATTEMPTS else "proposed"
    record_status(script, status, note=reason[:300])


# ------------------------------------------------------------------ CLI gate

def _cli(argv: list[str]) -> int:
    """`python -m agent.packet --validate [--require-slot]`

    The workflow's pre-flight. Exits non-zero with the reasons printed, so a
    bad packet stops the job before anything is rendered.

    --require-slot also fails when nothing is due, which is what a SCHEDULED
    run wants. --allow-early (or PACKET_ALLOW_EARLY=1) instead treats the next
    pending story as due, which is what a manual recovery run wants."""
    require_slot = "--require-slot" in argv
    allow_early = "--allow-early" in argv or early_claim_allowed()
    try:
        packet = load_packet()
    except PacketError as e:
        print(f"::error::{e}")
        return 1

    prior = sorted(set(history.published_subjects()) | set(published_story_subjects()))
    problems = validate_packet(packet, published_subjects=prior)
    if problems:
        print(f"::error::The story packet has {len(problems)} problem(s):")
        for p in problems:
            print(f"  - {p}")
        return 1

    ready = due_stories(packet, allow_early=allow_early)
    remaining = [s for s in stories(packet) if status_of(s) in SELECTABLE]
    print(f"Story packet {packet.get('packet_id')} is valid: "
          f"{len(stories(packet))} stories, {len(remaining)} still unpublished, "
          f"{len(ready)} due now.")

    out = os.getenv("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(f"due={'true' if ready else 'false'}\n")
            f.write(f"remaining={len(remaining)}\n")
            f.write(f"packet_id={packet.get('packet_id')}\n")

    if require_slot and not ready:
        # "Nothing due" is not a failure by itself. Selection is FIFO over
        # slots that have arrived, so it can only mean every story planned up
        # to now has already gone out — the queue is AHEAD of the clock, not
        # broken. That happens after a manual catch-up run, and it happened for
        # real on 2026-09-07 when GitHub fired a scheduled run 3h40m late, for
        # a slot another run had already served. Failing those emails the owner
        # an alert about a channel that is perfectly healthy.
        #
        # An EXHAUSTED packet is the real failure, and it has its own message:
        # nothing planned is left at all, so every slot from here publishes
        # nothing until a fresh week is delivered.
        if remaining:
            print("Nothing to publish for this run: every story planned up to "
                  f"now has already gone out, and {len(remaining)} remain for "
                  "slots still ahead. The queue is ahead of the clock.")
            return 0
        print("::error::The story packet is exhausted — no story remains for "
              "this run or any run after it. Deliver a fresh week.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
