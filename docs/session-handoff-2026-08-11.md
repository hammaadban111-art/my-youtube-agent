# Session handoff — 2026-08-11

Written to be read **cold**. This plus `PLAN.md`'s changelog is enough to
continue. Covers 2026-08-08 → 08-11: a four-day outage, its two root causes,
and the recovery.

Earlier context, only if you need it:
- `docs/session-handoff-2026-08-05b.md` — project overview, standing rules,
  how to use the knowledge graph. **Read this one if you are new to the repo.**
- `docs/session-handoff-2026-08-05.md`, `docs/session-handoff-2026-08-04.md`

---

## Current state (2026-08-11 ~11:50Z)

- Branch `main` @ `40df3de` + measurement commits. **206 tests pass** (was 148 on 08-05).
- **50 video records.** Every live video on the channel has a record.
- Quota 3,501/10,000 for the current Pacific day. Upload pace 6/7 in 24h.
- **B2 duplicate-topic detection is ACTIVE** (its hold date, 2026-08-07, has passed).
- Parked backlog: **empty**. All seven stranded videos resolved.
- Knowledge graph rebuilt — `graphify-out/`, gitignored, rebuild after a fresh clone.

## What actually happened

### The outage (08-07 → 08-08): the OAuth token expired

`invalid_grant: Token has been expired or revoked`. The secret was minted
2026-07-31T06:00:55Z; the last successful upload was 08-07T04:03Z and the
first failure 08-07T07:47Z — almost exactly **7 days** after minting.

That is the signature of a Google Cloud OAuth app still in **Testing**
publishing status: Google hard-expires its refresh tokens after 7 days.

Re-minted on 08-08 with all four scopes including `youtube.force-ssl`, which
also unblocked the replace button. **This recurs every 7 days until the OAuth
consent screen is switched from Testing to In production.** That is the single
most important open item.

### The second outage (08-09 → 08-11): git conflicts, not the token

Different failure entirely, and the runs were *uploading successfully* before
dying:

```
CONFLICT (content): Merge conflict in data/quota_ledger.json
data/videos/2026-08/qPliOoK8qjs.json: needs merge
 ! [rejected]  main -> main (non-fast-forward)
```

**147 conflicted paths.** Every run calls `followup.sweep()`, which re-measures
and rewrites *every* tracked video record, so two overlapping runs conflict on
all 40+ record files at once. The upload succeeded; only the commit that saves
the record failed — and the runner is then wiped.

Fixed two ways: both workflows now share one `concurrency` group so they
cannot overlap, and `scripts/resolve_data_conflicts.py` is the safety net for
what that cannot cover (a push from a laptop).

### Consequence: three videos were live with no record

`PLAN.md` OPEN ITEM #1 stopped being theoretical. `jqJ3RvlekIk` (08-03, 641
views), `K2F8t86cI9Y` (08-05, 100), `Ooy5CkNlnXs` (08-09, 619) — 1,360 views
invisible to `predict.py`, to duplicate detection, and to the quota ledger,
which is why the ledger read 156 units when a 1,600-unit upload had happened.

`scripts/backfill_orphan_records.py` rebuilds them from the YouTube API and is
explicit about what cannot be rebuilt: no segments, no hook, no grounding, and
**deliberately no prediction** — inventing one after the views are visible
would corrupt the accuracy history. Subjects were recovered from Actions logs,
where the grounding step prints the article it checked against.

### The dashboard said "Unknown error" for every incident, for weeks

One line caused it. GitHub's job-logs endpoint does not return the log — it
302s to a pre-signed Azure blob URL, and `urllib` re-sent the `Authorization`
header to the redirect target. Azure rejects that with
`HTTP 401: Server failed to authenticate the request`, and
`recent_failures()` catches log-fetch errors as best-effort, so the 401 was
swallowed. **The log was never read, for any failure.**

Now: redirects drop the header, the extractor prefers plain English keyed on
failures this project has actually had, and it prefixes the failing **step** —
`Run agent` means no video was published; `Persist topic history and video
record` means the video is live and only bookkeeping broke.

### The seven stranded videos

Rendered but never uploaded during the outage, preserved as Actions artifacts.
`park_for_next_run`'s docstring promised "a later run can publish it" but no
code ever read the directory. `scripts/publish_parked.py` is that path.

