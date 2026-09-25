"""
Upload-velocity guardrail.

On 2026-07-30 this channel published 8 videos in one day - several from
manual test dispatches, two of them 42 minutes apart - and its distribution
collapsed from ~1,000 views per video to 0-16, where it stayed. The upload
metadata was byte-identical before and after, so the cause was velocity on a
young channel, not a content or code regression.

Nothing in the pipeline noticed. Each run only knew about itself, so there
was no point at which "this is the fifth upload today" was even a
representable thought. This module is that missing check.

It ABORTS rather than warns. For an unattended pipeline the cost of a false
abort is one skipped slot, recoverable on the next run; the cost of a false
proceed is deepening a throttle that has already taken days to recover from.
Those are not symmetric, so the default is the cautious one.
"""
import os

from . import resilience, store

# Calibrated against this channel's own real numbers rather than picked for
# roundness. Was 7 when the schedule was 4x/day (4-5 in any rolling 24h, the
# 2026-07-30 throttle day peaked at 10). Since 2026-09-25 the schedule is
# 2x/day, which yields 2-3 in a rolling 24h once a late run is counted; 4 is
# one catch-up above that and still well short of anything burst-shaped.
MAX_UPLOADS_24H = 4

# The 24h ceiling alone never stopped a CLUSTER. On 2026-09-23 three videos
# went out at 05:41, 05:54 and 06:12 UTC (the Mac's punctual dispatch, the
# late GitHub cron for an earlier slot, and an overdue carried story, each a
# legitimate run on its own) and drew 36, 87 and 41 views; 2026-09-07 had
# three inside 27 minutes. Three in half an hour was always within "7 a day".
# Slots are 5h and 19h apart, so a 3h floor never touches the schedule — it
# only makes the second run of a cluster wait for the next slot, with its
# story still due.
MIN_GAP_HOURS = 3.0

# The 48h figure is reported and flagged but deliberately does NOT block.
# Right after a burst, 48h cannot distinguish "still bursting" from "burst
# yesterday, back on schedule today" - both read 12 on this channel's real
# data. Blocking on it would halt the normal schedule as punishment for
# history the pipeline can no longer do anything about, which is the opposite
# of what this guardrail is for.
WARN_UPLOADS_48H = 6

# Escape hatch for a deliberate manual run. Deliberately requires an explicit
# value rather than mere presence, so a stray empty env var can't disable the
# guardrail by accident.
OVERRIDE_ENV = "ALLOW_UPLOAD_BURST"


class VelocityBlocked(RuntimeError):
    """Raised instead of uploading when recent volume looks like a burst."""


def override_active() -> bool:
    return os.getenv(OVERRIDE_ENV, "").strip().lower() in {"1", "true", "yes"}


def check(now=None) -> dict:
    """Returns a report of recent upload volume. Raises VelocityBlocked when
    it exceeds the ceilings and no override is set.

    Called before rendering, not before uploading, so a blocked run costs
    ~zero CI minutes rather than the ~8 minutes a full render takes.
    """
    last_24h = store.recent_upload_count(24, now)
    last_48h = store.recent_upload_count(48, now)
    since_last = store.hours_since_last_upload(now)
    report = {
        "uploads_last_24h": last_24h,
        "uploads_last_48h": last_48h,
        "limit_24h": MAX_UPLOADS_24H,
        "warn_48h": WARN_UPLOADS_48H,
        "override": override_active(),
        "elevated_48h": last_48h >= WARN_UPLOADS_48H,
        "hours_since_last": since_last,
        "min_gap_hours": MIN_GAP_HOURS,
    }

    # Advisory only - surfaced on the dashboard, never blocks.
    if report["elevated_48h"]:
        resilience.record_degradation(
            "upload-velocity",
            f"{last_48h} uploads in the last 48h (elevated, threshold "
            f"{WARN_UPLOADS_48H})",
            "proceeding - 48h volume is advisory, only the 24h ceiling blocks",
        )

    if last_24h >= MAX_UPLOADS_24H and not report["override"]:
        raise VelocityBlocked(
            f"Upload blocked to protect distribution: {last_24h} uploads in "
            f"the last 24h (ceiling {MAX_UPLOADS_24H}). This channel was "
            f"throttled once already for exactly this - it peaked at 10 in "
            f"24h on 2026-07-30 and view counts collapsed from ~1,000 to "
            f"under 20. Set {OVERRIDE_ENV}=1 to publish anyway."
        )
    if (since_last is not None and since_last < MIN_GAP_HOURS
            and not report["override"]):
        raise VelocityBlocked(
            f"Upload deferred to keep uploads spaced out: the last one went "
            f"out {since_last * 60:.0f} minutes ago (minimum gap "
            f"{MIN_GAP_HOURS:g}h). Clustered uploads on 2026-09-07 and "
            f"2026-09-23 each drew under 90 views. The story stays due for "
            f"the next slot. Set {OVERRIDE_ENV}=1 to publish anyway."
        )
    return report
