"""The shortfall probe that decides whether a catch-up write is needed.

On 2026-09-09 the Wednesday packet-writing task failed eighteen seconds after
firing and nothing noticed. These tests pin the two properties the catch-up
automation depends on: it FIRES while there is a real hole, and — the part that
makes a daily retry terminate rather than run forever — it STOPS the moment a
full week has been written.
"""
import json

from agent import cadence, packet
from tests.test_packet import _packet, _story, _utc, _write_packet


def _probe(capsys):
    assert packet._cli(["--shortfall"]) == 0
    return capsys.readouterr().out


def _week_from(start):
    """A full, healthy week of unpublished stories beginning after `start` —
    built the same way a real packet is, via cadence.next_slots."""
    return [_story(slot, f"Subject {i}", story_id=f"st-{cadence.slot_id(slot)}-{i}")
            for i, slot in enumerate(cadence.next_slots(start, cadence.SLOTS_PER_WEEK))]


def test_a_full_fresh_week_reports_healthy(tmp_path, monkeypatch, capsys):
    """The terminating condition. Once a week has been written the probe must
    go quiet, or the daily catch-up would keep writing packets forever."""
    monkeypatch.setattr(packet, "_now", lambda: _utc(2026, 9, 16, 16, 0))
    _write_packet(tmp_path, monkeypatch, _week_from(_utc(2026, 9, 16, 16, 7)))

    out = _probe(capsys)
    assert "healthy" in out
    assert "SHORT" not in out


def test_an_exhausted_packet_reports_short(tmp_path, monkeypatch, capsys):
    """Every story already published and nothing planned: the channel has
    nothing to upload at the very next slot."""
    now = _utc(2026, 9, 14, 12, 0)
    monkeypatch.setattr(packet, "_now", lambda: now)
    spent = _story(_utc(2026, 9, 10, 6, 7), "Dyatlov Pass", story_id="spent")
    _write_packet(tmp_path, monkeypatch, [spent])
    packet.record_status(spent, "published", video_id="vid1")

    out = _probe(capsys)
    assert "SHORT" in out
    assert "nothing left to publish" in out


def test_a_packet_that_runs_dry_before_the_next_write_reports_short(
        tmp_path, monkeypatch, capsys):
    """The 2026-09-12 failure mode exactly: stories remain, so a story COUNT
    looks fine, but they run out before the replacement packet is due. This is
    why the probe measures hours-to-next-write and not how many stories are
    left."""
    now = _utc(2026, 9, 10, 12, 0)          # Thursday
    monkeypatch.setattr(packet, "_now", lambda: now)
    # Two stories left, both gone by Friday; the next write is the following
    # Wednesday, so there is a multi-day hole behind them.
    stories = [_story(_utc(2026, 9, 11, 6, 7), "A", story_id="a"),
               _story(_utc(2026, 9, 11, 11, 7), "B", story_id="b")]
    _write_packet(tmp_path, monkeypatch, stories)

    out = _probe(capsys)
    assert "SHORT" in out
    assert "The hole is" in out


def test_a_packet_that_will_not_load_is_the_worst_shortfall(
        tmp_path, monkeypatch, capsys):
    """A corrupt or missing packet must report short rather than crash: the
    watchdog exists for exactly the mornings when something is badly wrong, and
    a traceback there would take the alarm down with the thing it watches."""
    path = tmp_path / "packet.json"
    path.write_text("{ not json")
    monkeypatch.setattr(packet, "PACKET_PATH", str(path))

    out = _probe(capsys)
    assert "SHORT" in out


def test_the_probe_writes_machine_readable_outputs(tmp_path, monkeypatch, capsys):
    """The workflow branches on these, so their names are part of the contract."""
    now = _utc(2026, 9, 16, 16, 0)
    monkeypatch.setattr(packet, "_now", lambda: now)
    _write_packet(tmp_path, monkeypatch, _week_from(_utc(2026, 9, 16, 16, 7)))
    out_file = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out_file))

    _probe(capsys)
    written = dict(line.split("=", 1) for line in
                   out_file.read_text().strip().splitlines())
    assert written["shortfall"] == "false"
    assert written["remaining"] == str(cadence.SLOTS_PER_WEEK)
    assert written["deficit_hours"] == "0.0"


def test_the_probe_never_fails_its_workflow_step(tmp_path, monkeypatch, capsys):
    """Exit code is always 0, short or not. A non-zero exit would fail the
    watchdog job, and a red watchdog is indistinguishable from a broken one."""
    now = _utc(2026, 9, 14, 12, 0)
    monkeypatch.setattr(packet, "_now", lambda: now)
    spent = _story(_utc(2026, 9, 10, 6, 7), "Dyatlov Pass", story_id="spent2")
    _write_packet(tmp_path, monkeypatch, [spent])
    packet.record_status(spent, "published", video_id="vid2")

    assert packet._cli(["--shortfall"]) == 0
