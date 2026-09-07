# Faceless YouTube Agent — 100% free, fully automated

Writes a script (Gemini), narrates it (edge-tts), pulls stock footage
(Pexels), edits the video (moviepy/ffmpeg), and uploads to YouTube —
on a schedule, with zero manual steps after setup. Runs on GitHub's
free cloud runners, so your Mac doesn't need to be on.

## One-time setup (~20 minutes, never repeated)

1. **Get a free Gemini API key**: aistudio.google.com/apikey
2. **Get a free Pexels API key**: pexels.com/api
3. **Get YouTube upload credentials**:
   - console.cloud.google.com → new project → enable "YouTube Data API v3"
   - Credentials → Create OAuth client ID → Application type: **Desktop app** → download the JSON
   - On your Mac: `pip install google-auth-oauthlib` then
     `python get_refresh_token.py /path/to/client_secret.json`
   - This opens a browser once — log into the Google account that owns your
     YouTube channel and approve it. It prints three values you'll need next.
4. **Create a GitHub repo** and push this folder to it.
5. In the repo → Settings → Secrets and variables → Actions, add these
   **secrets**:
   - `GEMINI_API_KEY`
   - `PEXELS_API_KEY`
   - `YT_CLIENT_ID`
   - `YT_CLIENT_SECRET`
   - `YT_REFRESH_TOKEN`
   And this **variable** (Variables tab, not Secrets):
   - `NICHE` — e.g. "bizarre history facts", "unsolved mysteries", etc.

That's it. `.github/workflows/daily.yml` runs the whole pipeline every day
at 15:00 UTC (edit the cron line to change the time) and uploads a new
video with no further input from you. You can also trigger a run manually
from the GitHub Actions tab any time ("Run workflow" button).

## Testing locally on your Mac first (recommended)

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
brew install ffmpeg imagemagick

export GEMINI_API_KEY=...
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
(03:30 UTC)** — and emails you the result. It is the only weekly job besides
the Monday niche scan, and it has exactly one cron line; if you want a
different time, edit that line rather than adding a second one.

What it does:

- runs the regression suite (offline, with credentials stripped from the
  environment, the same way `tests.yml` runs it)
- **probes the YouTube OAuth token live** — this is the one worth having. If
  your Google OAuth consent screen is still in Testing mode the refresh token
  dies after 7 days, every upload fails, and nothing else tells you until you
  go and look
- reconciles the quota ledger against the real video records
- reports videos published without a record, videos rendered but never
  uploaded, and any scheduled run that failed in the last 8 days
- rebuilds the dashboard and **publishes it only if something real changed**,
  so a quiet week costs no commit and no deployment

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

- Gemini's free tier and YouTube's free upload quota (~6 uploads/day) are
  both comfortably enough for one video a day.
- `edge-tts` voices list: run `edge-tts --list-voices` to pick a different one.
- Videos upload as `public` by default — change `privacyStatus` in
  `agent/upload.py` to `"private"` if you want to review before publishing.
- This is a generic scaffold — script quality, footage relevance, and pacing
  will need some prompt/config tuning once you see the first few outputs.
