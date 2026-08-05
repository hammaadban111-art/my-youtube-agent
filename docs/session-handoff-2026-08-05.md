# Session handoff — 2026-08-05

Continues `docs/session-handoff-2026-08-04.md`. Read that one for the video
pipeline fixes (audio fades, caption flicker, anchored B-roll, Wikipedia
grounding). This file covers only what changed on 2026-08-05.

Repo is on `main`, everything below is pushed, CI green, 148 tests pass.

## What shipped

**`c840ab9` — cleanups.** Removed the duplicate `15 */3 * * *` cron in
`followup.yml` and committed the previously untracked
`docs/session-handoff-2026-08-04.md` and `.agents/`.

**`0838630` — B1: tags on upload, plus a quota-accounting fix.**

**`4a6109f` — self-improving mode turned on by manual approval.**

**`c58971a` — reuse bundles for weak videos, and render archiving.**

## Findings that changed decisions

### Retention was never actually broken any more

The old `invalid_scope` / HTTP 500 story is dead. `YT_REFRESH_TOKEN` was
re-minted **2026-07-31 06:00Z** carrying `yt-analytics.readonly`, and 14 of 26
records now hold real 100-point retention curves. There are **zero** HTTP 500s
anywhere in the records. The remaining "unavailable" entries all say
*"Analytics returned no retention rows yet"* — that is YouTube's own ~3-day
processing lag, not a permission problem.

**The local `.env` on the Mac still holds the pre-07-31 token** and still fails
`invalid_scope`. Only affects manual local runs; CI is fine. Re-run
`get_refresh_token.py` if local retention is ever needed. Not urgent.

Consequence: `quota.py` had been under-counting. Its comment claimed
`fetch_retention` never reached a metered endpoint, which stopped being true on
07-31, so every reading cost 3 units while the ledger recorded 2.
`UNITS_PER_READING` is now 3.

### The B1 gate was met, but B1 was never built

The gate from 07-31 asked for 3+ consecutive on-schedule uploads and a visible
view recovery. Both were met with room to spare — every `daily.yml` run since
2026-07-31 17:46Z is `event=schedule` (18 consecutive, no `workflow_dispatch`),
and on the age-comparable 5h reading:

| window | 5h views |
|---|---|
| pre-throttle (07-29 → 07-30 morning) | 999–1069 |
| throttle (07-30 14:09 → 07-31) | 0–40 |
| post-recovery (08-01 → 08-04) | 594–1105 on 11 of 13 |

About 74% of the pre-throttle median.

**Correction to the earlier record:** B1 and B2 were described as "built but
held back". They were not — only the spec existed, plus an unused `tags=`
kwarg. `build_tags()` was written this session. B2 is likewise unbuilt: what
exists is a soft "don't repeat these topics" line in the Gemini prompt, which
is prompt steering already live, not detection.

**B2 is still held, to 2026-08-07 at the earliest.** Group B's own rule is one
change at a time, ≥48h apart.

### Running code on real data caught what tests missed — twice

Unit tests passed clean on `build_tags()` while the real output was wrong. The
niche string emitted the stopword `and` as a tag, and
`Devil's Kettle (Minnesota)` emitted its Wikipedia disambiguator intact. PLAN's
own B1 risk note is that spammy tags could *worsen* distribution — so the tests
were green on the exact failure being shipped. Both fixed, then re-verified
across all 28 stored scripts.

Same pattern on the artifact step in `daily.yml`: as generated it used
`workdir/*.mp4` with no `if:` guard, which would have archived ~15 B-roll clips
(~40MB) per run on a **private** repo with capped Actions storage, and fired on
failures too. Now `workdir/final_video.mp4` with `if: success()`.

Keep doing this. Reading the diff is not enough.

### Self-improving mode needed TWO variables, not one

Setting `SELF_IMPROVE_APPROVED=true` alone did **not** turn it on.
`_hold_until()` is evaluated *before* the approval check and
`SELF_IMPROVE_AFTER` was still `2026-08-12`. Verified under the exact CI
environment: `active: False, held_until: '2026-08-12'`.

`SELF_IMPROVE_AFTER` moved `2026-08-12` → `2026-08-05` rather than being
deleted, so the variable still records that a brake existed and when it came
off. It fails closed — a typo re-holds.