All seven are now resolved: **five published**, one dropped as a same-subject
duplicate (`Yamal Peninsula` twice — the first run recorded nothing, so the
next run's duplicate detection was blind to it), and one withdrawn (see below).

`park_for_next_run` now saves the whole script, so a future parked video keeps
its subject instead of needing log archaeology.

## Mistakes made in these sessions — worth knowing about

1. **`gh secret set --body -` does not read stdin.** It sets the secret to the
   literal string `-`. I broke all three YouTube secrets this way, which
   changed the error from `expired or revoked` to `invalid_grant: Bad Request`
   and then `invalid_client`. Correct form: **omit `--body` entirely** and pipe
   the value on stdin. In the process I overwrote the repo's original
   `YT_CLIENT_ID`/`YT_CLIENT_SECRET`; secrets are write-only so **the Jul-31
   values are gone**. All three now come from the Jul-29 client in `.env`,
   verified working.
2. **A duplicate video was published** (`3uRPvUI9vaI`, same bundle as
   `Iz-ULqySj2A`). `publish_parked.py` deduplicated on *subject*, and that
   bundle's grounding had failed so it had no subject to match on. It is now
   **private**, its record marked `withdrawn`. Delete it properly if you want.
   The script now keys on the bundle's `parked_at` stamp.
3. **`can_delete()` was wrong when shipped.** It refreshed with a narrow scope
   list and read `granted_scopes` back, but Google echoes only what the refresh
   *asked for* — it was answering its own question. Widening the request is not
   the fix either: a request for scopes outside the grant is rejected outright.
   Each scope is probed alone now.
4. **`split_sides()` only handled the first conflict hunk.** The synthetic test
   passed; the real 5-file conflict failed instantly. Every mistake in this
   list was caught by running against real data, never by reading code.

## Open items, priority order

1. ~~**Switch the OAuth consent screen from Testing to In production.**~~
   **DONE 2026-08-15**, about three hours before the 08-08 token would have
   expired. Project `youtubve-503911` is now In production, so the 7-day clock
   is gone and the existing token survived — no re-mint was needed.
2. **`RESEND_API_KEY` is still not rotated** (secret stamped 2026-07-29,
   plaintext exposure 2026-08-04). It also means none of these failures ever
   emailed anyone — four days of outage went unnoticed.
3. **A token failure should fail fast and alert.** `invalid_grant` is
   permanent, not transient, but the code retries it three times and calls it
   "transient". Depends on item 2 to be useful.
4. **`3uRPvUI9vaI`** is private, not deleted. Decide.
5. Backfilled and recovered records carry no prediction and no script, by
   design. `predict.py` sees fewer training rows than the record count suggests.

## Tools added in these sessions

```bash
# publish videos that rendered but never uploaded (guardrail-checked, stops cleanly)
python scripts/publish_parked.py <dir> --subjects <map.json> --max 2 [--dry-run]

# rebuild records for videos live on YouTube with no record
python scripts/backfill_orphan_records.py [--subjects <map.json>] [--dry-run]

# resolve a rebase conflict in the committed data files (used by both workflows)
python scripts/resolve_data_conflicts.py

# replace a weak video from its stored bundle (needs the delete scope)
gh workflow run reupload.yml -f video_id=<id> -f confirm=<id> -f dry_run=false
```

## Traps

- **The local `.env` has `PRIVACY_STATUS` empty**, so `config` defaults to
  `private` while CI uploads `public`. Always `export PRIVACY_STATUS=public`
  before publishing from a laptop, or the video goes up invisible.
- **`public/data.json` is gitignored.** The live dashboard data is in the
  separate public repo (`hammaadban111-art/yt-agent-dashboard`); reading the
  local file tells you nothing.
- **Quota resets at midnight *Pacific*, not UTC.** A video uploaded at 04:26Z
  was charged to the previous Pacific day. Any "today's uploads" filter on UTC
  dates is wrong by up to 8 hours.
- **A green workflow run is not evidence of work done.** `followup.yml`
  reported success for two days while failing all 36 measurements, because
  `measure_all` skips a failing video rather than aborting.
- Everything else in `docs/session-handoff-2026-08-05b.md` still applies —
  especially the graph rules and "verify by running it".
