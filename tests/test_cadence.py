"""The publishing cadence is arithmetic that a weekly packet is counted from,
so it gets tested like arithmetic. A wrong slot list means a packet with a gap,
and a gap is a scheduled run with nothing to publish."""
from datetime import datetime, timedelta, timezone

from agent import cadence


def _utc(y, m, d, h=0, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=timezone.utc)


def test_two_slots_a_day_matching_daily_yml():
    slots = cadence.slots_between(_utc(2026, 9, 9), _utc(2026, 9, 9, 23, 59))
    assert [s.strftime("%H:%M") for s in slots] == ["06:07", "11:07"]
    assert cadence.SLOTS_PER_DAY == 2


def test_a_week_is_fourteen_slots():
    week = cadence.next_slots(_utc(2026, 9, 9, 15, 15), cadence.SLOTS_PER_WEEK)
    assert len(week) == 14
    # Seven days of publishing, ending one slot short of the same time a week on.
    assert week[0] == _utc(2026, 9, 10, 6, 7)
    assert week[-1] == _utc(2026, 9, 16, 11, 7)


def test_consecutive_weeks_leave_no_gap_and_no_overlap():
    """The property that actually matters: generating a packet every Wednesday
    from the last slot of the previous one covers every slot exactly once."""
    first = cadence.next_slots(_utc(2026, 9, 9, 15, 15), cadence.SLOTS_PER_WEEK)
    second = cadence.next_slots(first[-1], cadence.SLOTS_PER_WEEK)
    assert not set(first) & set(second)
    everything = cadence.slots_between(first[0], second[-1])
    assert everything == first + second


def test_slot_ids_are_stable_across_timezones():
    slot = _utc(2026, 9, 9, 16, 7)
    assert cadence.slot_id(slot) == "2026-09-09T1607Z"
    assert cadence.slot_id(slot.astimezone(cadence.DISPLAY_TZ)) == "2026-09-09T1607Z"


def test_local_times_are_ist_not_the_comment_in_daily_yml():
    """daily.yml's own comments say ~07:07 IST. They are half an hour out —
    IST is UTC+5:30. This pins the real times."""
    day = cadence.slots_between(_utc(2026, 9, 9), _utc(2026, 9, 9, 23, 59))
    assert [cadence.local(s).strftime("%H:%M") for s in day] == \
        ["11:37", "16:37"]


def test_a_late_run_still_belongs_to_its_own_slot():
    """GitHub's scheduler routinely runs late; delays of 1-3h were observed in
    July 2026. A run two hours behind is still that slot's run."""
    slot = _utc(2026, 9, 9, 6, 7)
    assert cadence.slot_for(slot + timedelta(hours=2)) == slot


def test_a_run_far_from_any_slot_belongs_to_none():
    assert cadence.slot_for(_utc(2026, 9, 9, 21, 30)) is None


def test_a_run_slightly_early_still_claims_the_coming_slot():
    slot = _utc(2026, 9, 9, 6, 7)
    assert cadence.slot_for(slot - timedelta(minutes=5)) == slot
