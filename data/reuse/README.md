# Video Reuse Bundles

This folder contains self-contained reuse bundles for previously produced videos.

## What is a Reuse Bundle?
A reuse bundle is a single JSON file storing all the script text, narration timing, voice settings, and visual keyword queries needed to render a video. 

## Key Facts About Reuse Bundles

- **Zero Gemini API Quota Cost:** Because the script, narrative structure, and visual keywords are already written and saved, re-rendering a video from a bundle requires zero Gemini API calls. The voice narration uses edge-tts, which is completely free.
- **Substitute for Rendered Videos:** Rendered `.mp4` video files for past uploads are not kept long-term. A reuse bundle acts as the lightweight master record allowing any video to be re-created from scratch whenever needed.
- **YouTube Description is NOT Saved:** The original YouTube description is not stored in the bundle. If you re-upload or re-publish a video, a new description must be written manually.
- **Legacy Generic Visual Queries:** If a bundle has `visual_queries_are_legacy_generic: true`, its visual keywords were created before our visual query improvements. Rendering this bundle directly will reproduce generic or poorly matched stock footage. You should update or regenerate the `visual_keywords` before re-rendering flagged bundles.
