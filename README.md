# Faceless YouTube Agent — 100% free, fully automated

Narrates a script (edge-tts), pulls stock footage (Pexels), edits the video
(moviepy/ffmpeg), and uploads to YouTube — on a schedule, with zero manual
steps after setup. Runs on GitHub's free cloud runners, so your Mac doesn't
need to be on.

**The scripts themselves are written a week ahead, not at run time.** A weekly
Claude Cowork task researches a full publishing week of stories — sources,
fact-checks, narration, footage queries, thumbnails, metadata — and commits
them to `content/weekly_story_packet.json`. Each scheduled run reads one story
out of that file and renders it. See [Where stories come
from](#where-stories-come-from) below.

## Where stories come from

There is no text-model API key anywhere in this repository, and no story is
generated inside a workflow. The chain is:

```
weekly no-model refresh  ->  content/weekly_editorial_brief.json  (committed)
                                        |
twice-weekly Claude task ->  content/weekly_story_packet.json  (committed)
                                        |
                    .github/workflows/story-packet.yml validates it on push
                                        |
    .github/workflows/daily.yml (2x/day)  ->  agent/packet.py claims one story
                                        |
              grounding -> tts -> visuals -> assemble -> upload -> record
```

- **`content/weekly_story_packet.json`** is the canonical plan: 14 stories,
  one per publishing slot for seven days, each with research, sources,
  per-claim verification, a five-segment script, footage queries, a thumbnail
  prompt and metadata. `agent/packet.py` validates every field against the
  same rules a generated script always had to pass.
- **`content/story_history.json`** is the durable ledger: every story that has
  ever been planned, with its status (`proposed` → `queued` → `published`, or
  `failed`/`skipped`) and a timestamped history. It is what stops a story
  going out twice and what the next weekly task reads to avoid re-proposing a
  subject.
- **If the packet is missing, invalid or exhausted, the run fails loudly and
  publishes nothing.** There is deliberately no fallback generator: inventing
  a story inside the workflow is the failure mode this design removes.

Why: from 2026-08-24 to 2026-09-04, twenty-eight scheduled runs died on
`503 UNAVAILABLE  This model is currently experiencing high demand`. A model
outage at 06:07 UTC cannot be retried into success and there is no second
source of a story at that moment. Researching a week ahead takes the
dependency off the critical path entirely.

Every Wednesday at 20:17 IST, GitHub refreshes the editorial brief before the
20:45 Claude Cowork session. It contains real retention, early-performance and
no-repeat evidence from this channel; it never calls a model or changes an
upload. Hook examples appear only when at least four top, early-distributed
videos carry retention data, so a low-view looping clip cannot become a channel
rule. The command below is also available for a manual refresh.

To write a packet by hand or from a different tool, draft the stories as a
JSON array and run:

```bash
python scripts/build_editorial_brief.py
# Read content/weekly_editorial_brief.md and its full JSON avoid_subjects list.
python scripts/assemble_packet.py drafts.json --packet-id 2026-W38
python -m agent.packet --validate      # the same gate CI runs
```

`assemble_packet.py` works out the slots, mints stable ids, carries forward
any story a previous packet planned that nothing has published yet, records the
fresh brief hash used by Cowork, and refuses to write the file if the brief is
missing, stale, malformed, or the packet would not validate. Each new draft
also needs an `editorial_rationale`: one short sentence saying which brief
signal informed its fresh angle or hook. Kept overlap stories do not need to be
rewritten.

## One-time setup (~20 minutes, never repeated)

1. **Get a free Pexels API key**: pexels.com/api
2. **Get YouTube upload credentials**:
   - console.cloud.google.com → new project → enable "YouTube Data API v3"
   - Credentials → Create OAuth client ID → Application type: **Desktop app** → download the JSON
   - On your Mac: `pip install google-auth-oauthlib` then
     `python get_refresh_token.py /path/to/client_secret.json`
   - This opens a browser once — log into the Google account that owns your
     YouTube channel and approve it. It prints three values you'll need next.
3. **Create a GitHub repo** and push this folder to it.
4. In the repo → Settings → Secrets and variables → Actions, add these
   **secrets**:
   - `PEXELS_API_KEY`
   - `YT_CLIENT_ID`
   - `YT_CLIENT_SECRET`
   - `YT_REFRESH_TOKEN`
   And this **variable** (Variables tab, not Secrets):
   - `NICHE` — e.g. "bizarre history facts", "unsolved mysteries", etc.

That's it. `.github/workflows/daily.yml` runs the whole pipeline **twice a
day** — 06:07 and 11:07 UTC, which is 11:37 and 16:37 IST — and uploads a new
video with no further input from you. (It was four a day until 2026-09-25; see
`agent/cadence.py` for why it was cut.) The slot list lives in
`agent/cadence.py` as well as in the cron lines; change both together, because
the packet's size (2 × 7 = 14 stories) is counted from it. You can also trigger a run manually from the GitHub Actions tab any
time ("Run workflow" button).

## Testing locally on your Mac first (recommended)

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
brew install ffmpeg imagemagick

export PEXELS_API_KEY=...
export YT_CLIENT_ID=...
export YT_CLIENT_SECRET=...
export YT_REFRESH_TOKEN=...
export NICHE="bizarre history facts"

python -m agent.main
```

## Pausing / resuming

The whole pipeline runs on GitHub's cloud servers, not your Mac — normal
daily uploads never touch your laptop's CPU/GPU, so gaming performance
is unaffected. If you still want a manual pause (e.g. going on vacation),
use the included one-command toggle from inside this folder:

```bash
./toggle.sh off      # pause daily uploads
./toggle.sh on        # resume daily uploads
./toggle.sh status    # check current state
```

Optional: add this to your `~/.zshrc` so you can run it from anywhere
without `cd`-ing into the folder first:
```bash
alias ytagent="~/path/to/faceless-youtube-agent/toggle.sh"
```
Then it's just `ytagent off` / `ytagent on` from any terminal tab.

## Weekly maintenance (Fridays)

`.github/workflows/weekly.yml` runs once a week — **Friday 09:00 IST
(03:30 UTC)** — and posts the result on the run's summary page. If anything
needs you, the run goes red and GitHub's own "Run failed" email tells you; a
quiet week stays green. There is no separate email service. It is the only weekly job besides
the Monday niche scan, and it has exactly one cron line; if you want a
different time, edit that line rather than adding a second one.

What it does:

- runs the regression suite (offline, with credentials stripped from the
  environment, the same way `tests.yml` runs it)
- **checks the story packet**: that it is valid, and how many days of stories
  are left. The channel can look perfectly healthy today and be out of content
  on Thursday, and nothing else in the repo would say so
- **probes the YouTube OAuth token live** — this is the one worth having. If
  your Google OAuth consent screen is still in Testing mode the refresh token
  dies after 7 days, every upload fails, and nothing else tells you until you
  go and look
- reconciles the quota ledger against the real video records
- reports videos published without a record (your own hand uploads, listed in
  `data/manual_uploads.json`, are skipped), videos rendered but never
  uploaded, and any scheduled run that failed in the last 8 days
- rebuilds the dashboard and **publishes it only if something real changed**,
  so a quiet week costs no commit and no deployment. Upload and follow-up
  workflows persist source records only; the public website is refreshed here,
  once per week

You can run it by hand from the Actions tab ("Run workflow"), or locally
without touching anything:

```bash
python scripts/weekly_health_check.py --skip-tests
```

## Resuming a failed run

The pipeline checkpoints its expensive stages (script, fact-check, prediction,
and the upload itself) to `workdir/checkpoint/`, which `daily.yml` carries
between runs. If a run dies partway, the next one reuses what already
succeeded instead of paying for it again.

The case that matters most: if a run uploads a video and then dies before
writing its record, the next run notices, writes the missing record, and
stops. It does **not** upload again — the checkpoint makes the upload
idempotent. Checkpoints expire after 5 hours, are discarded if the niche
changed, and are cleared as soon as a run completes.

## Notes / limits

- YouTube's upload quota is 100 videos/day on a separate pool from the 10,000
  Data API units (see `agent/quota.py`), so two uploads a day is nowhere near
  any ceiling.
- `edge-tts` voices list: run `edge-tts --list-voices` to pick a different one.
- Videos upload as `public` by default — change `privacyStatus` in
  `agent/upload.py` to `"private"` if you want to review before publishing.
- This is a generic scaffold — script quality, footage relevance, and pacing
  will need some prompt/config tuning once you see the first few outputs.
