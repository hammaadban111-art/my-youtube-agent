# Video Reuse Bundles

This folder contains self-contained reuse bundles for previously produced videos.

## What is a Reuse Bundle?
A reuse bundle is a single JSON file storing all the script text, narration timing, voice settings, and visual keyword queries needed to render a video. 

## Key Facts About Reuse Bundles

- **Zero Gemini API Quota Cost:** Because the script, narrative structure, and visual keywords are already written and saved, re-rendering a video from a bundle requires zero Gemini API calls. The voice narration uses edge-tts, which is completely free.
- **Substitute for Rendered Videos:** Rendered `.mp4` video files for past uploads are not kept long-term. A reuse bundle acts as the lightweight master record allowing any video to be re-created from scratch whenever needed.
- **YouTube Description is NOT Saved:** The original YouTube description is not stored in the bundle. A re-upload writes a fresh one from the bundle's own text (`rebuild_description` in `scripts/reupload_video.py`) — its hashtags are what carry the subject into the new video's tags.
- **Legacy Generic Visual Queries:** If a bundle has `visual_queries_are_legacy_generic: true`, its visual keywords were created before our visual query improvements. Rendering this bundle directly will reproduce generic or poorly matched stock footage. The re-upload path detects the flag and regenerates the queries first (one Gemini call, the only one a re-render ever makes).

## Using a bundle: the Replace flow

The dashboard shows a **Replace** button on any video that has a bundle. It
deletes that video from YouTube and re-uploads it from the bundle.

- The dashboard cannot do this itself — it is a static page on a separate
  public repo with no credentials. The button shows a confirmation dialog and
  then sends you to the workflow in this (private) repo.
- Run it with: `gh workflow run reupload.yml -f video_id=<id> -f confirm=<id> -f dry_run=false`,
  or from the Actions tab. `dry_run` defaults to **true** — that renders the
  video and spends no quota, uploads nothing and deletes nothing.
- **Cost: 1,650 quota units** (1,600 insert + 50 delete) out of 10,000/day.
  The workflow refuses if that would eat into the reserve the day's scheduled
  reads run on.
- **The refresh token needs `youtube.force-ssl` to delete.** A token minted
  before 2026-08-05 does not have it. Re-run `get_refresh_token.py` and update
  the `YT_REFRESH_TOKEN` secret first, or the replace will refuse to start.
- The new video is uploaded **before** the old one is deleted, so a failed
  render can never leave you with neither.
