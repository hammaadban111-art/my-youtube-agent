"""One slot, one video — and the pre-flight rules that protect a slot.

The incident: on 2026-09-08 slots 0607Z and 1107Z each published TWO videos.
Packet 2026-W37-bridge was claimed early by three manual dispatch runs inside 27
minutes; packet 2026-W38 then reused the same slot ids, and because eligibility
and mark_published were keyed on story_id alone, the ledger lookup for the new
story found nothing and it published on top.

Validation could not have caught it: it compares a packet against ITSELF, and
these were two different packets. The check has to compare against every video
the channel has actually published.
"""
from datetime import datetime, timedelta, timezone

import pytest

from agent import cadence, checkpoint, packet

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)


def _ledger(entries):
    return {"schema_version": 1, "stories": entries}


def _published(story_id, slot, video_id):
    return {"story_id": story_id, "slot": slot, "status": "published",
            "video_id": video_id, "published_at": "2026-09-08T06:10:00Z",
            "topic_subject": story_id}


# ------------------------------------------------------------ slot uniqueness

def test_a_slot_another_story_published_is_reported_as_served():
    led = _ledger({"st-a": _published("st-a", "2026-09-08T0607Z", "VID-A")})
    served = packet.slot_already_served("2026-09-08T0607Z", "st-b", ledger=led)
    assert served is not None
    assert served["video_id"] == "VID-A"


def test_a_story_does_not_block_its_own_slot():
    """The resume path re-checks the slot for the story that already holds it.
    Treating that as a conflict would refuse to finish writing its own record."""
    led = _ledger({"st-a": _published("st-a", "2026-09-08T0607Z", "VID-A")})
    assert packet.slot_already_served("2026-09-08T0607Z", "st-a", ledger=led) is None


def test_an_empty_slot_is_free():
    led = _ledger({"st-a": _published("st-a", "2026-09-08T0607Z", "VID-A")})
    assert packet.slot_already_served("2026-09-08T1107Z", "st-b", ledger=led) is None


def test_a_queued_but_unpublished_story_does_not_hold_a_slot():
    """Only a PUBLISHED video occupies a slot. A story that was claimed and then
    failed must not lock the slot forever."""
    led = _ledger({"st-a": {"story_id": "st-a", "slot": "2026-09-08T0607Z",
                            "status": "queued"}})
    assert packet.slot_already_served("2026-09-08T0607Z", "st-b", ledger=led) is None


def test_the_real_2026_09_08_double_publish_is_detected():
    led = _ledger({
        "st-voynich": _published("st-voynich", "2026-09-08T0607Z", "SvucVWynzzo"),
        "st-zanzibar": _published("st-zanzibar", "2026-09-08T0607Z", "TkShsZMSML0"),
        "st-wow": _published("st-wow", "2026-09-08T1107Z", "gkLPvVVLPAU"),
        "st-uvb": _published("st-uvb", "2026-09-08T1107Z", "1HYnamc7s4A"),
        "st-emu": _published("st-emu", "2026-09-09T0107Z", "XczaZWrHnRU"),
    })
    dupes = packet.duplicate_published_slots(ledger=led)
    assert set(dupes) == {"2026-09-08T0607Z", "2026-09-08T1107Z"}
    assert len(dupes["2026-09-08T0607Z"]) == 2
    # A slot with exactly one video is not a duplicate.
    assert "2026-09-09T0107Z" not in dupes


def test_no_duplicates_reads_as_empty():
    led = _ledger({"st-a": _published("st-a", "2026-09-08T0607Z", "VID-A")})
    assert packet.duplicate_published_slots(ledger=led) == {}


# ------------------------------------------------------------------- runway

def _story(slot_utc, status="proposed", sid=None):
    # slot_utc is the packet's own compact slot format: "2026-09-09T1607Z".
    return {"story_id": sid or f"st-{slot_utc}", "status": status,
            "slot": {"iso": slot_utc, "utc": slot_utc}}


