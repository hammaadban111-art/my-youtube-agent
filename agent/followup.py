"""
The 5-hour follow-up check. Entry point for the hourly workflow.

Runs on its own schedule rather than having the upload workflow sleep for
five hours, which would burn CI minutes doing nothing.

Each run finds videos that have passed the measurement age without being
measured and records their real view count and comments. One video failing
is logged and skipped rather than aborting the batch, so a single deleted
or unavailable video can't block every other measurement.
"""
from datetime import timezone

from . import dashboard, store, youtube_stats


def run() -> int:
    due = store.pending_measurement()
    if not due:
        print("[followup] Nothing due for measurement.")
        dashboard.build()
        return 0

    print(f"[followup] {len(due)} video(s) due for measurement.")
    measured = 0

    for record in due:
        vid = record["video_id"]
        try:
            print(f"[followup] Measuring {vid} ({record['title'][:50]})")
            stats = youtube_stats.fetch_stats(vid)
            comments = youtube_stats.fetch_comments(vid)
            retention = youtube_stats.fetch_retention(
                vid, uploaded_at_date=record["uploaded_at"][:10])

            uploaded = store.parse_ts(record["uploaded_at"])
            now = store._utcnow()
            elapsed = (now - uploaded).total_seconds() / 3600

            record["measurement"] = {
                "measured_at": store.iso(now),
                # Recorded rather than assumed: if a run was missed, the reading
                # is late and the report should say so instead of calling it 5h.
                "hours_after_upload": round(elapsed, 1),
                **stats,
                # Stored whether or not it worked - an "unavailable + reason"
                # record is the honest state, and is what the dashboard shows.
                "retention": retention,
            }
            record["comments"] = comments
            store.save_record(record)

            pred = record.get("prediction", {}).get("predicted_views")
            print(f"[followup]   predicted={pred} actual={stats['actual_views']} "
                  f"at {elapsed:.1f}h")
            if retention.get("available"):
                print(f"[followup]   retention: biggest drop {retention.get('biggest_drop_size')} "
                      f"at {retention.get('biggest_drop_at')} of video length")
            else:
                print(f"[followup]   retention unavailable: {retention.get('reason', '?')[:120]}")

            measured += 1
        except Exception as e:  # noqa: BLE001 - one bad video must not stop the rest
            print(f"[followup]   FAILED for {vid}: {type(e).__name__}: {e}")

    dashboard.build()
    print(f"[followup] Done. Measured {measured}/{len(due)}.")
    return measured


if __name__ == "__main__":
    run()
