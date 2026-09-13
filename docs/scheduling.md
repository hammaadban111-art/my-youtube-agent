# Scheduling: why the channel publishes at random times, and how to fix it

## The measurement

Every scheduled `daily.yml` run between 2026-08-25 and 2026-09-09, compared
against the cron time it was supposed to fire at:

| cron (UTC) | runs | median delay | worst |
|---|---|---|---|
| `01:07` | 10 | **274 min** | 289 min |
| `06:07` | 8 | 58 min | 283 min |
| `11:07` | 23 | 92 min | 267 min |
| `16:07` | 21 | 188 min | **523 min** |
| **all** | **62** | **166 min** | 8h 42m |

44 of 62 runs were more than an hour late. 29 were more than three hours late.

This is not something the repository can fix in code. GitHub deprioritises
`schedule:` triggers, and does so harder on private repositories and on
accounts near their Actions minute allowance. The cron fires when GitHub has
spare capacity, not when the cron says.

## Why it matters

- **Published-at times are scattered across 20 different UTC hours.** Neither
  the audience nor YouTube's recommender can learn a publishing rhythm.
- **"What is our best upload time?" is unanswerable from our own data.** The
  hour a video went out is mostly a record of GitHub queue depth, so any
  analysis of it is analysing noise.
- It caused a real lost slot. On 2026-09-07 a run fired 3h40m late, found the
  slot already served, and exited red with
  `##[error]No story in the packet is due for this run`.

The lost-slot half is now fixed in code regardless of scheduling:
`agent/packet.py::due_stories` drains slots FIFO instead of matching a run to
its exact slot, and `daily.yml` treats "nothing is due" as a skip rather than a
failure. A late run therefore publishes the right story. What remains broken is
*when* it publishes.

## The fix: an external scheduler and `repository_dispatch`

`daily.yml` already accepts a `repository_dispatch` of type `publish-slot`. A
dispatch starts the workflow within seconds of the POST, so moving the clock
outside GitHub removes the delay entirely.

**This is not active yet — it needs one secret that this repository does not
have.** Until it is configured, nothing is sent, the four cron lines keep
running the channel exactly as they do today, and the fallback is simply the
current behaviour. Wiring it in cannot make anything worse.

### What must be created manually

1. **A fine-grained personal access token**
   - Repository access: only `hammaadban111-art/my-youtube-agent`
   - Repository permissions: **Contents: read-only** and **Metadata:
     read-only**, plus the one that actually matters:
     **Actions: read and write** — `repository_dispatch` is an Actions write.
   - Expiry: set a real one and calendar the renewal. A silently expired token
     puts the channel back on the cron fallback, which is degraded but safe.

2. **An external scheduler** that can POST on a cron. Any of these works and
   all have a free tier sufficient for four requests a day: cron-job.org,
   Val Town, Cloudflare Workers Cron Triggers, or a `launchd` job on a machine
   that is reliably awake. The repository does not care which.

3. **The request**, sent at each of `01:07`, `06:07`, `11:07` and `16:07` UTC:

   ```
   POST https://api.github.com/repos/hammaadban111-art/my-youtube-agent/dispatches
   Authorization: Bearer <THE TOKEN>
   Accept: application/vnd.github+json
   X-GitHub-Api-Version: 2022-11-28
   Content-Type: application/json

   {"event_type": "publish-slot"}
   ```

   A `204 No Content` means accepted. Anything else means the cron fallback is
   carrying the channel and the token needs looking at.

**Do not paste the token into this repository.** It belongs in the external
scheduler only. Nothing in this repo needs to read it; the dispatch is inbound.

### Keep the crons

The four `cron:` lines stay as the fallback. If the external scheduler dies,
the channel keeps publishing on GitHub's schedule — late, but publishing. Two
triggers firing for the same slot is safe: `agent/packet.py::slot_already_served`
refuses to publish a second video into a slot the ledger already holds, and the
shared `repo-data-writers` concurrency group serialises the runs.

## Monitoring

`agent/cadence.py::lateness_minutes` and the late-run alert in `daily.yml`
report when a run starts more than `LATE_RUN_ALERT_MINUTES` (default 90) after
the slot it is serving. That is how you find out the external scheduler has
stopped without waiting to notice the upload times drifting again.

## What was considered and rejected

- **More cron lines** (hourly instead of 4×/day) so a late run lands nearer a
  slot. Rejected: every extra run costs a checkout and a Python setup even when
  it exits immediately, GitHub delays *all* crons rather than a random subset,
  and on a private repo already over its free minute allowance this spends real
  money to buy an unmeasured improvement.
- **A self-hosted runner.** Removes the queue delay but requires a machine that
  is always on, which is exactly what running on GitHub's runners was chosen to
  avoid.

## Who writes the packet, and what happens when they don't

The pipeline has no story generator. `content/weekly_story_packet.json` is
written by a **Claude Routine on Wednesdays at 15:15 UTC / 20:45 IST**, and
every scheduled run until the next one publishes out of that file. If the
Routine does not deliver, the channel goes quiet — there is no fallback
generator and there must never be one (see the note at the top of
`agent/packet.py`).

That is not hypothetical. On **2026-09-09 the Wednesday Routine failed
eighteen seconds after firing** and nothing was watching it. The miss surfaced
days later as an empty queue, was patched by hand with a "bridge" packet, and
the bridge's stories were the wrong length — which took the channel dark for a
further 22 hours. One unobserved failure cost most of a week.

Three things now cover that gap:

| | what it does | where it lives |
|---|---|---|
| **Wednesday writer** | writes the next full week | Claude Routine (`create_trigger`) |
| **Daily catch-up** | writes only the missing stories, and only when the packet is short | Claude Routine, daily |
| **Watchdog** | rings the bell if the packet is short, whatever the Routines did | `.github/workflows/packet-watchdog.yml` |

### The shortfall test

Both the catch-up and the watchdog ask the same question, via

    python -m agent.packet --shortfall

which reports SHORT when `runway_deficit_hours()` is above zero — that is,
when the packet will run out *before its replacement is due*.

It is deliberately **not** "are there 28 stories". Mid-week a healthy packet is
supposed to be half consumed, so a story count would report a shortfall every
single day and the catch-up would never stop. Measuring hours-to-next-write
instead means the condition clears by itself the moment a full week lands,
which is what makes a daily retry terminate.

### Why the catch-up is a Routine and not a workflow

Filling a shortfall means *writing stories*, which needs a model, and there is
no model in this repository. A GitHub Action can detect the hole; only Claude
can fill it. So the workflow alarms and the Routine writes. Keeping the alarm
in Actions is the point: if the catch-up Routine is broken too, the watchdog
still emails, because it does not depend on the thing it is watching.

### The length contract

Whoever writes a packet — Wednesday or catch-up — must read

    python -m agent.packet --length-spec

first. The 22-hour outage happened because the written specification said
"~150 words/min" while the configured voice actually speaks at about 205, so
prose written to spec came out a third too short and the renderer refused it.
That spec is now generated from the same constants the validator enforces, so
the two cannot disagree again.
