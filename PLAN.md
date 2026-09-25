# Multi-Phase Backlog Plan

Written 2026-07-31 before any code changes, as the record of what was decided
and why. Reviewed against the backlog's own entry conditions and its central
rule: **do not stack untested changes** — the channel already hit a real
distribution throttle from doing exactly that.

---

## Findings from the planning pass (these changed the plan)

### 1. BLOCKER: impressions/CTR are NOT retrievable via the API

Tested live against the real channel with the correct analytics-scoped token:

| Query | Result |
|---|---|
| `metrics=impressions` | `Unknown identifier (impressions)` — metric does not exist |
| `metrics=videoThumbnailImpressions` (Jan-2026 name) | `The query is not supported` — under every shape tried: `dimensions=day`, `dimensions=video`+`sort`, combined with `views`, and `filters=video==<id>` |
| `metrics=views,estimatedMinutesWatched,subscribersGained` | **query succeeds** — so the scope and API work; returns **no rows** (channel too new, data not processed) |

Impressions/CTR are a **YouTube Studio-only** metric for this channel. This is
not a scope problem and not something more code can fix.

**Consequences, flagged rather than worked around:**

- **Phase 1 item 4** becomes: document it as a manual Studio check, add a
  clearly-labelled "manual check" placeholder on the dashboard. Do **not**
  fabricate or proxy the number.
