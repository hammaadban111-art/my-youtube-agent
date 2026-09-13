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
from datetime import datetime, timedelta, timezone

from . import cadence, config, history, notify, script_writer

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

# How little planned content is left before it is an emergency rather than a
# statistic. The weekly Claude task delivers a fresh packet every Wednesday; if
# it does not, the channel keeps publishing until the last planned slot and then
# stops dead. That is exactly what happened between 2026-08-24 and 2026-09-01 —
# eight days, zero uploads — and nothing said so until the Friday health check,
# by which point the channel had already been silent for days.
#
# 48 hours is two full publishing days: enough to notice an email, sit down and
# produce a week, without the alert firing every single day of a normal week.
#
# It is a FLOOR, not the whole rule — see runway_deficit_hours(). A fixed
# threshold cannot see the real question, which is not "is there much left" but
# "will it last until the next packet is actually written".
RUNWAY_ALERT_HOURS = 48.0

# When the next week's packet gets written. The editorial brief refreshes at
# Wednesday 14:47 UTC (.github/workflows/editorial-brief.yml) and the Claude
# Cowork session that writes the packet follows it at 20:45 IST / 15:15 UTC.
PACKET_WRITE_WEEKDAY = 2      # Monday=0, so 2 is Wednesday
PACKET_WRITE_HOUR_UTC = 15
PACKET_WRITE_MINUTE_UTC = 15

# Slack demanded on top of simply reaching the next write — deliberately ZERO,
# and that is a finding rather than an oversight.
#
# A seven-day packet on a seven-day write cycle has no designed slack at all. A
# packet written Wednesday 15:15 UTC covering seven days ends at the following
# Wednesday 16:07, fifty-two minutes after its replacement is due. Demanding any
# meaningful margin on top of that would fire the alert every single week on a
# perfectly healthy schedule, and an alert that cries wolf weekly is worse than
# no alert — this was caught by test_a_packet_written_on_its_proper_day_has_slack
# before it ever reached the inbox.
#
# So this alert answers only the unambiguous question: is there a REAL hole? The
# deeper fix for the missing slack is a packet that covers more than seven days,
# which changes cadence.SLOTS_PER_WEEK and the validator's slot-run check with
# it — a larger change than an alert, and not one to make silently.
PACKET_WRITE_MARGIN_HOURS = 0.0


def next_packet_write(now: datetime = None) -> datetime:
    """When the next weekly packet is due to be written."""
    now = now or _now()
    candidate = now.replace(hour=PACKET_WRITE_HOUR_UTC,
                            minute=PACKET_WRITE_MINUTE_UTC,
                            second=0, microsecond=0)
    days_ahead = (PACKET_WRITE_WEEKDAY - now.weekday()) % 7
    candidate += timedelta(days=days_ahead)
    if candidate <= now:
        candidate += timedelta(days=7)
    return candidate


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
    slot_times = []
    prior = list(published_subjects or [])

    for i, original_story in enumerate(entries):
        # Validated against the story the pipeline will actually render, which
        # is the repaired one — see repair_story(). A recoverable length
        # overrun must not fail a pre-flight that gates every slot in the week.
        story, _ = repair_story(original_story)
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
            slot_times.append(slot)
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

        # THE LENGTH GATE — only for a story that can still be rendered.
        #
        # tts.synthesize_all aborts a run whose narration will not fit
        # VIDEO_LENGTH_SECONDS inside the safe playback-speed band. Until
        # 2026-09-13 that was the first and only place length was ever
        # checked, so a mislengthed packet passed --validate cleanly and then
        # burned one slot per story: the bridge packet 2026-W39 shipped with
        # 10 of its 20 stories out of band and took the channel dark for 22
        # hours, three failed attempts at a time.
        #
        # Scoped to SELECTABLE because the check is only actionable while the
        # prose can still be rewritten. Two already-published stories in that
        # same packet sit inside the estimator's safety margin; failing the
        # whole packet — and with it every future slot — over a video that is
        # already on the channel would be an outage caused by the outage
        # detector.
        if status in SELECTABLE and segments:
            length_problem = script_writer.check_narration_length(
                [seg.get("narration", "") for seg in segments])
            if length_problem:
                problems.append(f"{where}: {length_problem}")

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

        # thumbnail_prompt is OPTIONAL, and was a hard failure until 2026-09-09.
        #
        # The gate could reject a fully-researched story — costing that slot and
        # every later one in the same pre-flight — over a field that nothing in
        # this repository has ever consumed. There is no thumbnail upload path
        # in agent/upload.py, no image generation anywhere in the pipeline, and
        # the Shorts feed serves a frame from the video rather than a custom
        # thumbnail, so no video this channel has published was ever affected by
        # the prompt being present or absent.
        #
        # Kept in the schema rather than deleted: a custom thumbnail still shows
        # on the channel's Shorts shelf and in search results, so this becomes
        # useful the day an image step exists. Until then it is a note for a
        # human, and a note may not veto a week of publishing.
        if story.get("thumbnail_prompt") is not None and not isinstance(
                story.get("thumbnail_prompt"), str):
            problems.append(
                f"{where}: thumbnail_prompt must be a string when present.")

        problems.extend(validate_editorial(story, where))

        tags = (story.get("metadata") or {}).get("tags") or []
        if not isinstance(tags, list) or len(tags) < MIN_TAGS:
            problems.append(
                f"{where}: metadata.tags needs at least {MIN_TAGS} entries.")

    problems.extend(_validate_slot_run(slot_times))
    # Packet-wide, not per-story: a single title cannot be a monoculture.
    #
    # Judged across the WHOLE packet as authored, including stories that have
    # already published. Counting only the unpublished remainder was tried
    # first and is wrong twice over: the sample shrinks as the week drains, so
    # the same untouched packet passes on Monday and fails on Friday, and a
    # gate that starts failing mid-week blocks every remaining slot over a
    # decision nobody can now change. Live proof: 2026-W38 is 10/28 (36%) as
    # written and passes, but its last 12 unpublished stories were 5/12 (42%)
    # and would have failed the pre-flight for the rest of the week.
    problems.extend(validate_title_diversity([s.get("title") for s in entries]))
    return problems


