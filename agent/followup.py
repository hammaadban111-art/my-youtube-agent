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
every reading, first or not, updates `latest_measurement` for the dashboard
to display as current, alongside its own `measured_at` timestamp so the
dashboard can show how fresh each card's numbers are.

One video failing is logged and skipped rather than aborting the batch, so a
single deleted or unavailable video can't block every other measurement.
"""
from . import dashboard, notify, quota, store, youtube_stats


def _take_reading(record: dict) -> None:
    """Fetches and records one fresh reading for a single video, tracking its
    real quota cost as it goes (agent/quota.py). Raises on failure - callers
    decide what that means (skip vs. freeze anyway)."""
    vid = record["video_id"]
    print(f"[followup] Measuring {vid} ({record['title'][:50]})")
    stats = youtube_stats.fetch_stats(vid)
    quota.record_units(1)
    comments = youtube_stats.fetch_comments(vid)
    quota.record_units(1)
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
    record["comments"] = comments
    store.record_measurement(record, reading)
    store.save_record(record)

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
        notify.alert(
            "Follow-up measured nothing",
            f"All {len(due)} video(s) due for measurement failed.\n"
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
        print(f"[followup] {len(due)} video(s) due for measurement.")
        measured = measure_all(due)
    else:
        print("[followup] Nothing due for regular measurement.")

    if due_final:
        finalize_aged_out(due_final)

    return measured


def run() -> int:
    measured = sweep()
    dashboard.build()
    return measured


if __name__ == "__main__":
    run()
