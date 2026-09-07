"""
The publishing cadence, in one place.

WHY THIS MODULE EXISTS

The upload schedule was written down in three places that could drift apart:
the four cron lines in .github/workflows/daily.yml, the UPLOAD_HOURS_UTC list
in agent/dashboard.py, and the "~5 hours apart" reasoning in
agent/checkpoint.py. Nothing tied them together, so a change to the cron would
silently leave the dashboard predicting the old times.

Now that a week of stories is written IN ADVANCE (agent/packet.py) the cadence
stopped being cosmetic: the number of stories a weekly packet must contain is
derived from it. A packet built against the wrong slot list is a packet with
gaps, and a gap is a scheduled run with nothing to publish. So the slot list
lives here, dashboard.py reads it, and packet.py counts it.

THE REAL SCHEDULE, read off daily.yml on 2026-09-07 and confirmed against 30
days of real run history (four scheduled runs a day, every day):

    cron "7 1 * * *"   -> 01:07 UTC = 06:37 Asia/Kolkata
    cron "7 6 * * *"   -> 06:07 UTC = 11:37 Asia/Kolkata
    cron "7 11 * * *"  -> 11:07 UTC = 16:37 Asia/Kolkata
    cron "7 16 * * *"  -> 16:07 UTC = 21:37 Asia/Kolkata

(daily.yml's own comments say "~07:07 IST" and so on. They are half an hour
out — IST is UTC+5:30, not UTC+6. The times above are what actually fires.)

The minute is deliberately :07 rather than :00/:15/:30/:45, which are the most
oversubscribed marks on GitHub's shared scheduler; that decision is documented
in daily.yml and is preserved here.
"""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# Mirrors .github/workflows/daily.yml. Change both together.
SLOT_HOURS_UTC = (1, 6, 11, 16)
SLOT_MINUTE_UTC = 7
SLOTS_PER_DAY = len(SLOT_HOURS_UTC)
DAYS_PER_WEEK = 7
SLOTS_PER_WEEK = SLOTS_PER_DAY * DAYS_PER_WEEK

# The channel is operated from India and every human-facing time in this
# project (dashboard, weekly report, the story packet) is shown in IST.
DISPLAY_TZ = ZoneInfo("Asia/Kolkata")

# How far past a slot's nominal time a run may still claim that slot's story.
# GitHub's scheduler routinely delays a queued run, and the shared
# repo-data-writers lock can hold one behind another; delays of 1-3 hours were
# observed on 2026-07-29/30. A run that starts late is still that slot's run,
# so the window has to be wider than the observed delay and narrower than the
# ~5 hours to the next slot.
SLOT_GRACE_HOURS = 4.0
# And a little the other way, because a run can also start a minute or two
# EARLY relative to the nominal time.
SLOT_EARLY_MINUTES = 15


def slot_id(when: datetime) -> str:
    """The stable identifier for one publishing slot: "2026-09-09T0607Z".

    UTC and to the minute, so the same slot has the same id no matter which
    timezone the writer or the reader is in."""
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H%MZ")


def _slots_on(day: datetime) -> list[datetime]:
    midnight = day.astimezone(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return [midnight.replace(hour=h, minute=SLOT_MINUTE_UTC)
            for h in SLOT_HOURS_UTC]


def slots_between(start: datetime, end: datetime) -> list[datetime]:
    """Every publishing slot in [start, end], in order, as aware UTC datetimes."""
    start = start.astimezone(timezone.utc)
    end = end.astimezone(timezone.utc)
    out = []
    day = start.replace(hour=0, minute=0, second=0, microsecond=0)
    while day <= end:
        for slot in _slots_on(day):
            if start <= slot <= end:
                out.append(slot)
        day += timedelta(days=1)
    return out


def next_slots(after: datetime, count: int) -> list[datetime]:
    """The next `count` slots strictly after `after`.

    This is what a weekly packet is built from: one call with
    count=SLOTS_PER_WEEK gives exactly seven days of publishing, starting at
    the first slot the generating run cannot itself have missed."""
    after = after.astimezone(timezone.utc)
    out = []
    day = after.replace(hour=0, minute=0, second=0, microsecond=0)
    while len(out) < count:
        for slot in _slots_on(day):
            if slot > after:
                out.append(slot)
                if len(out) == count:
                    break
        day += timedelta(days=1)
    return out


def slot_for(now: datetime) -> datetime | None:
    """The slot a run starting at `now` belongs to, or None if it belongs to no
    slot at all (a manual run in the middle of the night, say).

    A run is matched to the most recent slot within SLOT_GRACE_HOURS behind it,
    or to a slot up to SLOT_EARLY_MINUTES ahead of it."""
    now = now.astimezone(timezone.utc)
    window_start = now - timedelta(hours=SLOT_GRACE_HOURS)
    window_end = now + timedelta(minutes=SLOT_EARLY_MINUTES)
    candidates = slots_between(window_start, window_end)
    return candidates[-1] if candidates else None


def local(when: datetime) -> datetime:
    return when.astimezone(DISPLAY_TZ)


def describe(when: datetime) -> str:
    """"Wed 2026-09-09 21:37 IST" — how a slot is written for a human."""
    return local(when).strftime("%a %Y-%m-%d %H:%M IST")
