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