def _validate_slot_run(slot_times: list[datetime]) -> list[str]:
    """Every publishing slot between the packet's first and last must have a
    story.

    A packet is a schedule, not a bag of stories, and the count check above
    only proves it holds as many as it says it does. A packet that skips
    2026-09-10T0607Z entirely still has the right length and still leaves that
    slot's scheduled run with nothing to publish — a silent dark slot rather
    than a loud failure. The gap is visible here, before the week starts,
    which is the only point at which it is cheap to fix."""
    real = sorted(s for s in slot_times if s is not None)
    # Small packets are used for manual recovery and for narrow validation
    # fixtures.  Only a declared full weekly packet promises every cadence
    # slot across its span; treating any two arbitrary stories years apart as
    # one continuous schedule broke the normal "queue ahead of the clock"
    # validation path.
    if len(real) != cadence.SLOTS_PER_WEEK:
        return []
    expected = cadence.slots_between(real[0], real[-1])
    actual_ids = {cadence.slot_id(r) for r in real}
    missing = [cadence.slot_id(s) for s in expected
               if cadence.slot_id(s) not in actual_ids]
    if not missing:
        return []
    shown = ", ".join(missing[:6]) + (f" (+{len(missing) - 6} more)"
                                      if len(missing) > 6 else "")
    return [f"the packet skips {len(missing)} publishing slot(s) between "
            f"{cadence.slot_id(real[0])} and {cadence.slot_id(real[-1])}: {shown}. "
            "Every skipped slot is a scheduled run with nothing to publish."]


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

    # UNREADABLE and UNRECOGNISED are the same failure wearing different
    # clothes, and only the first one used to be caught. A ledger that parsed
    # but was a list, or carried a schema_version this code has never seen, ran
    # straight through setdefault("stories", {}) and became an EMPTY ledger —
    # which says "nothing has ever been published" just as loudly as a corrupt
    # file would, and republishes the week just as thoroughly. Fail closed on
    # anything this code cannot actually read.
    if not isinstance(ledger, dict):
        raise PacketError(
            "content/story_history.json is not a JSON object (it is a "
            f"{type(ledger).__name__}). Repair it before publishing — reading "
            "it as empty would republish stories that have already gone out.")
    if ledger.get("schema_version") != SCHEMA_VERSION:
        raise PacketError(
            f"content/story_history.json is schema_version "
            f"{ledger.get('schema_version')!r} but this code reads version "
            f"{SCHEMA_VERSION}. Refusing to publish against a ledger it cannot "
            "read: an unrecognised ledger read as empty would republish "
            "stories that have already gone out.")
    stories_map = ledger.get("stories")
    if stories_map is None:
        ledger["stories"] = {}
    elif not isinstance(stories_map, dict):
        raise PacketError(
            "content/story_history.json has a 'stories' field that is a "
            f"{type(stories_map).__name__}, not an object keyed by story id. "
            "Repair it before publishing.")
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