def test_runway_measures_to_the_last_unpublished_slot():
    pkt = {"stories": [
        _story("2026-09-09T1607Z"),
        _story("2026-09-11T0107Z"),
    ]}
    hours = packet.runway_hours(pkt, now=NOW)
    # 2026-09-11T01:07Z is 37.1 hours after 2026-09-09T12:00Z.
    assert 37.0 < hours < 37.2


def test_published_stories_do_not_extend_the_runway():
    pkt = {"stories": [
        _story("2026-09-09T1607Z"),
        _story("2026-09-30T0107Z", status="published"),
    ]}
    hours = packet.runway_hours(pkt, now=NOW)
    assert hours < 5   # only the unpublished 16:07 slot counts


def test_an_exhausted_packet_has_no_runway_at_all():
    """None, not 0.0. 'Nothing left' and 'everything left is overdue' are
    different problems and only one of them is an emergency."""
    pkt = {"stories": [_story("2026-09-08T1607Z", status="published")]}
    assert packet.runway_hours(pkt, now=NOW) is None


def test_an_entirely_overdue_packet_reports_zero_not_negative():
    pkt = {"stories": [_story("2026-09-01T0107Z")]}
    assert packet.runway_hours(pkt, now=NOW) == 0.0


def test_the_alert_threshold_is_two_publishing_days():
    assert packet.RUNWAY_ALERT_HOURS == 48.0


# ------------------------------------------------------------------ lateness

def test_lateness_is_measured_from_the_slot():
    slot = datetime(2026, 9, 9, 11, 7, tzinfo=timezone.utc)
    assert cadence.lateness_minutes(slot, slot + timedelta(minutes=166)) == 166.0


def test_an_early_run_is_not_late():
    slot = datetime(2026, 9, 9, 11, 7, tzinfo=timezone.utc)
    assert cadence.lateness_minutes(slot, slot - timedelta(minutes=5)) == 0.0


def test_the_late_threshold_sits_above_the_measured_median():
    """The measured median delay was 166 minutes over 62 runs. A threshold
    inside the normal range would fire on more than half of all runs and be
    ignored within a week."""
    assert cadence.LATE_RUN_ALERT_MINUTES < 166
    assert cadence.LATE_RUN_ALERT_MINUTES >= 60


# ----------------------------------------------------------- recovery probe

def test_recovery_is_pending_when_a_video_was_uploaded_but_never_recorded(monkeypatch, tmp_path):
    """The state that stranded three videos in August. daily.yml gates the
    pipeline on a story being due, and this story is no longer selectable —
    so `due` is false exactly when the recovery matters."""
    monkeypatch.setattr(checkpoint, "CHECKPOINT_DIR", str(tmp_path / "cp"))
    monkeypatch.setattr(checkpoint, "STATE_PATH", str(tmp_path / "cp" / "state.json"))
    monkeypatch.setattr(checkpoint, "PENDING_UPLOAD_DIR", str(tmp_path / "pending"))
    monkeypatch.setattr(checkpoint, "load", lambda: [])
    monkeypatch.setattr(checkpoint, "has", lambda name: name == "publish")
    assert checkpoint.recovery_pending() is True


def test_recovery_is_pending_when_a_rendered_video_is_parked(monkeypatch, tmp_path):
    pending = tmp_path / "pending"
    pending.mkdir()
    (pending / "bundle.json").write_text(
        '{"title": "A parked story", "video_file": "final.mp4"}')
    (pending / "final.mp4").write_bytes(b"not really an mp4")
    monkeypatch.setattr(checkpoint, "PENDING_UPLOAD_DIR", str(pending))
    assert checkpoint.parked_bundle_waiting(str(pending)) is True


def test_a_claimed_bundle_is_not_waiting(monkeypatch, tmp_path):
    """.claimed.json means another run already took it; re-recovering it is how
    one render becomes two uploads."""
    pending = tmp_path / "pending"
    pending.mkdir()
    (pending / "bundle.claimed.json").write_text(
        '{"title": "Already taken", "video_file": "final.mp4"}')
    (pending / "final.mp4").write_bytes(b"x")
    assert checkpoint.parked_bundle_waiting(str(pending)) is False


