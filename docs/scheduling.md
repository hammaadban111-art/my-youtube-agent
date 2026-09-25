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
have.** Until it is configured, nothing is sent, the cron lines keep
running the channel exactly as they do today, and the fallback is simply the
current behaviour. Wiring it in cannot make anything worse.

### Active now (2026-09-23): a LaunchAgent on the owner's Mac

Until a token-backed external scheduler exists, the owner's Mac sends the
dispatch: `~/Library/LaunchAgents/com.hammaad.yt-publish-slot.plist` runs
`~/.local/bin/yt-publish-slot.sh` at 11:37 and 16:37 IST (the two slots
since 2026-09-25) using the `gh` CLI's existing login — no new token, nothing stored
in this repo. A slot missed while the Mac sleeps fires once on wake; when the
Mac is off, the crons below carry the channel as before. Log:
`~/Library/Logs/yt-publish-slot.log`. First manual dispatch, 2026-09-23 05:35Z:
the run started within seconds, against a cron that had been ~5h late daily.

The token-backed scheduler below is still the better fix, because it does not
depend on one laptop being awake.

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

The `cron:` lines stay as the fallback. If the external scheduler dies,
the channel keeps publishing on GitHub's schedule — late, but publishing. Two
triggers firing for the same slot is safe: `agent/packet.py::slot_already_served`
refuses to publish a second video into a slot the ledger already holds, the
shared `repo-data-writers` concurrency group serialises the runs, and
`agent/velocity.py` defers any upload within `MIN_GAP_HOURS` (3h) of the last
one — the late cron plus a carried overdue story put three videos out inside
31 minutes on 2026-09-23 before that floor existed.

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
