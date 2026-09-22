"""
The follow-up measurement check. Entry point for the periodic workflow.

Runs on its own schedule rather than having the upload workflow sleep for
hours, which would burn CI minutes doing nothing.

Each video gets a first reading ~5h after upload, then every eligible video
is re-checked on every single run after that - no tiering by age - until it
turns store.AGE_FREEZE_DAYS old, at which point it gets exactly one more
decision (fresh final reading if there's quota headroom that day, otherwise
an immediate freeze at whatever numbers are already on record) and is never
read again. The first reading is frozen into `measurement` for predict.py to
train on (comparable across videos at a consistent point in their life);
every reading, first or not, updates `latest_measurement` for the next weekly
dashboard build to display as current, alongside its own `measured_at`
timestamp so the dashboard can show how fresh each card's numbers are.

One video failing is logged and skipped rather than aborting the batch, so a
single deleted or unavailable video can't block every other measurement.
"""
import sys

from . import gha, quota, store, youtube_stats


def _take_reading(record: dict) -> None:
    """Fetches and records one fresh reading for a single video, tracking its
    real quota cost as it goes (agent/quota.py). Raises on failure - callers
    decide what that means (skip vs. freeze anyway)."""
    vid = record["video_id"]
    print(f"[followup] Measuring {vid} ({record['title'][:50]})")
    stats = youtube_stats.fetch_stats(vid)
    comments = youtube_stats.fetch_comments_result(vid)
    retention = youtube_stats.fetch_retention(
        vid, uploaded_at_date=record["uploaded_at"][:10])
    # Not tracked against quota - see agent/quota.py: this currently fails
    # during OAuth token refresh, before ever reaching a metered endpoint.

    uploaded = store.parse_ts(record["uploaded_at"])
    now = store._utcnow()
    elapsed = (now - uploaded).total_seconds() / 3600

    reading = {
        "measured_at": store.iso(now),
        # Recorded rather than assumed: if a run was missed, the reading
        # is late and the report should say so instead of calling it 5h.
        "hours_after_upload": round(elapsed, 1),
        **stats,
        # Stored whether or not it worked - an "unavailable + reason"
        # record is the honest state, and is what the dashboard shows.
        "retention": retention,
    }
    # The list stays where every existing record and reader expects it, so 114
    # records written before 2026-09-09 keep working untouched. What is NEW is
    # comments_status beside it: a failed read used to be written here as an
    # empty list, indistinguishable from a video nobody commented on. Only
    # overwrite the stored comments when the read actually SUCCEEDED — a token
    # blip must not erase comments a previous run genuinely collected.
    if comments["available"]:
        record["comments"] = comments["items"]
    record["comments_status"] = {
        "available": comments["available"],
        "reason": comments.get("reason"),
        "disabled": bool(comments.get("disabled")),
        "checked_at": store.iso(store._utcnow()),
    }
    store.record_measurement(record, reading)
    store.save_record(record)
    if not comments["available"]:
        print(f"[followup]   comments unavailable: {comments.get('reason', '?')[:120]}")

    n = len(record["measurement_history"])
    pred = record.get("prediction", {}).get("predicted_views")
    print(f"[followup]   reading {n}: "
          f"predicted={pred} actual={stats['actual_views']} at {elapsed:.1f}h")
    if retention.get("available"):
        print(f"[followup]   retention: biggest drop {retention.get('biggest_drop_size')} "
              f"at {retention.get('biggest_drop_at')} of video length")
    else:
        print(f"[followup]   retention unavailable: {retention.get('reason', '?')[:120]}")


def measure_all(due: list[dict]) -> int:
    """Refreshes every record in `due`, in place, saving each as it goes.
    Shared by the follow-up workflow (which passes every eligible video) and
    main.py (which sweeps right after an upload so that upload's run also
    refreshes every OTHER video's numbers, not just the one that just went
    up). One video failing is logged and skipped rather than aborting the
    batch."""
    measured = 0
    last_err = None
    for record in due:
        try:
            _take_reading(record)
            measured += 1
        except Exception as e:  # noqa: BLE001 - one bad video must not stop the rest
            last_err = e
            print(f"[followup]   FAILED for {record['video_id']}: {type(e).__name__}: {e}")

    # Skipping ONE bad video is the intended behaviour; every video failing is
    # a different event entirely and needs to be said out loud. followup.yml
    # reported success for two days while failing all 36 measurements, because
    # a batch of nothing-but-failures still exits 0 - the "a green workflow run
    # is not evidence of work done" trap in docs/session-handoff-2026-08-11.md.
    if due and measured == 0 and last_err:
        gha.error(
            "Follow-up measured nothing",
            f"All {len(due)} video(s) due for measurement failed. "
            f"Last error: {type(last_err).__name__}: {last_err}",
        )

    print(f"[followup] Measured {measured}/{len(due)}.")
    return measured


