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
