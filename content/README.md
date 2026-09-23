# `content/` — the story packet

Three files live here. The brief is generated from local channel data; the
packet and ledger are never hand-edited.

## `weekly_editorial_brief.json` — feedback for next week's research

GitHub refreshes this at 20:17 IST every Wednesday, before the 20:45 Claude
Cowork session. `python scripts/build_editorial_brief.py` performs the same
manual refresh. It writes JSON plus `weekly_editorial_brief.md`, using only
already-recorded channel measurements. It has:

- a full no-repeat `avoid_subjects` list;
- strong and weak examples for mature views, comparable early views and
  first-quarter retention. Hook examples appear only after at least four
  top, early-distributed records have retention data; otherwise the brief says
  that no hook pattern is ready to copy;
- methodology and caveats, so different-age view counts are never presented
  as a fake apples-to-apples comparison.

Claude must read this brief before researching new stories. Use it to choose
fresh angles and stronger hook structures; do not copy a prior title, subject
or wording. Every new draft must include an `editorial_rationale` sentence
saying which measured brief signal guided its angle or hook.
`scripts/assemble_packet.py` refuses a missing, malformed or older-than-eight-
day brief, stores its hash in every new packet, and rejects a new story without
that rationale.

## `weekly_story_packet.json` — the plan

One publishing week of stories, written ahead of time by a Claude Cowork task
that runs every Wednesday at 20:45 Asia/Kolkata. One story per publishing slot;
at four slots a day that is 28 stories a week (`agent/cadence.py`).

Each story carries everything the pipeline needs and everything a human needs
to check it: the research and its sources, a per-claim verification verdict, a
five-segment narration script, subject-anchored footage queries, a thumbnail
prompt, tags, and a stable `story_id`.

`agent/packet.py` validates every field before a run touches it — the same
rules `agent/script_writer.py` has always enforced on a script, plus the slot
arithmetic, research fields, editorial-brief provenance, and new-story
editorial rationale. `weekly_story_packet.md` is the same content, readable.

Stories from the previous packet whose slot has already PASSED unpublished
(the queue drains FIFO and GitHub starts runs late) are carried in front of the
new week, at most one day's worth (4), by `scripts/assemble_packet.py`. They
keep their original slots, are listed in the packet's `carried_overdue`, and
are the first ones the next runs publish. Do not mark them skipped; write the
usual number of drafts for the window.

## `story_history.json` — the ledger

Append-only status for every story ever planned:

    proposed  ->  queued  ->  published
                     |
                     +-----> failed   (after 3 attempts)
                     +-----> skipped  (subject went out under another entry)

This is what stops a story publishing twice, what the next weekly task reads so
it never re-proposes a subject, and what `scripts/resolve_data_conflicts.py`
merges when two runs write it at once (the further-along status wins; a
`published` is never rolled back).

## What makes a run fail

`daily.yml` validates the packet before every slot. It fails the run for a
packet that is missing, malformed, or **exhausted** — nothing left for this run
or any run after it.

It does NOT fail when no story is merely due yet. Selection is FIFO over slots
that have arrived, so that can only mean every story planned up to now has
already gone out: the queue is ahead of the clock, not broken. The run skips
and publishes nothing. GitHub fired a scheduled run 3h40m late on 2026-09-07
for a slot another run had already served, and a red run plus an alert email is
the wrong answer to a healthy channel.

## Manual recovery

A scheduled run may only publish the story whose slot has arrived; a slot that
passes with nothing to publish is a real failure and the run goes red. A manual
`workflow_dispatch` of `daily.yml` is different — it sets `PACKET_ALLOW_EARLY=1`
and claims the next pending story whatever the time, which is how a missed slot
gets caught up and how the pipeline is checked end to end.

## Editing by hand

Don't edit the JSON directly. Draft stories as a JSON array and run:

    python scripts/build_editorial_brief.py
    # Read content/weekly_editorial_brief.md and JSON avoid_subjects first.
    # Each draft needs editorial_rationale naming its measured evidence.
    python scripts/assemble_packet.py drafts.json --packet-id 2026-W38
    python -m agent.packet --validate

The assembler does the slot arithmetic, mints the ids, carries forward
still-unpublished stories from the previous packet, and refuses to write a
packet that would not validate. A gap in the packet is a scheduled run with
nothing to publish.
