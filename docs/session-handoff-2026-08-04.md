# Session Handoff — YouTube Agent Dashboard Work (2026-08-04)

Project: Faceless YouTube Shorts automation, niche "unsolved mysteries and bizarre history".

- Private repo: `hammaadban111-art/my-youtube-agent`, local clone `~/my-youtube-agent/faceless-youtube-agent`
- Public dashboard repo: `hammaadban111-art/yt-agent-dashboard`, live at hammaadban111-art.github.io/yt-agent-dashboard
- Channel: youtube.com/@histoyandmystery, 23-24 real subscribers
- Full detail: `~/.claude/projects/-Users-hammaad/memory/project_youtube_agent.md` (read first — this file is a condensed pointer). Also `MEMORY.md` index and `feedback_github_mcp_preference.md`.

## What shipped this session (4 rounds, all live in production)

### Round 1 — Cadence import, dashboard rebuild

User imported a Claude Design mockup ("Cadence Console"). Full rebuild delegated to `agy` per spec, verified in a real browser against real data. One bug `agy` left was fixed: the status strip showed a fake "FAILED" instead of "unavailable" when `last_run.available === false`.

Real backfill: reconciled local `data/videos/` against the real YouTube uploads playlist — found 2 real gaps, 1 unrelated leftover video (excluded), 1 phantom record (quarantined to `data/quarantine/`, not deleted).