- **The stated Phase 1 exit condition ("impressions data confirmed
  populating") cannot be met as written.** Proposed substitute, which
  preserves the intent (don't advance on confounded data):
  > 5–7 days of clean on-schedule uploads, no manual dispatch runs, AND
  > `views`/`estimatedMinutesWatched` returning real rows from the Analytics
  > API (currently returning none), AND retention curves populating on at
  > least 3 recent videos.
  Impressions-to-views ratio (Phase 2's first item) moves to a manual Studio
  read, recorded by hand into the phase report.

### 2. Analytics returns no rows at all yet

`views`/`estimatedMinutesWatched` queries succeed but are empty — the channel
is ~2 days old and YouTube hasn't processed analytics. This is consistent with
retention also being empty. **Phase 1 item 6 (confirm retention populating)
cannot be confirmed today** — it becomes a monitoring item, re-checked on a
schedule, not a one-off task.

### 3. Local `.env` holds the OLD refresh token

The new analytics-scoped token went to GitHub Secrets only. Local `.env` still
has the pre-fix token, which is why the first local analytics call in this
session failed with `invalid_scope`. Production (CI) is correct. Local testing
must inject the working token explicitly. **Do not commit the token anywhere;
`.env` is gitignored and stays that way.**

### 4. Root-cause context carried forward

The stuck-views investigation concluded the cause was **upload velocity on a
brand-new channel** (11 uploads in 2 days, incl. a same-hour manual burst),
not a code regression — upload metadata is byte-identical between the
high-performing and stuck eras. This is why Phase 1 item 5 (velocity
guardrail) and item 7 (no manual dispatch) matter more than any code fix here.

---

## Self-review against the "don't stack untested changes" rule

The backlog as written has Phase 1 changing 6 things at once. On a channel
that is *currently throttled*, that reintroduces the exact confound the
staging exists to prevent.

**Revision applied:** Phase 1 items are split by whether they can affect
what YouTube sees.

- **Group A — invisible to YouTube** (record schema, tests, dashboard,
  prediction gating, guardrail warning): safe to land together. They change
  no uploaded artifact.
- **Group B — changes the uploaded video or its metadata** (tags on upload,
  duplicate-topic detection changing topic selection): these alter what gets
  published. Land them **one at a time, separated by at least 48h of real
  uploads**, so if distribution shifts we know which one moved it.

Order: all of Group A first → then tags (B1) → 48h+ → then duplicate-topic
detection (B2). Tags first because it is the more likely upside and the more
reversible of the two.

---

## PHASE 0 — Setup

**Entry: immediate. Cannot affect distribution — no uploaded artifact changes.**

### 0.1 `system_version` tag on every record

- **Files:** `agent/store.py` (add field to `new_record`), `agent/main.py`
  (no change expected — `new_record` covers it), `agent/dashboard.py`
  (surface it per video).
- **Change:** constant `SYSTEM_VERSION` in `store.py`, stamped into every new
  record. Seed value `"phase1-stabilize"`. Backfill existing records with an
  explicit `"pre-phase0"` marker via a one-off script so old data is labelled
  rather than null.
- **What could go wrong:** backfill script corrupts real records; a null
  version on old records breaks dashboard grouping.
- **Verification:** run backfill against a **copy** of `data/` first, diff
  the JSON, confirm only the new key was added and no existing key changed;
  only then run against real data. Re-read every record and assert every one
  has a non-null version.

### 0.2 Regression test suite

- **Files:** new `tests/` directory, `requirements-dev.txt` (pytest).
- **Coverage, one test per real historical bug:**
  1. **Caption timing desync / overlap** — `assemble._caption_chunks` +
     `_assert_no_overlap`: fuzz randomized scripts, assert zero overlaps and
     zero over-width captions. (Re-establishes the 2,000-script check as a
     permanent test.)
  2. **Fact-correction silently failing** — `grounding.apply_corrections`:
     a contradicted claim whose text shares **no substring** with the
     narration must still be corrected via `segment_index`
     (the real K5-7-HTo38M bug). Assert `corrections_applied == 1`.
  3. **Backwards fact-grounding** — `ground_script`'s
     `final_segment_grounded` must be True only when a verdict actually
     carries the final segment's index; assert it is False when the final
     segment has no claim.
  4. **Retention/prediction training boundary** — `predict.predict`: a topic
     with high views but early drop-off must return `confidence == "low"`
     and `early_drop_off == True`; a normal-retention control stays
     `"medium"`. (Locks in this session's change.)
  5. **Measurement freeze boundary** — `store.record_measurement`: first
     reading freezes `measurement`; later readings update
     `latest_measurement`/`measurement_history` but must **never** mutate
     `measurement`. Protects predict.py's training data.
  6. *(added in Phase 1)* tags present on the upload request body.
  7. *(added in Phase 1)* duplicate `topic_subject` detection.
- **What could go wrong:** tests that hit the network become flaky and
  meaningless in an unattended run.
- **Verification:** every test runs offline with fixtures/monkeypatching — no
  Gemini, Pexels, or YouTube calls. Confirm by running the suite with network
  disabled. Deliberately re-introduce each historical bug locally and confirm
  the corresponding test fails (a test that never fails proves nothing), then
  revert.

---

## PHASE 1 — Stabilize

**Entry: immediate.**

### Group A (land together — nothing YouTube sees changes)

#### 1.1 Self-improving mode override + suppression-aware training
- **Files:** `agent/predict.py`, `agent/config.py`.
- **Change:** `SELF_IMPROVE_MIN_DATE` env override (`SELF_IMPROVE_AFTER`) so
  activation can be pushed past the throttle window; `self_improve_active`
  respects it. Separately, a record whose views are near-zero is treated as
  **no signal** — excluded from topic multipliers entirely — rather than as
  evidence the topic is bad. Threshold defined against real data
  (throttle-era videos sit at 0–16 views; healthy ones at ~1,000), so a
  conservative cutoff cleanly separates them without hand-tuning to noise.
- **Risk:** excluding too much leaves the model with no data; excluding too
  little poisons it with throttle noise. Mitigated by requiring a minimum
  sample count before learning activates at all (already exists:
  `MIN_SAMPLES_FOR_LEARNING`).
- **Verification:** simulated dates either side of the override; synthetic
  records at 0/3/16/1000 views asserting which are counted; confirm a
  throttle-era topic does **not** get pushed down in the multiplier.

#### 1.2 Impressions → documented as manual check
- **Files:** `agent/dashboard.py`, `PLAN.md` (this file), `README.md`.
- **Change:** dashboard shows an explicit "impressions: manual check in
  Studio — not exposed by the API" note, with the exact Studio path. No fake
  number, no proxy metric.
- **Verification:** render and read the page; confirm nothing implies a real
  figure exists.

#### 1.3 Upload-velocity guardrail
- **Files:** `agent/store.py` (count recent uploads), `agent/main.py` (check
  before uploading), `.github/workflows/daily.yml`.
- **Change:** at pipeline start, count uploads in the last 24h/48h from
  existing records. Above threshold (>4 in 24h or >8 in 48h — matching the
  intended 4x/day schedule), record a degradation and **abort before
  uploading** rather than merely warning. Aborting is the safe default for an
  unattended run: the cost is one skipped slot, versus deepening a throttle.
  Override env var for a deliberate manual run.
- **Risk:** a miscount silently halts all uploads for days.
- **Verification:** simulate record sets at 3/4/5 uploads-in-24h and assert
  abort/no-abort; confirm the real current record set does **not** trip it;
  confirm the degradation reaches the dashboard.

#### 1.4 Retention population — monitoring, not a task
- **Files:** none (observation only).
- **Change:** re-check on a schedule; record findings. Cannot be "completed"
  today (§2 above).
- **Verification:** live API call each check; log rows returned.

### Group B (land ONE at a time, ≥48h of real uploads apart)

#### B1 — Tags on upload
- **Files:** `agent/main.py`, `agent/upload.py` (already accepts `tags`).
- **Change:** build tags from data the pipeline already has —
  `topic_subject`, niche terms, and the description's existing hashtags —
  deduped, lowercased, length-capped to YouTube's limits.
- **Risk:** malformed/over-long tags rejected by the API → failed upload.
  Irrelevant/spammy tags could *worsen* distribution.
- **Verification:** build the real request body for a real script and inspect
  it **without uploading**; assert tag count/length within limits and that
  every tag is plausibly relevant. Then one real scheduled upload, then check
  the live video's tags via `videos.list`.

#### B2 — Duplicate topic detection (≥48h after B1)
- **Files:** `agent/history.py`, `agent/script_writer.py`.
- **Change:** `history.append_entry` also stores `topic_subject`;
  `load_recent_titles` gains a companion returning normalized subjects;
  `avoid_block` includes them. Widen `MAX_CONTEXT` (currently 30) — at 4
  uploads/day that is only ~7 days, and the real Tamam Shud repeat happened
  inside that window, so the lookback is not the whole problem; normalized
  **subject** comparison is. Widen to 60 anyway for headroom.
- **Risk:** old entries lack `topic_subject` → must not crash.
- **Verification:** feed a history containing "Tamam Shud case" and confirm
  the generated prompt names it as excluded; confirm legacy entries without
  the field are handled; confirm the real repeat that occurred would have
  been caught.

#### 1.7 No manual dispatch runs
Operational, not code. Only the 4x/day schedule runs during Phase 1.

### Phase 1 → 2 exit (REVISED — see §1)
5–7 days clean on-schedule uploads, no manual bursts, **and** Analytics
returning real rows for views/watch-time, **and** retention curves on ≥3
recent videos. Impressions checked manually in Studio and recorded by hand.

---

## PHASE 2 — Learn what's working

**Entry: Phase 1 exit met. Do not start otherwise — report the gap instead.**

- Impressions-to-views ratio: **manual Studio read**, recorded into the phase
  report (API cannot supply it).
- Evaluate whether the production upgrades (Ken Burns, music, captions, hook
  system) actually moved retention — compare `system_version` cohorts, which
  is exactly why Phase 0.1 exists.
- If retention lags, test hook/pacing variants against real cohort data.
- Sanity-check self-improving mode's topic choices for overfitting to noise.

**Exit:** 2–3 weeks of stable post-fix data and a confident, unconfounded
read on the retention trend.

---

## PHASE 3 — Scale and harden

**Entry: Phase 2 exit met (~weeks 3–6).**

- Revisit public-repo-for-unlimited-CI using real render-growth data (~55% of
  2,000 min/month today). Note: making the repo public exposes prompts and
  workflow config — a judgement call to surface, not decide unilaterally.
- Render optimizations before touching upload frequency.
- Track subs/watch-hours toward 1,000/4,000 on the dashboard (`subscribersGained`
  and `estimatedMinutesWatched` are confirmed-valid metrics — they return no
  rows only because the channel is too new).
- Keep watching for near-duplicate topics and data-quality drift.

---

## PHASE 4 — Approach monetization

**Entry: nearing 1,000 subs / 4,000 watch hours (~month 2).**

- Prepare (do not execute) the drop to fewer videos/day.
- Audit that every resilience path has **actually fired for real** at least
  once: Pexels fallback, voice fallback, failed-upload artifact save. Records
  show `degradations: []` on all 11 videos — **none have ever been
  exercised in production.** Plan to force each one in a controlled local run
  rather than waiting to discover a latent bug during an unattended stretch.
- Final report: phase-over-phase retention/view/impression numbers, what
  changed, what is still open.

---

## Standing rules for the unattended run

- Verify by running, never by reading or by trusting a self-report.
- Delegate routine/well-defined work to `agy`; core logic (prediction,
  data schema, anything touching secrets) written and reviewed directly.
- Never print or commit real keys/tokens. `.env` stays gitignored.
- When a phase's entry condition is not met: say so explicitly, do
  maintenance/monitoring, re-check later. Do not skip ahead.
- Write a summary back to the knowledge graph after each phase. **Note:** no
  Graphiti MCP server is connected in this environment — the local
  `graphify-out/` graph (211 nodes, built at `cf89984`, now stale) is the
  available substitute. Phase summaries are appended to this file's
  changelog and the graph re-run when it matters.

## Phase changelog

- **2026-07-31** — Plan written. No code changed yet. Phase 0 starting.
- **2026-07-31** — **Phase 0 complete.**
  - *0.1*: `SYSTEM_VERSION` added to `store.new_record`; all 11 existing
    records backfilled `pre-phase0`; dashboard shows it per video. Backfill
    proved non-destructive against a copy before touching real data
    (all 11 byte-identical with the added key stripped), re-confirmed via
    `git diff`.
  - *0.2*: 17 offline tests in `tests/`, covering all five historical bugs.
    Scaffolding delegated to `agy`; **every test then verified by
    reintroducing its bug and confirming the test fails** — substring
    fact-matching (1 fail), measurement-freeze removal (3 fails), retention
    confidence cap removal (1 fail), final-segment index coercion (1 fail),
    caption timing drift (1 fail, caught a real overlap at 8.3553s vs
    8.2555s). All mutations reverted; suite green and tree clean after.
    `tests.yml` runs them on every push with no API keys present, so the
    offline constraint is enforced rather than assumed.
  - *Deliberately not done*: tests are **not** an upload gate in `daily.yml`
    — a broken test would then halt the channel, a worse failure than the
    regressions being guarded.
- **2026-07-31** — **Phase 1 Group A complete** (nothing YouTube sees changed).
  - *1.1*: throttled videos (≤25 views) excluded from every average, and
    `SELF_IMPROVE_AFTER` brake wired through `daily.yml`, set to
    **2026-08-12**. Caught a live latent bug: the baseline median had already
    collapsed to **5**, so the model was about to predict ~5 views for every
    future video, and `top_performers` was about to start feeding throttled
    videos back into the script prompt as examples to imitate. Post-fix the
    same call predicts 1037.
  - *1.3*: velocity guardrail. Replayed against the real record set — it
    **blocks the 2026-07-30 burst** (8–10 in 24h) while allowing every
    healthy-era upload (3–5) and today's recovery uploads (6). Also fixed a
    bug found in my own new code: `recent_upload_count` was unbounded at the
    recent end, so historical replays counted uploads that hadn't happened.
  - *1.2*: impressions/CTR documented on the dashboard as a Studio-only
    manual check, with the exact API errors. No placeholder number.
  - Suite now **32 offline tests**; all new logic mutation-tested on
    committed code.

### DECISION — Group B is deliberately NOT starting yet

`B1` (tags) and `B2` (duplicate-topic detection) both change what gets
published. The channel is currently **mid-recovery** from the throttle: the
newest uploads are the first clean, on-schedule ones.

Landing a metadata change now would confound the one measurement that
matters most right now — *is distribution recovering on its own?* If views
climb after adding tags, there is no way to tell whether tags helped or the
throttle simply lifted. That is precisely the confound this staged backlog
exists to prevent, and Phase 1's own exit condition asks for 5–7 days of
**clean** uploads.

**Gate for starting B1:** 3+ consecutive on-schedule uploads with no manual
dispatches, and a visible view-count recovery trend (any video clearing the
25-view no-signal threshold). Until then the correct action is monitoring,
not shipping.

**Status at 2026-07-31 ~14:50Z — the throttle appears to be lifting:**

| video | uploaded | views |
|---|---|---|
| `2NiZ95wtNX0` | 07-30 17:47 | 4 |
| `shVrW1NX6QA` | 07-30 20:36 | 1 |
| `FEiE9QJ3hOQ` | 07-30 19:35 | 21 (was 5) |
| `t_D38dcRsrU` | 07-31 04:37 | 10 (was 2) |
| **`TLxlsPYijlo`** | **07-31 13:12** | **462 in ~1.6h** |

The newest video is behaving like the pre-throttle era (~1,000 views), and
the last three upload runs were all `schedule`, no `workflow_dispatch`. The
recovery half of the gate is effectively met; the "3+ consecutive clean
uploads" half needs another day. **Still holding B1** — one strong data
point is not a trend, and adding tags now would confound the very recovery
being measured.

### OPEN ITEM #1 (found 2026-07-31, not yet fixed) — records can miss real uploads

Reconciled the channel's uploads playlist against `data/videos/`:

- **14 videos on YouTube, 12 records.**
- `lbUqcseFke8` (2026-07-31T09:20Z) **was uploaded but never recorded** — the
  09:14 scheduled run failed *after* a successful upload, in the
  `git pull --rebase` inside the "Persist topic history" step
  (`error: could not apply b64bdb6... Record video, prediction and topic
  history`). `daily.yml` and `followup.yml` both commit to `main`, and they
  collided.
- `6uONCytSgmA` (2026-07-29) predates working record-keeping;
  `S0dk8Knhh0g` (2025-01) is unrelated to this project.
- `5Z7wifCabEk` is recorded but **not on the channel** — a record for a video
  that never became visible.

**Why this matters more than it looks:** the velocity guardrail counts
*records*, so an upload that fails to commit is invisible to it. Right now
that makes it read 6 uploads in 24h when the true figure is 7 — which is
exactly its blocking ceiling. The guardrail can therefore undercount in
precisely the situation it exists to catch, and a repeated commit failure
would widen the gap silently.

**Proposed fix (deliberately not applied unattended — needs a real design
decision):** have `velocity.check()` reconcile against the uploads playlist
rather than trusting local records, with the record count as a fallback when
the API call fails. Costs 1–2 quota units per run (negligible against the
10,000/day cap) but adds a network dependency to a pre-render check, so the
failure semantics need deciding: fail-open (upload anyway) risks the burst
this guards against, fail-closed risks halting the channel on a transient
API blip. Leaning fail-open **plus** a recorded degradation, since a missed
block is recoverable and a wrongly-halted channel is what Phase 1 is trying
to end. A separate reconciliation step that back-fills missing records is
probably the better primary fix.

### Monitoring checklist (run on each check-in)

1. `gh run list --workflow=daily.yml` — confirm schedule-only, no
   `workflow_dispatch`.
2. Views on the newest videos — has anything cleared 25?
3. Retention: does `fetch_retention` return rows yet (still empty as of
   2026-07-31)?
4. Analytics `views`/`estimatedMinutesWatched` — still zero rows?
5. Impressions: manual Studio read, recorded by hand.

### Cost ceiling for tracking every video forever (measured 2026-08-01)

The 7-day tracking cutoff was removed, so the number of videos measured each
day now grows for the life of the channel instead of sitting at ~28. Two
budgets bound that, and they are nothing like each other in size.

Per-day work at U uploads/day and T tracked videos: videos under 48h old are
read every run (8 runs/day), everything else once a day.

    readings/day = 8 x (2U) + (T - 2U)      = 56 + T at U=4
    Data API     = 2 units per reading      (videos.list 1 + commentThreads.list 1)

**YouTube Data API — not the constraint.** 10,000 units/day, and `videos.insert`
no longer draws on that bucket at all (it has its own 100 calls/day), so uploads
cannot crowd out measurement.

| tracked videos | units/day | % of bucket |
|---|---|---|
| 100 | 312 | 3% |
| 1,000 | 2,112 | 21% |
| 4,944 | 10,000 | 100% — **ceiling** |

4,944 videos is ~3.4 years at 4 uploads/day.

**GitHub Actions minutes — the real constraint.** This repo is private, so
minutes are metered (2,000/month on the Free plan). Measured from real runs:
`daily.yml` averages 426s over 8 runs, `followup.yml` 30s over 14 runs, of which
the measure step is ~11s fixed plus roughly 1-3s per video read.

    daily.yml alone:            852 min/month of the 2,000
    everything, at 14 videos: ~1,042 min/month (~52%)

| seconds per reading | ceiling | at 4 uploads/day |
|---|---|---|
| 1s | ~2,000 videos | ~17 months |
| 2s | ~970 videos | ~8 months |
| 3s | ~630 videos | ~5 months |

So the horizon is 5-17 months, not 3.4 years, and the per-reading figure that
decides where in that range it lands is currently too noisy to pin down — the
real runs measured 5 readings in 15s and 4 readings in 23s. Re-measure once
there are enough tracked videos for the marginal cost to be visible above the
fixed overhead.

Worth noting the shape of the risk: overrunning Actions minutes stops the
uploads too, because both workflows draw on the same pool. It is not a
measurement-only failure.

**If it needs cutting later**, the cheapest lever is tapering old videos rather
than dropping them: daily for the first month, then weekly. A video 6 months
old moving from 1 read/day to 1 read/week cuts its cost by 86% while still
keeping its total from freezing. Not built — the numbers do not call for it yet.

**Third budget, unquantified:** `fetch_retention` calls the YouTube Analytics
API, a separate API with its own quota whose daily ceiling Google does not
publish (visible only in the Cloud console). Volume there is 56 + T calls/day,
same as the reading count. No quota errors observed in real runs to date.

### External benchmark prior for view predictions (built 2026-08-01)

**What the YouTube API actually exposes for channels we do not own.** Verified
by real calls, not from documentation:

| field | available for other channels? |
|---|---|
| `viewCount`, `likeCount`, `commentCount` | YES — `videos.list` part=statistics |
| duration, publishedAt, title | YES — part=contentDetails/snippet |
| audience retention | NO |
| impressions, CTR | NO |
| views over time / views at age N | NO |

`videos.list` part=`fileDetails` and part=`processingDetails` both return
**403** for a video we do not own. The YouTube Analytics API only accepts
`ids=channel==MINE` or a channel the token manages, so there is no route to
retention or impressions for anyone else's video. Confirmed: only cumulative
lifetime counts are obtainable.

**Comparable channels do not exist within reach.** 1,040 recent Shorts (≤90s)
from 24 niche channels:

- smallest channel found: **38,700 subs**. Ours at the time: **15**.
- views per subscriber ranged **0.002 to 1.024** — a 417x spread, so channel
  size does not predict views per Short well enough to scale down to ours.
- our real median (1,022 views) sits at the **1st percentile** of that pooled
  distribution, and the 2.8th percentile of the sub-1M-subscriber subset.

So "average some comparable channels" was not buildable. What was buildable is
a **floor**: p5 of Shorts from the smallest reachable channels = **1,567 views**.
Grounded in real niche data rather than picked; the exact percentile is only
weakly supported by our own five outcomes and should be revisited as more
arrive.

**Known unit mismatch, deliberately accepted.** The external number is lifetime
views on someone else's Short; what we predict is our views at 5h. The API
exposes nothing that converts between them. They are numerically close at our
current scale by coincidence, and that coincidence expires as the channel grows
— survivable only because the prior is weighted out as our own data arrives.

**Backtest** (walk-forward, each video predicted from only what preceded it,
mean log10 error — "how many orders of magnitude off"):

| sample | old (constant 25) | with prior | |
|---|---|---|---|
| n=10, all measured | 1.900 (79x off) | 1.529 (34x off) | −20% |
| n=5, got distribution | 1.259 (18x off) | 0.419 (3x off) | −67% |
| n=12, all measured | 1.610 (41x off) | 1.300 (20x off) | −19% |
| n=8, got distribution | 1.118 (13x off) | 0.604 (4x off) | **−46%** |

Both rows re-measured two videos later, which is why two samples are shown: the
margin narrows as own-data accumulates, exactly as intended — the blend weights
the prior out. The direction is the guarantee, the magnitude is not. Holds
across K=2..20, so the win is from having a grounded anchor at all rather than
from tuning. These are real measurements on a very small sample, not strong
ones.

**Blend timing.** Weight toward our own data is n/(n+5) on signal-carrying
videos, not elapsed time. Observed rate: 3.2 signal videos/day (5 of 10 were
throttled). So 50% own-data was reached immediately, 75% lands ~3 days out, 90%
~12 days. The "roughly 2 weeks" instinct corresponds to about 90%.

**Quota.** Runtime cost is **zero** — `predict()` reads a committed JSON file.
One-time curation cost 52 units of the 10,000/day bucket plus 2 `search.list`
calls from its separate 100/day bucket. Refreshing the snapshot costs **75 units**
(measured by running it, not derived; the channel list is pinned so there is no
`search.list`): 2.5 units/day averaged if refreshed monthly, 0.03% of the
bucket. `scripts/refresh_benchmark.py --dry-run` reproduces the snapshot
without writing it.

**Also found:** the refresh token does not carry `yt-analytics.readonly` — the
Analytics API returns `invalid_scope` outright. Retention has therefore never
worked on this channel, and `fetch_retention` has been recording "unavailable"
for that reason rather than because YouTube had no rows yet. Re-run
`get_refresh_token.py` to fix. Not fixed here; it is a separate change.

### Niche monitoring: content-type selection, segmentation, honest ranges (2026-08-01)

**The earlier "no small channels are reachable" finding was wrong.** It came
from ordering search results by view count, which returns the biggest videos by
construction. Selecting by content type across eight topic-type queries, using
both `relevance` and `viewCount` ordering and no size filter at all, reached
**410 channels spanning 0 to 60M subscribers — 135 under 10k subs, 89 under
1,000**. The comparables existed; the earlier query could not see them.

**Segmentation by content type does NOT work as a view predictor.** Walk-forward
backtest, mean log10 error, lower is better:

| prior | all n=12 | signal n=8 |
|---|---|---|
| single floor 1,567 (previously shipped) | 1.300 | 0.604 |
| p5 of the wide unfiltered pool | 1.202 | 0.621 |
| **median of sub-1k-sub channels (960)** | **1.202** | **0.515** |
| **per-category p5** | 1.397 | **0.738 — worse** |
| per-category p5, small channels only | 1.280 | 1.120 |

Per-category priors are worse than a single number, and the reason is
measurable: **median subscriber count per category ranged from 1,860
(disappearance) to 988,500 (science_nature)**. A category's raw view numbers
mostly record which channels its query happened to surface, not anything about
the content.

**What does survive is the residual after dividing channel size out** — how a
category performs against same-size peers:

| category | n | vs same-size peers | outside 2 SE |
|---|---|---|---|
| science_nature | 38 | 3.94x | yes |
| historical | 58 | 2.68x | yes |
| unexplained | 118 | 1.07x | **no — not usable** |
| disappearance | 74 | 0.58x | yes |
| murder_coldcase | 100 | 0.46x | yes |

Between-category spread is 0.93 log10; within-category spread is 0.97. The
signal is real and roughly the same size as the noise for any individual video,
so it **ranks kinds of story and cannot forecast one**. Wired into topic
selection as a tilt; never multiplies a predicted view count.

Our own per-category data is 1–5 videos per category (0–3 with signal). Far too
thin to compute anything from; no per-category own-channel signal is computed.

**Anchor moved from 1,567 to 960** — the median Short from a channel under
1,000 subs, a median rather than a tail percentile, and the best performer in
backtest. Note the tension with "stop matching by channel size": video
*selection* is by content type only, as intended, but the absolute anchor still
needs a size band to mean anything. The unfiltered pool's median is 1,457,914
views — 1,400x this channel's reality. An unfiltered pool cannot produce a
usable absolute number. Size conditioning is confined to that one figure.

**Scan cadence: weekly.** One scan costs 16 `search.list` calls of the separate
100/day bucket and ~20 units of the 10,000/day bucket — 2.3 calls and ~3 units
a day averaged. Quota is not the binding factor; detectability is. Bootstrapping
the anchor over 2,000 resamples gives a 95% interval of 624–1,286 (±1.5x from
sampling noise alone), and two scans minutes apart already differed ~6% in kept
Shorts from search churn. Daily would cost 7x to resample the same noise.

**Predictions now display as ranges.** Backtested error is a mean of 3.3x and a
worst case of 199x, so a bare number was claiming precision that does not
exist. Band is ±10x at no own data, narrowing toward ±3.2x as our own sample
grows — the narrowing is structural (the prior carries a lifetime-vs-5h unit
mismatch and is weighted out), not measured, because per-sample-count error
buckets hold two or three videos each. The point estimate is untouched and is
still what accuracy scoring and the blend use.

- **2026-08-05** — **B1 (tags on upload) SHIPPED. B2 still held.**

  **The B1 gate is met, on real data.** Both halves:
  - *3+ consecutive on-schedule uploads, no manual dispatches*: met with room
    to spare. Every `daily.yml` run since 2026-07-31 17:46Z is `event=schedule`
    — 18 consecutive runs, zero `workflow_dispatch`.
  - *Visible view recovery past the 25-view no-signal threshold*: met. Measured
    on the frozen 5h reading, which is the only age-comparable number:

  | window | 5h views |
  |---|---|
  | pre-throttle (07-29 → 07-30 morning) | 999–1069 |
  | throttle (07-30 14:09 → 07-31) | 0–40 |
  | post-recovery (08-01 → 08-04) | 594–1105 on 11 of 13 |

  Recovery sits at roughly 74% of the pre-throttle median (778 vs 1052) — not
  a full return, but unambiguously out of the throttle band, and the confound
  the hold existed to avoid (can't tell tags from throttle-lift) is now gone
  because the recovery has already been measured *without* tags.

  Two outliers, neither throttle-shaped: `SP68Vfly9Qk` (48 at 5h, 85 at 23.5h)
  is a genuinely weak video; `I3h9s6b1fM8` read 1 at 10.6h but 808 later, which
  is delayed indexing, not suppression.

  **B2 is NOT shipping with B1.** Group B's own rule is "land ONE at a time,
  ≥48h of real uploads apart". Shipping both today would rebuild the exact
  attribution problem the throttle hold was about — if the next week's views
  move, tags and dedupe would be indistinguishable. Earliest B2 start:
  **2026-08-07**, after ≥48h of uploads carrying tags.

  **Correction to the record:** B1 and B2 were described as "built but held
  back". They were not. Only the spec existed, plus `upload_video`'s unused
  `tags=` kwarg. `build_tags()` was written for this commit. B2's parts are
  likewise unbuilt — `history.load_recent_titles()` feeds a soft "don't repeat
  these" instruction into the Gemini prompt, which is prompt steering already
  live, not the detection/rejection B2 specifies.

  **Verification** (PLAN's own B1 requirement — request body inspected, nothing
  uploaded): real request bodies built from all 28 stored scripts. Every body
  within YouTube's limits (≤15 tags, ≤500 total chars, ≤100 per tag). Two
  defects that unit tests passed straight over were caught only by running it
  on real data: the niche string leaked the stopword `and` as a tag, and
  `Devil's Kettle (Minnesota)` leaked its Wikipedia disambiguator as
  `devil's kettle (minnesota)`. Both fixed; real output is now
  `["devil's kettle", "minnesota", "unsolved", "mysteries", "bizarre",
  "history"]`. The description-hashtag path stays unit-test-only — descriptions
  are not persisted in video records, so there is no real sample to check.

  **Still unproven:** no video has been uploaded with tags yet. Confirm on the
  next scheduled upload via `videos.list(part="snippet")` that the tags landed.

- **2026-08-05** — **Self-improving mode turned ON by manual approval.**

  Approved by the user directly, deliberately skipping the day-5 approval
  email. `_request_approval_once` is never reached now — `_is_approved()`
  returns True and `self_improve_active` short-circuits before it — so no
  approval email will ever be sent, and `data/self_improve_gate.json` stays
  unwritten. That is the intended outcome, not a bug.

  **Two variables had to change, not one.** Setting `SELF_IMPROVE_APPROVED`
  alone did NOT turn it on. `_hold_until()` is evaluated *before* the approval
  check, and `SELF_IMPROVE_AFTER` was still `2026-08-12`. Verified against the
  exact CI environment: with `AFTER=2026-08-12, APPROVED=true` the gate
  reported `active: False, held_until: '2026-08-12'`.

  `SELF_IMPROVE_AFTER` moved `2026-08-12` → `2026-08-05` rather than being
  deleted, so the variable still records that a brake existed and when it was
  released. Restore the hold at any time by setting it back to a future date;
  it fails closed, so even a typo re-holds.

  **Why releasing the brake a week early was safe — checked on real records,
  not assumed.** The brake existed to stop the model learning from
  throttle-suppressed videos. That protection does not come from the date; it
  comes from `has_signal()`, which is independent and already working. With
  the gate open on the live data (30 records):

  - 25 records carry signal; 5 excluded — `qnAWjYhNlck` (2 views),
    `IyUpBV7GWx4` (4), `2NiZ95wtNX0` (8), `shVrW1NX6QA` (15), and one with no
    reading. Every throttled video is correctly excluded.
  - `top_performers()` — the list actually fed into the script prompt — comes
    back as five genuine 1,099–1,252 view videos (Sodder children, Tunguska,
    Sailing stones, Franklin's lost expedition, MV Joyita). No throttled video
    reaches the prompt.

  So the confound the date brake was protecting against is already handled
  structurally, and `days_of_history` is 6 against a `SELF_IMPROVE_MIN_DAYS`
  of 5.

  **Interaction with B1:** tags shipped earlier today, self-improving mode the
  same day. Group B's "one change at a time" rule is about *published metadata*
  and still holds — B2 remains held to 2026-08-07. But be aware that two
  different levers moved on 2026-08-05, so attributing any view movement from
  here needs care.

- **2026-08-05 (later)** — **Upload quota accounting, B2 built-and-held, and
  the replace button.**

  **Uploads are on the ledger now.** `videos.insert` costs 1,600 units — 16% of
  the daily 10,000 — and `record_units()` was only ever called from
  `dashboard.py` and `followup.py`, so every upload this channel has ever made
  was invisible to it. `quota.record_upload()` books it, keyed by video id so
  the three callers that now book it (the upload path, the re-upload path, the
  reconciliation sweep) can never double-count. `reconcile_uploads()` sweeps
  today's records on every dashboard build, so the ledger self-heals within one
  follow-up cycle. Verified on the real ledger: 179 → 1,779 units, booking
  exactly `5zSVFkP4WVM` (01:56 PT today) and correctly *not* `7-F4mLwE1mI`,
  which was 21:26 PT **yesterday** despite sharing a UTC date. That Pacific
  boundary is the whole reason a naive "today's uploads" sweep would be wrong.

  Two thresholds, deliberately different: `fits_in_cap()` (hard 10,000) gates
  the scheduled upload, because refusing that at 70% would halt the channel
  over a comfort line; `has_headroom_for()` (the existing 70% reserve) gates
  discretionary spend — final refreshes and re-uploads. `main.py` now refuses
  before rendering rather than after.

  **B2 is built, and held to 2026-08-07 as the rule requires.** Subjects are
  recorded from now on (data collection is not a change YouTube sees), the
  lookback widened 30 → 60, and both the prompt-side avoid list and the hard
  rejection switch on at the date. `DUPLICATE_BLOCK_AFTER` fails closed —
  including on an *empty* GitHub Variable, which arrives as `""` rather than
  absent and would otherwise have released it early by omission.

  Replayed over the real 32-entry history, detection flags **2** repeats and
  zero false positives across 29 subjects: the known `Tamam Shud case` pair,
  and a previously unreported one — **`Devil's Kettle` published twice**,
  2026-08-01 (`p2N0xOZxIUA`) and 2026-08-04 (`efMGQGVzh8Y`), under near
  identical titles. That second pair is 3 days apart and well inside the old
  30-entry window, so the prompt-side avoid list *had* the information and the
  model ignored it. Prompt steering alone was never going to be enough.

  On a double miss the run publishes the repeat and records a degradation
  rather than raising. Same reasoning `tests.yml` already states for not being
  an upload gate: a heuristic that silently halts the channel is the worse
  failure.

  **Replace button.** `scripts/reupload_video.py` + `reupload.yml`, reachable
  from a confirmation dialog on each video's row. Uploads the replacement
  *first* and deletes the old video only after that succeeds — the reverse
  order is one failed render away from having deleted a video and having
  nothing to put up. Refuses before rendering when quota is short, when
  velocity is blocking, or when the token cannot delete.

  **Found by running it:** the stored refresh token has **no delete scope**.
  `youtube.upload` + `youtube.readonly` + `yt-analytics.readonly` cannot call
  `videos.delete`. `get_refresh_token.py` now asks for `youtube.force-ssl`, but
  the token must be re-minted before any replace can work. A dry run on
  `qnAWjYhNlck` proved the rest of the path end to end: regenerated visual
  queries (its bundle is the flagged legacy-generic one), edge-tts, Pexels
  re-download, and a real 1080x1920 / 35.8s / 24.6MB H.264 render. The
  regenerated queries returned actual Glenelg jetty footage — the real
  Somerton Man location — in place of the old "fog over coastline".

- **2026-08-08 → 08-11** — **Four-day outage, two root causes, full recovery.**
  Detail was in `docs/session-handoff-2026-08-11.md` (removed from the tree 2026-09-25 when the repo went public; still in git history).

  **Cause 1 (08-07/08): the OAuth refresh token expired**, exactly 7 days
  after minting, because the Google Cloud app is still in *Testing* publishing
  status. Re-minted with `youtube.force-ssl` added. **This recurs every 7 days
  until the consent screen is switched to In production** — the top open item.
  *(Closed 2026-08-15: published to In production. See that entry below.)*

  **Cause 2 (08-09/10/11): git conflicts, not the token.** Those runs uploaded
  successfully and then died committing the record: every run re-measures
  every tracked video, so two overlapping runs conflict on all 40+ record
  files (147 paths in one run). Both workflows now share one concurrency
  group; `scripts/resolve_data_conflicts.py` is the safety net.

  **OPEN ITEM #1 is no longer theoretical.** Three videos were live with no
  record (1,360 views), invisible to predict.py, duplicate detection and the
  quota ledger. `scripts/backfill_orphan_records.py` rebuilds what YouTube
  still knows, with no invented prediction — writing one after the views are
  visible would corrupt the accuracy history.

  **Every dashboard incident read "Unknown error" for weeks.** GitHub's
  job-logs endpoint 302s to Azure and urllib re-sent the Authorization header;
  Azure 401s; the best-effort `except` swallowed it. The log was never read
  for any failure. Fixed, plus plain-English summaries and the failing step
  name — "Run agent" vs "Persist topic history" is the difference between a
  video that never existed and one that is already live.

  **Seven stranded videos resolved**: five published via the new
  `scripts/publish_parked.py`, one dropped as a same-subject duplicate, one
  withdrawn after being published twice by a dedup bug of my own.

  **Four defects were found by running against real data, none by reading
  code**: `gh secret set --body -` silently setting secrets to `-`;
  `can_delete()` reading back its own request; `split_sides()` handling only
  the first conflict hunk; and `PRIVACY_STATUS` defaulting to private on a
  laptop while CI publishes public. The standing rule earns its place again.

  148 → 206 tests.

- **2026-08-15** — **OAuth app published, and the three defects behind the
  last 48 hours of lost slots.**

  **The 7-day token clock is gone.** The Google Cloud consent screen for
  project `youtubve-503911` (client `yt agent desktop`, `667007500818-…`) was
  switched from *Testing* to **In production**. `YT_REFRESH_TOKEN` was set
  2026-08-08T16:54:59Z and would have hard-expired ~2026-08-15T16:55Z; it was
  published with roughly three hours to spare, so the existing token survives
  and no re-mint was needed. The top open item since 08-08 is closed. Cost of
  publishing: the app is now reachable by any Google account and, being
  unverified against sensitive YouTube scopes, shows an "unverified app"
  warning to anyone who is not the owner. Only the owner uses it. The 100-user
  lifetime cap is unchanged (1 used).

  **A single hang cost two upload slots and one follow-up.** Run 31766841885
  (08-14T03:27:12Z) hung in "Run agent" and GitHub killed it at 09:27:28Z —
  exactly its 6-hour default ceiling, against a ~11-minute normal run. Because
  both workflows share the `repo-data-writers` group, it held the lock for six
  hours and GitHub evicted the two runs queued behind it: 31771716659
  (followup, 05:01Z) and 31781223061 (daily, 07:46Z), both with an empty
  `jobs[]` — they never started. Neither workflow set `timeout-minutes`. Now
  45 on daily, 30 on follow-up. The concurrency group is worth keeping; what
  it needed was a bound on how long one member can hold it.

  **35 seconds of backoff was not enough for a Gemini overload.** Runs
  31821999062 and 31882142922 both died on `ServerError: 503 UNAVAILABLE`
  from `script_writer.generate_script` after exhausting all four retries —
  5+10+20s. Widened to 6 attempts with a 160s cap: 10+20+40+80+160 = 310s.
  Affordable precisely because the job now has a 45-minute ceiling.

  **Cancelled runs read "Unknown error" on the dashboard.** `_failed_job()`
  matched only `conclusion == "failure"`, so every cancelled run fell through
  to the placeholder — the 08-14T07:46Z slot said exactly that while this was
  being read. `_cancelled_reason()` now diagnoses from the jobs payload with
  no log fetch at all (an evicted run has no log): empty `jobs[]` means
  evicted before starting, a job spanning ≥5h30m means it hit the 6-hour
  ceiling. Cancelled runs also now appear in the failures panel — they cost
  real slots. Verified against the three real payloads, not fixtures.

  **Found by review, not by the tests that passed:** the first cut keyed the
  Gemini summary off the bare string `503 UNAVAILABLE`, which `gemini_utils`
  prints on *every retry, including ones that then succeed*. A run whose video
  was already live and which died later on a git conflict would have been
  reported as "no video was published for this slot" — the same
  wrong-diagnosis class that hid three live videos until 08-11. The marker is
  now the raised traceback (`genai.errors.ServerError: 503`) and sits below
  the post-upload causes so a conflict wins. Two regression tests lock it in.

  **Verified live, not claimed:** upload quota reads the real 100/day pool on
  every run (`1/100 upload slots used, 138/10000 Data API units`); the
  dashboard publishes fresh (12:58Z) with real causes and per-slot
  published/not-uploaded state; email alerts fire (`Resend responded 200`) on
  the unrotated key. Tiering is live and staying.

  206 → 258 tests.

- **2026-08-18** — **Two-day outage, two independent root causes, plus a
  separate Niche Scan break — all confirmed from real run logs, not
  assumed.**

  Nine upload runs failed between 2026-08-15T16:33Z and 2026-08-17T16:36Z.
  Reading each failure's actual log (not assuming it was another 503)
  split them into two unrelated causes:

  **Cause 1 — the 08-15 OAuth fix didn't survive its own token.** Four
  runs died on `RefreshError: invalid_grant: Token has been expired or
  revoked`, the *first* at 2026-08-15T16:48Z — right at the old 7-day
  expiry the "In production" switch (same day, 14:04Z) was supposed to
  close off. Root cause: a refresh token minted while the consent screen
  is in *Testing* keeps its 7-day expiry forever, even after the screen is
  later published — only a token minted **after** publishing is exempt.
  Publishing the app didn't retroactively fix the token that was already
  live. Fixed by re-minting: new `scripts/remint_yt_token.py` runs the
  existing OAuth flow (now under production status) and pushes the result
  straight to the `YT_REFRESH_TOKEN` GitHub secret via `gh secret set`,
  without ever printing the token — run interactively 2026-08-18, GitHub
  secret's updated-at timestamp confirms it landed.

  **Cause 2 — the 08-15 backoff widening wasn't enough, this time for
  real.** Five separate runs each exhausted all 6 `gemini-flash-latest`
  retries (~5 minutes of backoff) against sustained 503/429 — not a blip a
  longer wait would ride out. `gemini_utils.call_with_retry` now falls
  back to `gemini-flash-lite-latest` (a distinct model id, separate
  serving capacity, still free-tier) once the primary model's own budget
  is exhausted, before giving up for real. Confirmed live before shipping
  (fallback answered instantly while primary was refusing every call) and
  confirmed again by the actual verification run below, where both
  `generate_script` and `verify_claims` hit the exact same exhaustion
  pattern as the five real failures and were rescued by the fallback.

  **Niche Scan (separate workflow, separate bug):** every run failed in
  10s on `ModuleNotFoundError: No module named 'isodate'`. `scan_niche.py`
  imports it; `requirements-followup.txt` (the minimal file `niche_scan.yml`
  installs) never listed it. Added.

  **Verified against a real run, not claimed:** manually dispatched
  `daily.yml` (run 32087451818) after both fixes landed. It reproduced the
  exact primary-model exhaustion pattern live, fell back successfully
  twice, rendered, and uploaded a real video —
  `youtube.com/watch?v=EIFrkYJo2VI` ("The 1935 Mystery of the Burning
  Pilot"), confirmed publicly live by fetching the page title directly,
  not just trusting the workflow's green checkmark. 258 → 259 tests (one
  new fallback-success regression test; the max-attempts test was rewritten
  for the new two-model call count rather than added to).

- **2026-08-19** — **Recovered the 4 videos parked during the 08-16/17
  outage, using the existing `scripts/publish_parked.py`.**

  Downloaded all 4 `unuploaded-video-*` artifacts via `gh run download`.
  Two were the same subject (Derinkuyu underground city) from different
  failed runs — `is_duplicate_subject` does not catch two *unpublished*
  items being compared to each other within one batch (only against
  already-published history), so this needed a manual call: kept the
  08-15 version (stronger hook - the real "chased a chicken" discovery
  detail), dropped the 08-17 one.

  Published the Derinkuyu video by hand first (`PBdJxbjUDwQ`) to satisfy
  "upload one right now." **Real bug hit immediately**: it came back
  `privacyStatus: private`. `daily.yml`'s `PRIVACY_STATUS` env line
  (`${{ vars.PRIVACY_STATUS || 'private' }}`) defaults private unless the
  repo Variable is set — true both on a laptop and in CI, not a
  laptop-vs-CI split as the 2026-08-11 note implied. The repo Variable
  *is* set to `public` (confirmed: the 08-18 verification run published
  correctly through CI), so only this one manual, local, off-CI publish
  needed a manual flip to public after the fact. **Any future by-hand
  `publish_parked.py` run must export `PRIVACY_STATUS=public` first**, or
  fix the video after with `videos().update`.

  For the remaining 2, added `.github/workflows/recover_parked.yml`
  (twice-daily cron, self-limiting since `publish_parked.py`'s own dedup
  makes every run after the videos are handled a no-op) rather than
  publishing both at once. First scheduled fire, triggered manually to
  verify: correctly SKIPPED the Ocean Floor / Torquigener video as
  already covered by a same-subject video published elsewhere in the
  meantime, and published the Silphium one for real (`8ciIGsPAT54`),
  through CI, correctly public with no manual fix needed. Zero parked
  videos left, so the recovery workflow is deleted — it did its one job.

- **2026-09-22** — **Email alerts removed; five bugs fixed; the weekly report
  stops repeating settled items.**

  **Resend / `agent/notify.py` removed entirely, at the owner's request.** No
  code sends mail any more; `RESEND_API_KEY` and `NOTIFY_TO` are gone from
  every workflow. What replaced it is GitHub itself: a failure turns its run
  red, GitHub emails the owner about that, and `agent/gha.py` pins an
  `::error::` annotation on the run page saying what to do (the OAuth-expiry
  instructions, a parked upload needing reconciliation, a follow-up that
  measured nothing). `followup.yml` now exits 1 when every due reading fails;
  the weekly report goes to the run's summary page and the job goes red only
  when something needs a human. The self-improve approval email is gone too —
  `SELF_IMPROVE_APPROVED` still gates the mode, the dashboard says when it is
  waiting. Entries above that mention alert emails describe the old system.

  Bugs, each verified against the live service, not only in tests:

  1. **Comments had never been read.** Every `commentThreads.list` returned
     `403 insufficientPermissions`. google-auth sends the requested scopes on
     refresh, which narrows the access token to exactly those, and the reads
     asked for upload+readonly; comments need `youtube.force-ssl`, which the
     08-18 token already held. Same video, same token, only the requested
     scope changed: 403 before, 3 comments after.
  2. **A retired story's checkpoint wedged the queue.** `reclaim_script`
     ignored `failed`, so a story retired on its first attempt by
     `NarrationLengthError` was re-queued and re-rendered from the checkpoint
     it left; an attempts-exhausted one raised on every run inside the 5h TTL.
     Now `StaleCheckpoint`: the run discards the checkpoint and publishes the
     next due story in the same run.
  3. **"Packet runs dry 4h before the next one is written" fired every week.**
     Real packets end Wednesday 11:07 UTC, four hours before the replacement
     is written — but its first slot, 16:07, is the very next one. The tests
     used a packet ending 16:07, which no real packet does. Now counted in
     slots (`runway_hole`); story-packet.yml fails when a new week leaves one.
  4. **The weekly job's red run was an artifact quota failure** on its
     600-byte report, after every check passed. The report now goes to the job
     summary; no artifact.
  5. **The weekly report repeated settled items:** the two 2026-09-08
     duplicate slots (acknowledged via `reconcile_story_slots.py
     --acknowledge`, both videos kept live), the owner's own unlisted FIFA
     upload `UUdmnQXu-Qo` (now in `data/manual_uploads.json`), ordinary cron
     lag counted as a stuck queue (now only a story >26h late), and failures
     a later run already recovered from (now listed as history).

  Also: backfilled records for `CpFSS82SUCs` (Colossi of Memnon) and
  `3q80NkecVZA` (Derinkuyu), live since the 08-10/11 outage with no record —
  their lost records are how both subjects were published again later. The
  Pexels cache now holds only the generic fallback queries (was 4.1 GB,
  restored in 62s every run for a ~1% hit rate: 1,353 distinct queries, 18
  reused). TTS gets a second rate-repair pass before a story is retired.

- **2026-09-23** — **Full-codebase bug sweep: every source file, workflow and
  script re-read line by line; 21 defects fixed.** Each has a regression test
  in `tests/test_bug_sweep_2026_09_23.py` that fails against the old code (28
  of its 30 tests did; the other 2 are guards). Verified live where a service
  was involved, as noted.

  Publishing and recovery:
  1. **A bookkeeping error could upload a video twice.** The quota-ledger write
     after a successful `videos.insert` sat inside the retry ladder, so a disk
     or JSON error there looked like a failed insert and the same render was
     uploaded again. Ledger writes can no longer raise into the ladder.
  2. **One guardrail hit wedged a parked video for good.** Recovery claimed the
     bundle BEFORE the pace/quota checks, so a refusal left a claim that every
     later run read as "may already be live". Guards now run first.
  3. **A failed recovery upload copied the render into a second bundle** and
     left the original claimed in front of it forever. Recovery no longer
     re-parks: a refused insert releases the claim (safe to retry), an
     ambiguous one flags the original bundle for a human.
  4. **Only the oldest parked bundle was ever looked at**, so an
     already-published or needs-a-human bundle hid every one behind it.
  5. **daily.yml only saved the parked-upload cache while a bundle was waiting**,
     so the run that published the last one saved nothing and the next restore
     brought the published bundle back — a full dependency install every run
     and, with no story due, a red run. Now saved after every agent run.
  6. **A stale publish checkpoint could re-record a live video** (blanking its
     readings, duplicating its topic entry) if a run died between writing the
     record and clearing the checkpoint. Now detected and discarded.
  7. Out of upload slots: the render is now parked instead of thrown away.
  8. `publish_parked.py --force` did not override the stale claim the
     pipeline's own error message tells you to use it on. Now it does.

  Fact-checking and data quality:
  9. **Corroboration matched substrings**: "war" inside "software", "ship"
     inside "relationship". Now whole words (plural/possessive folded). On this
     week's four remaining real stories old and new agree exactly, so nothing
     real was lost.
  10. **"Operation Paul Bunyan" was checked against the folk-hero article
      "Paul Bunyan"** (0/5 supported). Wikipedia's own top hit, "Panmunjom axe
      murder incident", is reached through a redirect with that exact name, but
      shares no word with it and was discarded. Search now reports redirect
      titles (`srprop=redirecttitle`); live: now the right article, 5/5.
  11. **The dashboard said "Facts verified" on 85 videos; 64 are.** It counted
      every claim the article never mentioned as confirmed. Partly confirmed
      videos now read "N/M facts confirmed".
  12. The topic classifier matched inside words ("Toward the Award" was
      historical via "war"). Now anchored to word starts; 1 of 165 real titles
      changes (a "Backwards" false match), coverage 64.8%, above the 60% gate.
  13. A payoff-line contradiction with a string `segment_index` slipped past the
      blocking rule; and when it did block, the story went back in the queue to
      fail twice more. Now integer-compared and retired at once.
  14. Records, topic history, channel history and capability files are written
      atomically — a torn record used to stop every later run.
  15. If the narrator voice failed partway, one video mixed two voices. The
      narration is now redone in the one voice that works.

  Reporting and maintenance:
  16. **CI minutes counted only daily.yml and followup.yml.** Live: 1,315 of
      2,000 used this month (65.8%), about 50 more than the old figure.
  17. The merge-conflict resolver corrupted files when one side of a conflict
      was empty, crashed on one bad file, and could not merge
      `channel_history.json` or `analytics_capabilities.json` (a conflict there
      failed all five push attempts). Verified on a real git rebase conflict.
  18. Re-uploads dropped each segment's fallback footage query and the original
      voice, and recorded the house TTS rate instead of the one used.
  19. Dashboard: the replace dialog printed "undefined upload"; the search box
      lost focus on every 30-second refresh; charts logged invalid-SVG errors.
  20. niche_scan.yml and reupload.yml had no timeout, so a hang could hold the
      shared writer lock for six hours.
  21. Smaller: a misleading "no story due" error when every due story was a
      duplicate; a validation message naming the wrong status; a crash on a
      whitespace-only subject; Pexels URL boilerplate earning relevance points.

  **Follow-up the same day (owner: "do whatever you think is best"):**
  `assemble_packet.py` now carries up to one day (4) of the previous packet's
  stories whose slots passed unpublished, in front of the new week, instead of
  dropping them (2026-W39 had skipped six researched stories this way). The
  number of drafts the Wednesday session writes is unchanged: they sit before
  the window, and `validate_packet` counts and gap-checks only the window.
  Simulated against the real W39 packet and ledger: 32 stories, valid.

  Also found: the "slow" 06:37 IST upload is GitHub's cron, which started that
  slot ~5h late every day. `repository_dispatch` (docs/scheduling.md) started a
  run within seconds when sent.