def test_metadata_without_its_video_file_is_not_waiting(tmp_path):
    pending = tmp_path / "pending"
    pending.mkdir()
    (pending / "bundle.json").write_text(
        '{"title": "Lost render", "video_file": "gone.mp4"}')
    assert checkpoint.parked_bundle_waiting(str(pending)) is False


def test_corrupt_bundle_metadata_does_not_crash_the_preflight(tmp_path):
    pending = tmp_path / "pending"
    pending.mkdir()
    (pending / "bundle.json").write_text("{not json at all")
    assert checkpoint.parked_bundle_waiting(str(pending)) is False


def test_no_pending_directory_means_nothing_to_recover(tmp_path):
    assert checkpoint.parked_bundle_waiting(str(tmp_path / "nope")) is False


# ------------------------------------- runway against the next scheduled write

def test_the_next_write_is_the_coming_wednesday_1515_utc():
    """Cowork writes the packet Wednesday 20:45 IST / 15:15 UTC, after the
    editorial brief refreshes at 14:47 UTC."""
    sat = datetime(2026, 9, 12, 10, 32, tzinfo=timezone.utc)   # a Saturday
    assert packet.next_packet_write(sat) == datetime(
        2026, 9, 16, 15, 15, tzinfo=timezone.utc)


def test_a_write_later_the_same_wednesday_is_still_today():
    wed_morning = datetime(2026, 9, 16, 9, 0, tzinfo=timezone.utc)
    assert packet.next_packet_write(wed_morning).day == 16


def test_a_write_already_past_rolls_to_next_week():
    wed_evening = datetime(2026, 9, 16, 18, 0, tzinfo=timezone.utc)
    assert packet.next_packet_write(wed_evening) == datetime(
        2026, 9, 23, 15, 15, tzinfo=timezone.utc)


def test_the_real_2026_W38_hole_is_detected():
    """The case a fixed 48-hour floor could not see, and the reason this
    function exists. W38's last slot was Tue 2026-09-15 01:07 UTC with 62.6
    hours of runway — comfortably ABOVE the 48-hour threshold — while the next
    packet was not written until Wed 2026-09-16 15:15 UTC. About six slots with
    nothing to publish."""
    now = datetime(2026, 9, 12, 10, 32, tzinfo=timezone.utc)
    pkt = {"stories": [_story("2026-09-15T0107Z")]}
    assert packet.runway_hours(pkt, now=now) > packet.RUNWAY_ALERT_HOURS
    assert packet.runway_deficit_hours(pkt, now=now) > 0


def test_a_packet_written_on_its_proper_day_has_slack():
    """A packet written Wednesday covering seven days reaches the next
    Wednesday write and must NOT fire the alert every single week."""
    now = datetime(2026, 9, 16, 16, 0, tzinfo=timezone.utc)   # just written
    pkt = {"stories": [_story("2026-09-23T1607Z")]}
    assert packet.runway_deficit_hours(pkt, now=now) == 0.0


def test_an_exhausted_packet_reports_the_whole_wait_as_the_deficit():
    now = datetime(2026, 9, 12, 10, 32, tzinfo=timezone.utc)
    pkt = {"stories": [_story("2026-09-08T0107Z", status="published")]}
    assert packet.runway_hours(pkt, now=now) is None
    deficit = packet.runway_deficit_hours(pkt, now=now)
    assert 100 < deficit < 110   # Saturday to the following Wednesday


def test_the_margin_is_zero_on_purpose():
    """Not an oversight. A seven-day packet on a seven-day write cycle has no
    designed slack: written Wednesday 15:15 UTC it ends the following Wednesday
    16:07, fifty-two minutes after its replacement is due. Any meaningful margin
    fires the alert every week on a healthy schedule, and a weekly false alarm
    is worse than no alarm.

    If packets ever cover more than seven days this can rise — until then, a
    non-zero value here is a bug."""
    assert packet.PACKET_WRITE_MARGIN_HOURS == 0.0


