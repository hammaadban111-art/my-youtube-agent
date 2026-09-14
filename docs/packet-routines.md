# The two Claude Routines that keep the packet stocked

The pipeline has no story generator (`agent/packet.py`, top of file). Stories
come from Claude Routines, and this file is the source of truth for what those
Routines are told to do.

| Routine | When | What it does |
|---|---|---|
| **Weekly YouTube story packet** | Wed 15:15 UTC / 20:45 IST | writes the next full week |
| **Story packet catch-up** | daily 02:40 UTC, then a random 30-300 min delay | writes only the missing stories, and only when `--shortfall` says the packet is short |

`.github/workflows/packet-watchdog.yml` watches both and emails if the packet is
short regardless of what either Routine did.

## Why this file exists

The weekly Routine was created through the claude.ai Routines UI, so it can only
be edited there — an agent cannot update it (`update_trigger` refuses a routine
created via `http_api`). Its prompt is therefore kept here, in version control,
and **must be pasted into the UI by hand whenever it changes**.

The catch-up Routine was created by an agent and can be updated with
`update_trigger`, but its prompt is kept here too so the pair stay legible
together.

## What changed on 2026-09-14, and why

The weekly prompt used to say:

> Total narration around 110-125 words across the five segments (~35-45 seconds spoken).

Both halves of that were wrong in the same direction. The configured voice at
`TTS_RATE` speaks at roughly **205 words a minute**, not the ~150 a written
estimate assumes, so "35-45 seconds" of prose written to that rule comes out
around 25-30 seconds — and `agent/tts.py` refuses to render it. That is exactly
what produced the 2026-09-12 bridge packet, ten of whose twenty stories were out
of band, and the 22-hour blackout that followed.

The contract is now stated in **characters**, which predict spoken length more
than twice as accurately as words, and it is generated from the same constants
the validator enforces:

    python -m agent.packet --length-spec

Both prompts below now point at that command instead of quoting a number that
can drift.

---

## Weekly Routine prompt (paste into the claude.ai Routines UI)

The corrected prompt is kept as a separate file so it can be copied without
picking it out of prose:

    docs/weekly-packet-routine-prompt.md

---

## Catch-up Routine prompt

Stored with the Routine itself (`trig_0124QxKT3DAqQhFHQTbTkGCT`) and editable
with `update_trigger`. Its shape:

1. **Get the repository.** The Routine pins no git source, so it clones if the
   working directory is empty. (An agent-created Routine cannot set `sources`;
   if you would rather it had one, recreate the Routine in the UI with the
   repository attached and delete the agent-created one.)
2. **Ask `python -m agent.packet --shortfall`.** If the packet is HEALTHY it
   stops immediately, having done nothing. This is the normal path and it is
   meant to be cheap — that is what makes a daily Routine affordable.
3. **Only if SHORT**, defer itself by a random 30-300 minutes via `send_later`,
   then write the missing stories on the deferred run. The randomness spreads
   the work off a fixed instant, and — since the 2026-09-09 miss was a five-hour
   usage limit — simply trying again later the same day is the real remedy.
   If `send_later` fails it writes immediately rather than skipping a day.
4. **Repeat daily until healthy.** The condition clears itself the moment a full
   week lands, which is what makes the retry terminate instead of looping.
