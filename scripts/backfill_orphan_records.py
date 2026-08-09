#!/usr/bin/env python3
"""
Creates records for videos that are live on the channel but have none.

PLAN.md OPEN ITEM #1 ("records can miss real uploads") stopped being
theoretical: the upload succeeds, then the commit step that saves the record
fails, and the runner is wiped. Three videos were published this way on
2026-08-03, 08-05 and 08-09.

An orphan is not a cosmetic gap. It is invisible to predict.py's training
set, to duplicate detection, to the dashboard, and - because
quota.reconcile_uploads works from records - its 1,600 units never reach the
ledger either, so the agent thinks it has more quota than it does.

What can be rebuilt from YouTube is the shell: id, title, description,
upload time, tags, privacy. What cannot is everything generated before the
upload - the segments, the hook candidates, the grounding report, and the
prediction that was made *before* publishing. A backfilled record says so
rather than pretending, and carries no prediction at all: inventing one after
the views are known would quietly corrupt the accuracy history.

Videos that were never produced by this agent are left alone - matched by
being older than the first real record.

Usage:
  python scripts/backfill_orphan_records.py --dry-run
  python scripts/backfill_orphan_records.py --subjects subjects.json
"""
import argparse
import glob
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from googleapiclient.discovery import build

from agent import config, history, quota, store, upload

READONLY = "https://www.googleapis.com/auth/youtube.readonly"


def live_videos() -> list[dict]:
    yt = build("youtube", "v3", credentials=upload._credentials([READONLY]))
    channel = yt.channels().list(part="contentDetails", mine=True).execute()
    playlist = channel["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]

    ids, token = [], None
    while True:
        page = yt.playlistItems().list(part="contentDetails", playlistId=playlist,
                                       maxResults=50, pageToken=token).execute()
        ids += [i["contentDetails"]["videoId"] for i in page["items"]]
        token = page.get("nextPageToken")
        if not token:
            break

    out = []
    for i in range(0, len(ids), 50):
        page = yt.videos().list(part="snippet,status,statistics",
                                id=",".join(ids[i:i + 50])).execute()
        out += page["items"]
    return out


def build_record(video: dict, subject: str) -> dict:
    snippet, status = video["snippet"], video["status"]
    uploaded = datetime.strptime(snippet["publishedAt"][:19] + "Z", "%Y-%m-%dT%H:%M:%SZ")
    uploaded = uploaded.replace(tzinfo=timezone.utc)

    script = {"title": snippet["title"], "description": snippet.get("description", ""),
              "topic_subject": subject}
    record = store.new_record(video["id"], script, uploaded_at=uploaded)
    record["privacy_status"] = status.get("privacyStatus")
    record["niche"] = config.NICHE
    # Deliberately no prediction. The whole point of the stored prediction is
    # that it was made BEFORE the views existed; writing one now, with the
    # actual number already visible, would poison accuracy_summary().
    record["prediction"] = {}
    record["grounding"] = {}
    record["script"] = {}
    record["backfilled_from_youtube"] = {
        "backfilled_at": store.iso(datetime.now(timezone.utc)),
        "reason": "published successfully but the record commit failed; "
                  "rebuilt from the YouTube API",
        "views_at_backfill": int(video.get("statistics", {}).get("viewCount", 0)),
        "tags": snippet.get("tags", []),
        "subject_source": "recovered manually" if subject else "unknown",
        "lost_forever": ["script segments", "hook candidates", "grounding report",
                         "the pre-upload prediction"],
    }
    record["degradations"] = [{
        "stage": "record-backfill",
        "what": "the record was lost when the run's commit step failed after upload",
        "instead": "rebuilt from YouTube; generation-time data is gone",
    }]
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill records for live-but-unrecorded videos.")
    parser.add_argument("--subjects", default=None,
                        help="JSON map of {video_id: topic_subject}")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    subjects = {}
    if args.subjects:
        with open(args.subjects) as f:
            subjects = json.load(f)

    known = {json.load(open(p))["video_id"]
             for p in glob.glob(os.path.join(os.path.dirname(__file__), "..",
                                             "data", "videos", "*", "*.json"))}
    records = store.all_records()
    earliest = min((r.get("uploaded_at", "") for r in records if r.get("uploaded_at")),
                   default="")

    orphans = []
    for video in live_videos():
        if video["id"] in known:
            continue
        if earliest and video["snippet"]["publishedAt"] < earliest:
            print(f"  skipping {video['id']} ({video['snippet']['publishedAt'][:10]}) - "
                  f"predates this agent's first video, not ours")
            continue
        orphans.append(video)

    if not orphans:
        print("No orphaned videos. Every live video has a record.")
        return 0

    print(f"\n{len(orphans)} live video(s) with no record:")
    for v in sorted(orphans, key=lambda x: x["snippet"]["publishedAt"]):
        subject = subjects.get(v["id"], "")
        print(f"  {v['snippet']['publishedAt']}  {v['id']}  "
              f"{v['statistics'].get('viewCount', '?'):>5} views  "
              f"subject={subject or 'UNKNOWN'}")
        print(f"      {v['snippet']['title'][:60]}")
        if args.dry_run:
            continue
        record = build_record(v, subject)
        store.save_record(record)
        history.append_entry(v["snippet"]["title"], subject)

    if args.dry_run:
        print("\nDRY RUN - nothing written.")
        return 0

    added = quota.reconcile_uploads(store.all_records())
    print(f"\nBackfilled {len(orphans)} record(s). "
          f"Quota ledger caught up by {added} units "
          f"(now {quota.units_used_today()}/{quota.DAILY_CAP}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
