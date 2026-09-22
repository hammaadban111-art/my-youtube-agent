"""The weekly maintenance report: what counts as a problem, and what it does.

The report is the notification now — a red weekly run is what makes GitHub
email the owner — so it has to stop repeating settled items. The 2026-09-18
run reported four "problems" and three of them were not: two duplicate slots
a human had nothing left to decide about, the owner's own FIFA upload flagged
as a lost record, and ordinary scheduler lag counted as a stuck queue. And the
run itself went red for none of them: an artifact-storage quota failure on a
600-byte report file.
"""
import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent import packet, store

ROOT = Path(__file__).resolve().parent.parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


weekly = _load("weekly_health_check")
summary = _load("weekly_summary")


# ------------------------------------------------------------ manual uploads

def test_manual_uploads_file_lists_ids_not_notes(tmp_path, monkeypatch):
    path = tmp_path / "manual_uploads.json"
    path.write_text(json.dumps({"_about": "notes", "ABC123": "mine"}))
    monkeypatch.setattr(store, "MANUAL_UPLOADS_PATH", str(path))
    assert store.manual_upload_ids() == {"ABC123"}


def test_a_missing_manual_uploads_file_means_none(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "MANUAL_UPLOADS_PATH", str(tmp_path / "nope.json"))
    assert store.manual_upload_ids() == set()


def test_the_owners_fifa_upload_is_whitelisted():
    assert "UUdmnQXu-Qo" in store.manual_upload_ids()


def test_a_manual_upload_is_not_reported_as_a_lost_record(monkeypatch):
    monkeypatch.setattr(weekly.config, "YT_REFRESH_TOKEN", "token")
    monkeypatch.setattr(weekly.store, "all_records", lambda: [
        {"video_id": "OURS", "uploaded_at": "2026-08-01T00:00:00Z"}])
    monkeypatch.setattr(weekly.store, "manual_upload_ids", lambda: {"MINE"})
    monkeypatch.setattr(weekly.youtube_stats, "fetch_recent_uploads", lambda limit: [
        {"video_id": "OURS", "published_at": "2026-08-01T00:00:00Z"},
        {"video_id": "MINE", "published_at": "2026-08-17T00:00:00Z", "title": "fifa"}])
    report = weekly.Report()
    weekly.check_orphan_records(report)
    assert report.blocking == []


def test_a_real_orphan_is_still_reported(monkeypatch):
    monkeypatch.setattr(weekly.config, "YT_REFRESH_TOKEN", "token")
    monkeypatch.setattr(weekly.store, "all_records", lambda: [
        {"video_id": "OURS", "uploaded_at": "2026-08-01T00:00:00Z"}])
    monkeypatch.setattr(weekly.store, "manual_upload_ids", lambda: set())
    monkeypatch.setattr(weekly.youtube_stats, "fetch_recent_uploads", lambda limit: [
        {"video_id": "LOST", "published_at": "2026-08-10T00:00:00Z", "title": "x"}])
    report = weekly.Report()
    weekly.check_orphan_records(report)
    assert len(report.blocking) == 1


# ------------------------------------------------------------ duplicate slots

def _published(story_id, slot, video_id, **extra):
    return {"story_id": story_id, "slot": slot, "status": "published",
            "video_id": video_id, "published_at": "2026-09-08T06:10:00Z", **extra}


def test_an_acknowledged_duplicate_slot_is_no_longer_reported():
    ledger = {"schema_version": 1, "stories": {
        "a": _published("a", "2026-09-08T0607Z", "V1"),
        "b": _published("b", "2026-09-08T0607Z", "V2")}}
    packet._save_ledger(ledger)
    assert packet.duplicate_published_slots()
    packet.acknowledge_slot("2026-09-08T0607Z", "kept both")
    assert packet.duplicate_published_slots() == {}
    # Acknowledging never frees the slot for a third video.
    assert packet.slot_already_served("2026-09-08T0607Z", "c") is not None


def test_a_new_publish_into_an_acknowledged_slot_is_reported_again():
    ledger = {"schema_version": 1, "stories": {
        "a": _published("a", "2026-09-08T0607Z", "V1",
                        slot_conflict_acknowledged={"at": "x", "note": "y"}),
        "b": _published("b", "2026-09-08T0607Z", "V2",
                        slot_conflict_acknowledged={"at": "x", "note": "y"}),
        "c": _published("c", "2026-09-08T0607Z", "V3")}}
    packet._save_ledger(ledger)
    assert "2026-09-08T0607Z" in packet.duplicate_published_slots()


def test_acknowledging_a_slot_that_is_not_duplicated_changes_nothing():
    packet._save_ledger({"schema_version": 1, "stories": {
        "a": _published("a", "2026-09-08T0607Z", "V1")}})
    assert packet.acknowledge_slot("2026-09-08T0607Z", "n") == []


# ------------------------------------------------------------ queue lateness

NOW = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)


def _packet_with_due(hours_late):
    stories = []
    for i, late in enumerate(hours_late):
        slot = NOW - timedelta(hours=late)
        stories.append({"story_id": f"st-{i}", "status": "proposed",
                        "slot": {"utc": slot.strftime("%Y-%m-%dT%H%MZ")}})
    # Plenty of future stories so no runway hole is reported.
    for slot in weekly.cadence.next_slots(NOW, 40):
        stories.append({"story_id": f"future-{slot:%d%H}", "status": "proposed",
                        "slot": {"utc": weekly.cadence.slot_id(slot)}})
    return {"packet_id": "t", "stories": stories}