Releasing it a week early was checked against live records, not assumed. The
protection the brake stood for comes from `has_signal()`, which is independent:
of 30 records, 25 carry signal, all four throttled videos (2, 4, 8, 15 views)
are excluded, and `top_performers()` returns five genuine 1,099–1,252 view
subjects. No suppressed video reaches the script prompt.

No approval email will ever fire — `_is_approved()` returns True and
`self_improve_active` short-circuits before `_request_approval_once`.

**Still unverified:** activation on a real scheduled run. No pipeline run had
executed between the change (08:19Z) and the end of the session. Check the
first `schedule` run after that and confirm the new video record carries
`prediction.self_improve_active == true`.

### Recoverability audit — 6 weak videos, all partially reusable

Under 350 views within the first 48h: `qnAWjYhNlck` (2), `IyUpBV7GWx4` (4),
`2NiZ95wtNX0` (8), `shVrW1NX6QA` (15), `FEiE9QJ3hOQ` (48), `SP68Vfly9Qk` (79).
Five are the 07-30 throttle window; `SP68Vfly9Qk` is post-recovery and just
weak.

**Every rendered `.mp4` is gone** — the Actions artifact list is empty
(`total_count: 0`), because `daily.yml` only preserved renders on `failure()`.
So nothing qualifies as full reuse.

All six are **partial reuse, at zero Gemini cost**: records already store
per-segment narration and `visual_keywords`, plus title, voice and TTS rate.
Proven by re-synthesizing a real bundle's first segment through edge-tts —
56,880 bytes of audio, zero Gemini calls.

Gaps: the **YouTube description was never persisted** anywhere, and Pexels clip
ids were not either, so B-roll needs re-downloading (15 clips per video; all 30
stored queries missed the local cache).

Two things worth acting on:
- `qnAWjYhNlck` trips `visual_queries_are_legacy_generic` — its stored queries
  predate the anchored-visual fix, so re-rendering as-is would reproduce the
  generic footage that fix removed. The flag only catches the 8 banned queries,
  so it under-reports; others are generic in spirit but slip through.
- `qnAWjYhNlck` and `2NiZ95wtNX0` are **both "Tamam Shud case"**, uploaded 3.5h
  apart. Real evidence that B2 duplicate-detection is worth building.

**The real bottleneck is upload quota, not generation.** YouTube charges 1,600
units per upload against a 10,000/day cap, so six re-uploads is ~9,600 units —
essentially a full day. And `record_units()` is only called from `dashboard.py`
and `followup.py`, so **uploads are not tracked in the ledger at all**. Fix
that before any bulk re-upload.

## Storage that now exists

- `scripts/export_reuse_bundle.py` — `<video_id>...` or
  `--all-below 350 --within-hours 48`.
- `data/reuse/<video_id>.json` — committed, 31KB for all six.
- `data/reuse/README.md` — plain-English.
- `daily.yml` archives future renders on success, 14-day retention.

Deliberately **not** built: the delete/re-upload button and its confirmation.

## Open items

1. **`RESEND_API_KEY` was never rotated.** Hard evidence: the GitHub secret was
   last updated **2026-07-29 15:14Z**; the plaintext exposure happened
   **2026-08-04**. The exposed key is still live. Only the user can rotate it,
   on resend.com, then update the repo secret. Low blast radius (send email as
   the domain; no YouTube access) but still outstanding.
2. **Confirm self-improving mode activated** on the first real scheduled run.
3. **B2 duplicate-topic detection** — earliest 2026-08-07.
4. **Uploads are missing from the quota ledger** (1,600 units each, untracked).
5. **Local `.env` YouTube token is stale** (pre-07-31, no analytics scope).

## Tooling note

The graphify skill was **not installed** at `~/.claude/skills/graphify/` for
most of this project, which is why the doc/semantic graph pass never ran.
Installed on 08-05 via `graphify install --platform claude`.

There are **no hooks configured** — `hooks: {}` in both
`~/.claude/settings.json` and `settings.local.json`. The "automatic post-task
graphify update" rule in `~/.claude/CLAUDE.md` is an instruction to the model,
not harness automation. Nothing runs unless the assistant chooses to run it,
and **PLAN.md is never auto-updated** — every entry is hand-written.

`graphify update` also reports freshness unreliably: after `c58971a` it printed
"No code-graph topology changes detected" and left the report stamped at an
older commit, while `graph.json` did contain the new nodes. Don't trust the
"Built from commit" line; probe `graph.json` directly.