def test_a_correctly_scheduled_week_never_alerts():
    """The false-positive guard. Four consecutive on-time weekly packets, each
    written Wednesday and covering seven days, must all stay silent."""
    for week in range(4):
        written = datetime(2026, 9, 16, 15, 15, tzinfo=timezone.utc) + timedelta(days=7 * week)
        last_slot = written + timedelta(days=7)
        pkt = {"stories": [_story(last_slot.strftime("%Y-%m-%dT%H%MZ"))]}
        assert packet.runway_deficit_hours(pkt, now=written + timedelta(minutes=45)) == 0.0, week


def _real_week(written):
    """A packet laid out exactly the way scripts/assemble_packet.py and the
    weekly routine build one: the 28 slots strictly after the write time."""
    return {"stories": [_story(cadence.slot_id(s))
                        for s in cadence.next_slots(written, cadence.SLOTS_PER_WEEK)]}


def test_a_real_week_never_reports_a_hole_on_any_day():
    """The false alarm that fired on every run from 2026-09-21. A real packet
    written Wed 15:15 UTC ends Wed 11:07 UTC the following week — four hours
    BEFORE its replacement is written — yet no slot goes unserved, because the
    next slot (16:07) is the new packet's first. The old check compared the last
    slot with the write TIME and called that a 4-hour hole, every week. The
    tests above never caught it because their packets ended at 16:07, which a
    real one never does."""
    written = datetime(2026, 9, 16, 15, 15, tzinfo=timezone.utc)
    pkt = _real_week(written)
    last = max(packet._slot_dt(s) for s in pkt["stories"])
    assert last == datetime(2026, 9, 23, 11, 7, tzinfo=timezone.utc)
    for hours in range(1, 7 * 24, 5):
        now = written + timedelta(hours=hours)
        assert packet.runway_hole(pkt, now=now) == [], now
        assert packet.runway_deficit_hours(pkt, now=now) == 0.0, now


def test_a_real_week_missing_its_last_stories_reports_exactly_those_slots():
    written = datetime(2026, 9, 16, 15, 15, tzinfo=timezone.utc)
    pkt = _real_week(written)
    pkt["stories"] = pkt["stories"][:-3]
    hole = packet.runway_hole(pkt, now=datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc))
    assert [cadence.slot_id(s) for s in hole] == [
        "2026-09-23T0107Z", "2026-09-23T0607Z", "2026-09-23T1107Z"]


def test_the_first_slot_of_the_next_packet_is_the_one_after_the_write():
    now = datetime(2026, 9, 22, 11, 29, tzinfo=timezone.utc)
    assert packet.next_packet_first_slot(now) == datetime(
        2026, 9, 23, 16, 7, tzinfo=timezone.utc)


def _cli_on(monkeypatch, pkt, now, *argv):
    monkeypatch.setattr(packet, "_now", lambda: now)
    monkeypatch.setattr(packet, "load_packet", lambda path=None: pkt)
    monkeypatch.setattr(packet, "validate_packet", lambda p, published_subjects=None: [])
    monkeypatch.setattr(packet.history, "published_subjects", lambda: [])
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    return packet._cli(["--validate", *argv])


def test_a_short_new_week_fails_the_coverage_check(monkeypatch):
    written = datetime(2026, 9, 16, 15, 15, tzinfo=timezone.utc)
    pkt = _real_week(written)
    pkt["stories"] = pkt["stories"][:-4]
    now = written + timedelta(minutes=30)
    assert _cli_on(monkeypatch, pkt, now, "--require-coverage") == 1
    # ...but the same packet is still good enough for the upload pre-flight:
    # a hole next Wednesday is no reason to skip the slot that is due now.
    assert _cli_on(monkeypatch, pkt, now) == 0


def test_a_full_new_week_passes_the_coverage_check(monkeypatch):
    written = datetime(2026, 9, 16, 15, 15, tzinfo=timezone.utc)
    now = written + timedelta(minutes=30)
    assert _cli_on(monkeypatch, _real_week(written), now, "--require-coverage") == 0