def _run_packet_check(monkeypatch, pkt):
    monkeypatch.setattr(packet, "_now", lambda: NOW)
    monkeypatch.setattr(packet, "load_packet", lambda path=None: pkt)
    monkeypatch.setattr(packet, "validate_packet", lambda p, published_subjects=None: [])
    monkeypatch.setattr(weekly.history, "published_subjects", lambda: [])
    report = weekly.Report()
    weekly.check_story_packet(report)
    return report


def test_ordinary_scheduler_lag_is_not_a_stuck_queue(monkeypatch):
    """Two stories due, 9 and 4 hours late: GitHub's cron lag plus one retried
    slot. The queue drains FIFO; nothing needs a human."""
    report = _run_packet_check(monkeypatch, _packet_with_due([9, 4]))
    assert report.blocking == []


def test_a_story_more_than_a_day_late_is_a_stuck_queue(monkeypatch):
    report = _run_packet_check(monkeypatch, _packet_with_due([30, 4]))
    assert len(report.blocking) == 1
    assert "stuck" in report.blocking[0]


# ------------------------------------------------------------ the summary step

def test_an_all_clear_week_is_green_and_writes_the_summary(tmp_path, monkeypatch):
    report = tmp_path / "r.txt"
    report.write_text("Weekly health check\nAll clear — nothing needs a human this week.")
    page = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(page))
    monkeypatch.setattr(sys, "argv", ["weekly_summary.py", str(report)])
    assert summary.main() == 0
    assert "all clear" in page.read_text()


def test_a_week_with_problems_goes_red_with_one_annotation_each(tmp_path, monkeypatch, capsys):
    report = tmp_path / "r.txt"
    report.write_text("Weekly health check\nPROBLEM: token expired\nPROBLEM: packet empty")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    monkeypatch.setattr(sys, "argv", ["weekly_summary.py", str(report)])
    assert summary.main() == 1
    errors = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("::error ")]
    assert len(errors) == 2
    assert "token expired" in errors[0]


def test_a_missing_report_goes_red(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.setattr(sys, "argv", ["weekly_summary.py", str(tmp_path / "none.txt")])
    assert summary.main() == 1


def test_the_weekly_job_uploads_no_artifact():
    """Artifact storage is an account-wide quota; a full one failed the whole
    job on 2026-09-18 after every check had passed."""
    workflow = (ROOT / ".github" / "workflows" / "weekly.yml").read_text()
    assert "upload-artifact" not in workflow
    assert "weekly_summary.py" in workflow


# ------------------------------------------------------------ recovered failures

def _runs_api(runs):
    def api_get(path):
        if "/jobs" in path:
            return {"jobs": [{"conclusion": "failure", "id": 7,
                              "steps": [{"name": "Run agent", "conclusion": "failure"}]}]}
        return {"workflow_runs": runs}
    return api_get


def test_a_failure_followed_by_a_success_is_marked_recovered(monkeypatch):
    from agent import ci_status
    monkeypatch.setattr(ci_status, "_token", lambda: "t")
    monkeypatch.setattr(ci_status, "_repo", lambda: "o/r")
    monkeypatch.setattr(ci_status, "WORKFLOWS", ["daily.yml"])
    monkeypatch.setattr(ci_status, "_fetch_job_log", lambda job_id: "RuntimeError: boom")
    monkeypatch.setattr(ci_status, "_api_get", _runs_api([
        {"id": 3, "html_url": "u3", "created_at": "2026-09-18T06:00:00Z", "conclusion": "success"},
        {"id": 2, "html_url": "u2", "created_at": "2026-09-17T11:34:06Z", "conclusion": "failure"},
    ]))
    failures = ci_status.recent_failures()["failures"]
    assert [f["recovered"] for f in failures] == [True]


def test_a_failure_with_no_later_success_is_still_open(monkeypatch):
    from agent import ci_status
    monkeypatch.setattr(ci_status, "_token", lambda: "t")
    monkeypatch.setattr(ci_status, "_repo", lambda: "o/r")
    monkeypatch.setattr(ci_status, "WORKFLOWS", ["daily.yml"])
    monkeypatch.setattr(ci_status, "_fetch_job_log", lambda job_id: "RuntimeError: boom")
    monkeypatch.setattr(ci_status, "_api_get", _runs_api([
        {"id": 2, "html_url": "u2", "created_at": "2026-09-18T11:34:06Z", "conclusion": "failure"},
        {"id": 1, "html_url": "u1", "created_at": "2026-09-18T06:00:00Z", "conclusion": "success"},
    ]))
    failures = ci_status.recent_failures()["failures"]
    assert [f["recovered"] for f in failures] == [False]


def _weekly_failures(monkeypatch, failures):
    from agent import ci_status
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for f in failures:
        f.setdefault("created_at", stamp)
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setattr(ci_status, "recent_failures",
                        lambda: {"available": True, "failures": failures})
    report = weekly.Report()
    weekly.check_recent_failures(report)
    return report


def test_recovered_failures_are_listed_but_do_not_turn_the_week_red(monkeypatch):
    report = _weekly_failures(monkeypatch, [
        {"workflow": "daily.yml", "error": "old", "recovered": True}])
    assert report.blocking == []
    assert any("followed by a successful run" in line for line in report.lines)


def test_an_unrecovered_failure_turns_the_week_red(monkeypatch):
    report = _weekly_failures(monkeypatch, [
        {"workflow": "daily.yml", "error": "still broken", "recovered": False},
        {"workflow": "daily.yml", "error": "old", "recovered": True}])
    assert len(report.blocking) == 1