Email-approval gate for self-improving mode: `agent/notify.py` (stdlib `urllib`, Resend API — found and fixed a Cloudflare 403 on urllib's default User-Agent), gated behind new `SELF_IMPROVE_APPROVED` GitHub repo Variable, one-time email, verified with a real send.

### Round 2 — Refresh every video, IST everywhere

Removed the old tiered recheck cadence (3h/daily by age), replaced with `store.measurable_records()`: every eligible video refreshed on every run (both `daily.yml` via `main.py` and `followup.yml`). Added a per-video "updated Xh ago" freshness label. Pinned every timestamp on the page to `Asia/Kolkata` via `Intl.DateTimeFormat` (JS `Date` getters default to the viewer's local timezone otherwise).

### Round 3 — 30-day freeze and quota ledger

User asked for the real quota math before implementing, not an estimate. Real cost: 2 units/reading (`videos.list` + `commentThreads.list`; retention costs 0 since it fails on OAuth token refresh before hitting a metered endpoint) x 12 runs/day. The old "refresh forever" policy scaled with total lifetime video count — real ceiling ~600-700 videos.

Built `agent/quota.py` (self-tracked ledger, Pacific-day-boundary reset — Google exposes no live quota API) and `store.AGE_FREEZE_DAYS = 30`: past 30 days a video gets one final decision (fresh read if headroom is under 70% of the 10k cap, else freeze at last-known) via `store.due_for_final_refresh()` / `store.freeze_record()`, wired through `followup.finalize_aged_out()`.

New steady state: ~2,888 units/day forever (bounded by uploads/day x 30, not lifetime count) — under 29% of cap, holds to ~13.8 uploads/day. Verified with a real API call against a time-simulated 31-day-old video in an isolated temp dir.

Caught own bug: a test called `freeze_record()` for real without mocking `DATA_DIR` and wrote a stray file into the actual repo — found via `git status`, fixed before committing.

### Round 4 — Plain English, new chart, likes/comments/summary

User re-sent item 1 (30-day freeze) verbatim — confirmed already shipped, no rework.

- "GROUNDED 5/5" -> "Facts verified"; "N CONTRADICTED" -> "N fact(s) corrected before upload" (tooltip clarifies pre-upload); degradation jargon -> plain one-liners (`plainDegradation()` map covers all 5 real stages: upload-velocity, youtube-upload, edge-tts, pexels, backfill); footer debug text -> "Last refreshed / Next check" in IST.
- New "Channel Total Views Over Time" chart at the top, reconstructed exactly from each video's `history` field (not bucketed). Fig. 1 moved unchanged to the bottom. Found and fixed x-axis label overlap: index-distance dedup broke on bursty/clustered timestamps, needed an actual pixel-distance check; fixed on both charts.
- Likes/comments added to every video card, plus a new channel-summary strip (subscribers / total views / total likes / total comments) at the very top. Checked first that subscriber count was not already fetched (it was not), added `youtube_stats.fetch_channel_stats()` (1 unit).
- Caught a real bug: `summary.total_views` had summed each video's frozen 5h reading instead of the latest — fine for accuracy scoring, wrong for a summary total. Fixed to sum `latest_measurement`.

Then the user caught a process failure: all 4 items were reported done but `TaskUpdate` was never called, so the task list still showed 4 pending. Re-verified against the actual live repo via the GitHub API (not memory), confirmed real, fixed the task list.

**Lesson for next session:** always update the task tracker in the same turn as the work, and when asked "is X done", re-check the live source rather than answering from memory.

## Key architecture facts

- Data: `data/videos/YYYY-MM/<video_id>.json`, one file per video (git-mergeable; two workflows write concurrently).
- `public/data.json` is gitignored (regenerated every run, race-condition history). Only `public/index.html` commits to the main repo. Both files are copied to the separate public dashboard repo by `scripts/publish_dashboard.sh` (SSH deploy key).
- Two workflows: `daily.yml` (4x/day, uploads and generates) and `followup.yml` (every 3h, measures). Both call `dashboard.build()` at the end.
- GitHub MCP has no access to the private repo (only sees the public `yt-agent-dashboard`) and has no delete/rename operation at all — use plain `git`/`gh` CLI for the private repo, per explicit user instruction.
- Real GitHub push occasionally fails with `remote: fatal error in commit_refs` (transient; hit twice this session, a bare retry fixed it both times). The workflows' own push step does NOT retry.
- Retention data is permanently broken (`invalid_scope` on OAuth refresh) — known, pre-existing, not fixed.
- Verification pattern established and expected: real browser clicks via claude-in-chrome, real API calls where feasible, cross-check totals manually, fetch the actual latest commit via `gh api` before claiming anything is live. Never trust memory for "is it shipped" questions.

## Round 5 — video-quality audit: four pipeline fixes (commits `5df5d60`, `c915bab`)

A quality audit reported four defects. Two had a different root cause than reported, and the fact-check defect turned out to have four holes rather than one. All fixes are pipeline-level and were verified against three real end-to-end renders (no upload), not by reading the code.

### 1. Audio splice edges — partly a non-issue
`tts._trim_and_fade()` already trimmed silence and applied 15ms fades to every sentence clip, plus 140ms pads. The genuinely unprotected spots were `assemble._segment_clip()`'s hard `subclip()` truncation and the segment-to-segment join. Added `AUDIO_EDGE_FADE = 0.015` there. Measured on the finished mp4: first 15ms silent, next 200ms at −35.7 dBFS against a −5.3 peak.

### 2. Caption flicker — duration, not word count
Captions were already 3 words per chunk. Nothing enforced a minimum on-screen time, and `CAPTION_MIN_TICK = 0.001` explicitly permitted a 1ms caption. First real render: **16 of 46 captions under 0.6s, down to 0.277s** ("Radar", "freezing"). Added `CAPTION_MIN_SECONDS = 0.6` with neighbour merging, and — the actual fix — the merge steps the font size down (never below `CAPTION_MIN_FONTSIZE`) when the merged text would otherwise overflow. `_fit_caption_text`'s "splitting is preferred over shrinking" preference is right for layout and wrong for a caption too brief to read at any size. Later render: shortest 0.583s, with four chunks just under the floor because merging them would break the 4-word cap (accepted, documented trade-off).

### 3. Irrelevant B-roll — a prompt instruction, not a search bug
`PROMPT_TEMPLATE` explicitly demanded a "GENERIC, common stock-footage scene" and gave "fog over hills, stormy ocean, candle in dark room" as models. So a Tanzanian soda lake honestly queried "still lake" and Pexels correctly returned snowy alpine lakes. Replaced with a subject-anchored `visual_query` (3-6 words, validated against `BANNED_GENERIC_QUERIES`) plus a `visual_fallback`, a relevance floor on search results, and a four-rung ladder that records a degradation whenever the anchored query is not what was used. A relevance miss is deterministic, so it now skips `resilience.retry` (new `dont_retry_on=`) instead of burning three Pexels calls re-asking. Real renders produce `lake natron tanzania red water`, `ol doinyo lengai volcano tanzania`, `lake baikal siberian frozen ice aerial` — no fallback rung used.

### 4. Fact-checking — four holes, and the biggest one found by luck
A render happened to generate the Lake Natron script itself, hook: *"Animals that fall into this glassy lake turn into stone."* Grounding returned **5 claims, 0 supported, 5 SILENT, status "checked"** — which the dashboard renders as "Facts verified".

Cause: `topic_subject` was `"Lake Natron calcification phenomenon"`, a descriptive phrase rather than an article title, and Wikipedia's search returned **"Tibesti Mountains"** — a mountain range in Chad and Libya. Every claim was graded against it.

Fixed across four fronts:
- verbatim narration now reaches the verifier, not just the model's own mild paraphrase of what it wrote;
- a `MISLEADING` verdict replaces the instruction that whitelisted "different wording or extra detail" (which is exactly how sensationalism reads);
- candidate articles are **ranked** — exact title match, then meaningful-token overlap, then raw-word overlap, then search order — with a narrowed-subject retry (`"Lake Natron calcification phenomenon"` → `"Lake Natron"`) and per-claim entity lookups for facts living in other articles;
- `source_relevant`, `coverage` and `unverified_source` are reported so an all-SILENT result can never render as verified.

Article selection verified live afterwards — all five picked the wrong article before:

| subject | before | after |
|---|---|---|
| Dyatlov Pass incident | Chivruay Pass incident | Dyatlov Pass incident |
| Tunguska event | Tunguska | Tunguska event |
| Lead masks case | Face masks during the COVID-19 pandemic | Lead masks case |
| Roanoke Colony disappearance | American Horror Story: Roanoke | Roanoke Colony |
| Lake Natron calcification phenomenon | Tibesti Mountains | Lake Natron |

### Two API behaviours that only a live call could reveal

**MediaWiki refuses to batch whole-article extracts.** `prop=extracts&explaintext` with several pipe-separated titles and `exlimit=max` returns 200 OK with the warning `"exlimit" was too large for a whole article extracts request, lowered to 1` and empty extracts for every page but one. Measured: `Dyatlov Pass incident|Chivruay Pass incident|Devil's Pass` returned Chivruay with 2033 chars and the top-ranked article with **zero**. Every unit test passed while the script was grounded against the wrong incident. Fix: one whole-article call for the primary source plus one batched `exintro` call for the rest — two requests regardless of article count. `exlimit>1` is only honoured alongside `exintro`.

**Wikipedia rate-limits rapid calls (HTTP 429)**, and a bare `except Exception: return []` was laundering that into "no source found" — a rate-limited run would have shipped with the fact-check silently never having run. Now retried with backoff and propagated to the existing error report. When probing `fetch_source` by hand, pace calls 6-8s apart or you measure the rate limiter instead of the code.

### CI has no ffmpeg
`requirements-dev.txt` is deliberately minimal, so a test that makes pydub encode/decode mp3 passes locally and fails on Actions with `FileNotFoundError: 'ffmpeg'` — which is what happened on `5df5d60`. Keep audio tests in memory. Reproduce CI-only breakage locally with `env PATH=/usr/bin:/bin ./venv/bin/python -m pytest -q`.

### State after this round
Tests 86 → 130, seven new test files, no network in any of them. CI green on `c915bab`. Delegation: `agy` handled rounds 1-6, but could not fix the MediaWiki batching bug — its tests passed against fixtures that assumed batching worked — so that fix and its tests were written directly.

**Not yet proven:** `MISLEADING` has never fired on a live script; it is unit-tested only. The dashboard badge changes reach the public site on the next `daily.yml` / `followup.yml` run and have not been visually confirmed there.

## Current repo state

All work pushed to `main`, published to the live dashboard site, CI (`tests.yml`) green. No known open bugs. `data/quarantine/` holds 1 phantom record with a README explaining why. Local clone: `~/my-youtube-agent/faceless-youtube-agent`. `.env` is present locally with real credentials (never print them — an earlier grep mistake this session exposed `RESEND_API_KEY` in plaintext; rotation was recommended, unresolved whether it happened).