def published_by_slot(ledger: dict = None) -> dict[str, list[dict]]:
    """Every published ledger entry, grouped by the slot it was published for.

    A slot is one video. Nothing enforced that: validate_packet() rejects two
    stories claiming the same slot WITHIN one packet, which says nothing about
    a slot a PREVIOUS packet already served. On 2026-09-08 slot 0607Z went out
    twice — 'Voynich manuscript' claimed early by a manual run off the previous
    packet, then 'Anglo-Zanzibar War' claimed 5.1h late off this one — because
    the second packet reused a slot id the ledger already had a video for."""
    entries = (ledger or _load_ledger())["stories"].values()
    by_slot: dict[str, list[dict]] = {}
    for entry in entries:
        if entry.get("status") != "published":
            continue
        slot = str(entry.get("slot") or "").strip()
        if not slot:
            continue
        by_slot.setdefault(slot, []).append(entry)
    return by_slot


def slot_already_served(slot_key: str, story_id: str = "",
                        ledger: dict = None) -> dict | None:
    """The published entry that already holds `slot_key`, if it is a different
    story from `story_id`. None means the slot is free for this story."""
    for entry in published_by_slot(ledger).get(slot_key, []):
        if entry.get("story_id") != story_id:
            return entry
    return None


def duplicate_published_slots(ledger: dict = None) -> dict[str, list[dict]]:
    """Slots that have more than one published video, newest publish last.

    Detection only. Reconciling them means deciding which video stays on the
    channel, and nothing here deletes anything from YouTube — see
    scripts/reconcile_story_slots.py."""
    return {
        slot: sorted(entries, key=lambda e: str(e.get("published_at") or ""))
        for slot, entries in published_by_slot(ledger).items()
        if len(entries) > 1
    }


def repair_story(story: dict) -> tuple[dict, list[str]]:
    """A copy of `story` with the length overruns that are recoverable already
    repaired, plus a note per repair.

    agent/script_writer.py has carried two trimmers since the generator era —
    trim_long_topic_subject and trim_long_visual_queries — written precisely
    because throwing a slot away over two words is the expensive choice. The
    packet then rejected the same overruns outright and never called either of
    them, so they were dead code and a five-word topic_subject failed
    daily.yml's pre-flight for every remaining slot in the week.

    Trimming keeps the LEADING words, which is where the subject anchor is, and
    only ever shortens: a subject that is missing, or a visual query with too
    FEW words, cannot be repaired by inventing text and stays a real failure."""
    repaired = dict(story)
    notes = []

    subject_note = script_writer.trim_long_topic_subject(repaired)
    if subject_note:
        notes.append(f"topic_subject trimmed: {subject_note}")

    segments = [dict(s) if isinstance(s, dict) else s
                for s in (repaired.get("segments") or [])]
    if segments:
        repaired["segments"] = segments
        for note in script_writer.trim_long_visual_queries(segments):
            notes.append(f"visual_query trimmed: {note}")
    return repaired, notes


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


def runway_hours(packet: dict, now: datetime = None) -> float | None:
    """Hours of planned publishing left: the gap from now to the LAST slot that
    still holds an unpublished story.

    None means the packet is already exhausted. 0.0 means every remaining story
    is overdue, which is a different (and less urgent) problem than having
    nothing left at all — the queue can be behind the clock and still full."""
    now = now or _now()
    slots = [_slot_dt(s) for s in stories(packet) if status_of(s) in SELECTABLE]
    slots = [s for s in slots if s is not None]
    if not slots:
        return None
    return max(0.0, (max(slots) - now).total_seconds() / 3600.0)


