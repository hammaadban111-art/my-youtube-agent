# `content/` — the story packet

Two files live here. Neither is written by hand.

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
arithmetic and the research fields. `weekly_story_packet.md` is the same
content, readable.

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

## Editing by hand

Don't edit the JSON directly. Draft stories as a JSON array and run:

    python scripts/assemble_packet.py drafts.json --packet-id 2026-W38
    python -m agent.packet --validate

The assembler does the slot arithmetic, mints the ids, carries forward
still-unpublished stories from the previous packet, and refuses to write a
packet that would not validate. A gap in the packet is a scheduled run with
nothing to publish.