def finalize_aged_out(due_final: list[dict]) -> tuple[int, int]:
    """One-shot handling for every video that just crossed AGE_FREEZE_DAYS: a
    fresh final reading if there's quota headroom left today, otherwise an
    immediate freeze at whatever numbers are already on record. Either way
    the video is marked final (store.freeze_record) and never appears in
    due_for_final_refresh() or measurable_records() again - this is a single
    decision per video, not a retry loop that waits for headroom to free up.
    Returns (refreshed, frozen_without_a_fresh_read)."""
    refreshed = 0
    frozen_stale = 0
    for record in due_final:
        vid = record["video_id"]
        if quota.has_headroom():
            try:
                print(f"[followup] Final refresh for {vid} (30 days old)")
                _take_reading(record)
                refreshed += 1
            except Exception as e:  # noqa: BLE001 - freeze at last-known numbers rather than retry forever
                print(f"[followup]   Final refresh FAILED for {vid}: {type(e).__name__}: {e} "
                      f"- freezing at last-known numbers")
                frozen_stale += 1
        else:
            print(f"[followup] Quota headroom too low today ({quota.units_used_today()} units used) "
                  f"- freezing {vid} at last-known numbers without a final read")
            frozen_stale += 1
        store.freeze_record(record)

    if due_final:
        print(f"[followup] Finalized {len(due_final)} video(s) at {store.AGE_FREEZE_DAYS} days: "
              f"{refreshed} got a fresh final read, {frozen_stale} froze at last-known numbers.")
    return refreshed, frozen_stale


def sweep() -> int:
    """One full pass: refresh every eligible (<AGE_FREEZE_DAYS) video, then
    resolve every video that just crossed the freeze line. Shared by the
    follow-up workflow's run() and main.py (called right after an upload)."""
    due = store.measurable_records()
    due_final = store.due_for_final_refresh()

    measured = 0
    if due:
        # Split by tier, because "12 due" means something different when 3 of
        # them are under 48h old and 9 are older ones that came round on their
        # daily turn. The log is the only place the tiering is observable.
        every_run = sum(1 for r in due if store.reading_cadence(r) == "every-run")
        daily = len(due) - every_run
        print(f"[followup] {len(due)} video(s) due for measurement "
              f"({every_run} under {store.FRESH_WINDOW_HOURS}h, {daily} on the daily tier).")
        measured = measure_all(due)
    else:
        print("[followup] Nothing due for regular measurement.")

    if due_final:
        finalize_aged_out(due_final)

    _record_channel_snapshot()

    return measured


def _record_channel_snapshot() -> None:
    """One channel-level reading per sweep, deduplicated to one row per UTC day
    by store.record_channel_snapshot().

    Costs 1 Data API unit against a budget running at ~1.2% utilisation, and it
    is the only way the channel's headline number ever acquires a trend line.
    Never fatal: a failed reading is written down AS a failure, with the real
    reason, so the series can tell "we could not read" from "the number fell"."""
    try:
        stats = youtube_stats.fetch_channel_stats()
    except Exception as e:  # noqa: BLE001 - recorded, never fatal
        reason = f"{type(e).__name__}: {e}"[:300]
        store.record_channel_snapshot(error=reason)
        print(f"[followup] channel stats unavailable: {reason[:120]}")
        return
    row = store.record_channel_snapshot(stats)
    print(f"[followup] channel: {row.get('subscriber_count')} subscribers, "
          f"{row.get('total_views')} total views "
          f"(series row for {row.get('day')})")


def run() -> int:
    measured = sweep()
    return measured


def main() -> int:
    """followup.yml's exit status. Non-zero when videos were due and not one
    reading succeeded, so the workflow goes red and GitHub says so. It used to
    exit 0 through two whole days of 36-for-36 failures (see
    docs/session-handoff-2026-08-11.md)."""
    due = len(store.measurable_records())
    measured = run()
    return 1 if due and not measured else 0


if __name__ == "__main__":
    sys.exit(main())