def runway_deficit_hours(packet: dict, now: datetime = None) -> float:
    """How many hours SHORT the packet is of surviving until the next write.

    0.0 means it comfortably outlives the next packet; a positive number is the
    size of the hole.

    WHY A FIXED 48-HOUR THRESHOLD WAS NOT ENOUGH. It answers "is there much
    left", when the question that actually decides whether the channel goes
    quiet is "will this last until its replacement is written". Those come
    apart whenever the write day drifts, and it drifted immediately:

      packet 2026-W38 was written Tue 2026-09-08 and its last slot is
      Tue 2026-09-15 01:07 UTC — a correct, full seven days. But Cowork writes
      on WEDNESDAYS, so the next packet was not due until Wed 2026-09-16 15:15
      UTC. 38 hours, about six slots, with nothing to publish. Runway at the
      time was 62.6 hours: comfortably ABOVE the 48-hour threshold, and still
      guaranteed to run dry.

    A packet written on its proper day has roughly a day of slack and never
    trips this. One written early, or a Cowork session that slips, trips it
    immediately — which is the whole point."""
    now = now or _now()
    runway = runway_hours(packet, now)
    if runway is None:
        # Already exhausted. The deficit is the whole wait for the next write.
        return max(0.0, (next_packet_write(now) - now).total_seconds() / 3600.0)
    needed = ((next_packet_write(now) - now).total_seconds() / 3600.0
              + PACKET_WRITE_MARGIN_HOURS)
    return max(0.0, needed - runway)


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
        # Editorial shape and experiment arm, carried through to the video
        # record so performance can later be grouped by them. See
        # EXPERIMENT_FIELDS.
        "editorial": editorial_metadata(story),
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

        # ONE SLOT, ONE VIDEO. Checked here and not only in validation because
        # the two see different things: validation compares a packet against
        # itself, this compares it against every video the channel has actually
        # published. Slot 2026-09-08T0607Z went out twice precisely because a
        # new packet reused a slot id the ledger already held a video for, and
        # nothing looked across the two packets.
        slot_dt = _slot_dt(candidate)
        served = slot_already_served(
            cadence.slot_id(slot_dt) if slot_dt else "",
            str(candidate.get("story_id") or "")) if slot_dt else None
        if served:
            print(f"      skipping {candidate.get('story_id')}: slot "
                  f"{cadence.slot_id(slot_dt)} was already published as "
                  f"{served.get('video_id')} ({served.get('story_id')})")
            record_status(
                candidate, "skipped",
                note=f"slot {cadence.slot_id(slot_dt)} was already served by "
                     f"{served.get('story_id')} (video {served.get('video_id')})")
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

    story, repairs = repair_story(story)
    story.setdefault("packet_id", packet.get("packet_id", ""))
    for note in repairs:
        print(f"      [packet] {note}")

    late = lateness_hours(story, now)
    record_status(story, "queued",
                  note=f"claimed by a run at {_iso(now)}"
                       + (f", {late:.1f}h after its slot" if late else "")
                       + (" (manual early claim)" if allow_early else ""))
    slot = _slot_dt(story)
    print(f"      story {story.get('story_id')} for slot "
          f"{cadence.describe(slot) if slot else 'unknown'}: "
          f"{story.get('topic_subject')!r}")
    script = to_script(story)
    if repairs:
        script["packet_repairs"] = repairs
    return script


def reclaim_script(script: dict, now: datetime = None) -> dict:
    """Re-counts a claim a resumed run inherited from a checkpoint.

    claim_script() is what increments `attempts`, and a resumed run skips it —
    the script comes back out of the checkpoint instead. So a story that failed
    late in the pipeline, four runs in a row, was still on attempt 1 and could
    never reach MAX_ATTEMPTS: the retirement that stops one bad entry wedging
    the whole queue was unreachable from the exact failure mode it was written
    for.

    Raises rather than returning when the story must not be rendered again —
    already published, or out of attempts — because continuing would either
    duplicate a video or re-enter the loop this exists to break."""
    now = now or _now()
    story_id = str(script.get("story_id") or "").strip()
    if not story_id:
        # A checkpointed script from before provenance existed. Nothing to
        # count against, and refusing it would strand a resumable run.
        return script

    entry = ledger_entry(story_id)
    if entry.get("status") == "published":
        raise PacketError(
            f"The checkpointed story {story_id} is already marked published "
            f"(video {entry.get('video_id')}). Refusing to render it again — "
            "clear the checkpoint if this is genuinely a new story.")
    if entry.get("attempts", 0) >= MAX_ATTEMPTS:
        record_status(script, "failed",
                      note=f"retired after {entry.get('attempts')} attempts; "
                           "the last one resumed from a checkpoint")
        raise PacketError(
            f"The checkpointed story {story_id} has been claimed "
            f"{entry.get('attempts')} times without reaching the channel and is "
            f"now retired ({MAX_ATTEMPTS} attempts is the limit). The next run "
            "will start the following story instead.")

    record_status(script, "queued",
                  note=f"re-claimed from a checkpoint by a run at {_iso(now)}")
    return script


def mark_published(script: dict, video_id: str) -> None:
    """Called once the video is on the channel. Idempotent by story id, so the
    resume path can call it for a video an earlier run uploaded.

    A publish into a slot another video already holds is still recorded — by
    this point the video IS live and refusing to write it down would strand it
    — but it is recorded WITH the conflict attached, so the weekly health check
    and scripts/reconcile_story_slots.py can surface a decision only a human
    can make."""
    if not script.get("story_id"):
        return
    extra = {}
    slot = _slot_dt(script)
    if slot:
        served = slot_already_served(cadence.slot_id(slot),
                                     str(script.get("story_id") or ""))
        if served:
            extra["slot_conflict"] = True
            extra["slot_conflict_with"] = {
                "story_id": served.get("story_id"),
                "video_id": served.get("video_id"),
                "published_at": served.get("published_at"),
            }
            print(f"[packet] WARNING: slot {cadence.slot_id(slot)} already "
                  f"holds {served.get('video_id')}; recording {video_id} as a "
                  "slot conflict for a human to reconcile")
    record_status(script, "published", note=f"published as {video_id}",
                  video_id=video_id, published_at=_iso(_now()), **extra)


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

    # Surfaced, never fatal. A slot that already has two videos is history: the
    # videos are live and failing this gate would stop every remaining slot
    # from publishing over something no run can undo. Reconciling it is a
    # human decision — see scripts/reconcile_story_slots.py.
    duplicates = duplicate_published_slots()
    for slot, entries in sorted(duplicates.items()):
        print(f"::warning::slot {slot} has {len(entries)} published videos: "
              + ", ".join(f"{e.get('video_id')} ({e.get('story_id')})"
                          for e in entries)
              + ". Run scripts/reconcile_story_slots.py to review it.")
    if duplicates:
        # Emailed as well as logged. A ::warning:: on a green run is invisible
        # unless someone opens the run, and a duplicate slot means two videos
        # on the channel competing for the same audience — a human has to pick
        # one. notify.alert never raises, so a mail failure cannot fail the
        # pre-flight.
        notify.alert(
            f"{len(duplicates)} slot(s) published more than one video",
            "These slots each hold more than one published video:\n\n"
            + "\n".join(
                f"  {slot}: " + ", ".join(
                    f"{e.get('video_id')} ({e.get('topic_subject')})"
                    for e in entries)
                for slot, entries in sorted(duplicates.items()))
            + "\n\nBoth videos are live. Deciding which one stays is a human "
              "call: run scripts/reconcile_story_slots.py to review them.\n"
              "New double-publishes are prevented at claim time by "
              "slot_already_served(); this is about the ones already out.")

    # How much planned content is left, and an email while there is still time
    # to do something about it rather than after the channel has gone quiet.
    runway = runway_hours(packet)
    if runway is None:
        print("::warning::the packet has no unpublished stories left at all.")
    else:
        deficit = runway_deficit_hours(packet)
        write_at = next_packet_write()
        print(f"Planned content remaining: {runway:.1f} hours "
              f"(alert threshold {RUNWAY_ALERT_HOURS:.0f}h). Next packet is "
              f"written {cadence.describe(write_at)}.")
        if deficit > 0:
            # The packet will run dry BEFORE its replacement is written, which
            # a runway number alone cannot show: 2026-W38 had 62.6 hours left —
            # comfortably above the 48-hour floor — and a guaranteed 38-hour
            # hole behind it.
            print(f"::error::the packet runs out {deficit:.0f} hours before "
                  f"the next one is due to be written.")
            notify.alert(
                f"Packet runs dry {deficit:.0f}h before the next one is written",
                f"Packet {packet.get('packet_id')} has {runway:.1f} hours of "
                f"planned content left. The next packet is not written until "
                f"{cadence.describe(write_at)}, which leaves roughly "
                f"{deficit:.0f} hours — about {int(deficit // 6)} publishing "
                "slots — with nothing to publish.\n\n"
                "This is the failure that produced eight silent days between "
                "2026-08-24 and 2026-09-01. The runway number alone does not "
                "show it: a packet can be above the 48-hour floor and still be "
                "guaranteed to run out first, whenever the write day drifts.\n\n"
                "Write the next week EARLY rather than waiting for the "
                "scheduled session:\n"
                "  python scripts/build_editorial_brief.py\n"
                "  python scripts/assemble_packet.py drafts.json --packet-id <id>\n"
                "  python -m agent.packet --validate")
        if runway < RUNWAY_ALERT_HOURS:
            notify.alert(
                f"Story packet runway is down to {runway:.0f} hours",
                f"Packet {packet.get('packet_id')} has {len(remaining)} "
                f"unpublished stories and its last planned slot is "
                f"{runway:.1f} hours away.\n\n"
                "When it runs out the channel publishes nothing — there is no "
                "fallback generator by design. Between 2026-08-24 and "
                "2026-09-01 that meant eight days of silence.\n\n"
                "Deliver a fresh week with the weekly Claude task, or by hand:\n"
                "  python scripts/build_editorial_brief.py\n"
                "  python scripts/assemble_packet.py drafts.json --packet-id <id>\n"
                "  python -m agent.packet --validate")

    # A run that starts hours after its slot still publishes the right story
    # (due_stories drains FIFO), but it publishes it at the wrong time, and a
    # channel whose upload times are scattered across 20 UTC hours cannot build
    # an audience habit. Reported so the external dispatch trigger going
    # silently missing is visible — see docs/scheduling.md.
    slot_now = cadence.slot_for(_now())
    if slot_now is not None:
        late = cadence.lateness_minutes(slot_now, _now())
        if late >= cadence.LATE_RUN_ALERT_MINUTES:
            print(f"::warning::this run started {late:.0f} minutes after its "
                  f"{cadence.slot_id(slot_now)} slot.")
            notify.alert(
                f"Scheduled run started {late:.0f} minutes late",
                f"The run serving slot {cadence.slot_id(slot_now)} "
                f"({cadence.describe(slot_now)}) started {late:.0f} minutes "
                f"after its slot time.\n\n"
                "The story is still correct — selection drains slots in order — "
                "but the upload time is not.\n\n"
                "If the repository_dispatch trigger in docs/scheduling.md is "
                "configured, it has stopped and the cron fallback is carrying "
                "the channel. If it is not configured yet, this is the "
                "known GitHub scheduling delay (median 166 minutes measured "
                "over 62 runs) and docs/scheduling.md says what to do.")

    out = os.getenv("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(f"due={'true' if ready else 'false'}\n")
            f.write(f"remaining={len(remaining)}\n")
            f.write(f"packet_id={packet.get('packet_id')}\n")
            f.write(f"runway_hours={'' if runway is None else round(runway, 1)}\n")
            f.write(f"duplicate_slots={len(duplicates)}\n")

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



# TITLE DIVERSITY.
#
# Measured across the 129 videos published up to 2026-09-12: 108 of them — 83.7%
# — open with the word "The". "The Day" appears 10 times, "The Ghost" 8, "The
# Strange" 6. The top 25 and bottom 25 videos by views are formally
# indistinguishable (mean 8.3 vs 8.1 words, "The" opener 24/25 vs 25/25), which
# says nothing about which titles work and everything about only one kind of
# title ever being tried. To a browsing viewer the channel reads as one video
# published over and over.
#
# The 2026-W38 packet fixed this by hand — its worst opener share is 10 of 28
# (35.7%, "A") — but nothing stopped the next packet regressing to the old
# habit. This is the gate that does.
#
# 40% is set from that evidence: it passes W38 comfortably, and the historical
# 83.7% fails it by a mile. Deliberately a loose bound rather than a style
# rule — it catches a monoculture, it does not dictate how to write a title.
#
# Only counted across a FULL week's packet: a two-story recovery packet sharing
# an opener is not evidence of anything.
MAX_TITLE_OPENER_SHARE = 0.4
MIN_STORIES_FOR_DIVERSITY_CHECK = 8


def title_opener_counts(titles: list[str]) -> dict[str, int]:
    """How many titles start with each opening word, case- and quote-folded."""
    counts: dict[str, int] = {}
    for title in titles:
        words = str(title or "").strip().split()
        if not words:
            continue
        opener = words[0].lower().strip("\"'\u2018\u2019\u201c\u201d")
        if opener:
            counts[opener] = counts.get(opener, 0) + 1
    return counts


def validate_title_diversity(titles: list[str]) -> list[str]:
    """Fails a packet whose titles nearly all open the same way."""
    usable = [t for t in titles if str(t or "").strip()]
    if len(usable) < MIN_STORIES_FOR_DIVERSITY_CHECK:
        return []
    counts = title_opener_counts(usable)
    if not counts:
        return []
    opener, n = max(counts.items(), key=lambda kv: kv[1])
    share = n / len(usable)
    if share > MAX_TITLE_OPENER_SHARE:
        return [
            f"{n} of {len(usable)} titles ({share * 100:.0f}%) open with "
            f"{opener!r}, above the {MAX_TITLE_OPENER_SHARE * 100:.0f}% limit. "
            "83.7% of this channel's first 129 videos opened with 'The' and "
            "the best and worst performers were indistinguishable; vary the "
            "openings rather than shipping another week of one shape."
        ]
    return []


# ------------------------------------------------------- experiment metadata

# The editorial dimensions a story may declare, and the ONLY ones the pipeline
# records for later analysis.
#
# WHY THIS EXISTS. Every one of the first 114 videos used the same voice
# (en-US-GuyNeural), the same five-segment structure, the same Pexels stock
# treatment, and — measured 2026-09-09 — a title beginning with the word "The"
# 94% of the time. With no variation recorded anywhere, no question about
# format could be answered from the channel's own history: the top 25 and
# bottom 25 videos by views were formally indistinguishable (mean 8.3 vs 8.1
# words, "The" opener 24/25 vs 25/25), which says nothing about what works and
# everything about there being only one thing being tried.
#
# These fields do not CHANGE anything on their own. They are labels, so that
# when a packet does vary a dimension, the variation is attributable afterwards
# instead of being lost. One variable at a time is the discipline the analysis
# needs; the schema simply refuses to lose the record of it.
EXPERIMENT_FIELDS = (
    "series",         # recurring format, e.g. "Solved" — a reason to subscribe
    "experiment",     # the dimension deliberately varied in this packet
    "arm",            # which side of that experiment this story is on
    "title_style",    # e.g. "the-noun", "absurd-number", "question"
    "hook_style",     # e.g. "cold-open-number", "scene-then-question"
    "voice",          # overrides config.VOICE when set
    "pacing",         # e.g. "standard", "fast-cut"
    "visual_style",   # e.g. "stock-kenburns", "static-archival"
)


def editorial_metadata(story: dict) -> dict:
    """The story's declared editorial shape, with unknown keys dropped.

    Absent is absent: a field the packet did not declare is simply not present,
    rather than defaulting to "standard". A default would make 114 historical
    videos look like a deliberate control group for an experiment nobody ran."""
    raw = story.get("editorial")
    if not isinstance(raw, dict):
        return {}
    out = {}
    for field in EXPERIMENT_FIELDS:
        value = raw.get(field)
        if isinstance(value, str) and value.strip():
            out[field] = value.strip()
    return out


def validate_editorial(story: dict, where: str) -> list[str]:
    """Editorial metadata is optional, but must not be malformed when present.

    Deliberately permissive about VALUES — the vocabulary of title styles is
    the editorial team's to grow, and a validator that only accepts an
    enumerated list would reject the first genuinely new idea. It is strict
    about SHAPE, because a dict where a string belongs is how a field silently
    stops being recorded."""
    raw = story.get("editorial")
    if raw is None:
        return []
    if not isinstance(raw, dict):
        return [f"{where}: editorial must be an object when present."]
    problems = []
    for key, value in raw.items():
        if key not in EXPERIMENT_FIELDS:
            problems.append(
                f"{where}: editorial.{key} is not a recognised field "
                f"(expected one of {', '.join(EXPERIMENT_FIELDS)}).")
        elif not isinstance(value, str):
            problems.append(f"{where}: editorial.{key} must be a string.")
    # An arm without an experiment cannot be grouped against anything, and an
    # experiment without an arm cannot be split. Either alone is a recording
    # mistake that only shows up weeks later when the analysis comes up empty.
    if ("arm" in raw) != ("experiment" in raw):
        problems.append(
            f"{where}: editorial.experiment and editorial.arm must be set "
            "together — an arm with no experiment cannot be compared to "
            "anything, and an experiment with no arm cannot be split.")
    return problems


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
