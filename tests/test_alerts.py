"""Failures that need a human are raised as GitHub annotations.

There is no mail sender any more (the Resend integration was removed on
2026-09-22). What makes a failure visible is a red run — GitHub emails the owner
about that itself — plus an ::error:: annotation on the run page that says what
to DO. These tests pin both halves: the annotation text, and that the paths
which must go red still do.
"""
import sys

import httplib2
import pytest
from google.auth.exceptions import RefreshError
from googleapiclient.errors import HttpError
from unittest.mock import MagicMock

from agent import followup, gha, upload


def _annotation_lines(out: str, level: str = "error") -> list[str]:
    return [ln for ln in out.splitlines() if ln.startswith(f"::{level} ")]


def test_an_annotation_keeps_a_multi_line_message_in_one_command(capsys):
    """A raw newline ends a workflow command at the first line; GitHub's
    encoding (%0A) keeps the whole message inside the annotation."""
    gha.error("Upload failed: permanently", "line one\nline two, 100%")
    lines = _annotation_lines(capsys.readouterr().out)
    assert lines == [
        "::error title=Upload failed%3A permanently::line one%0Aline two, 100%25"]


def test_the_email_sender_is_gone():
    assert "agent.notify" not in sys.modules
    with pytest.raises(ImportError):
        from agent import notify  # noqa: F401


def test_permanent_upload_failure_is_annotated_with_what_to_do(monkeypatch, capsys):
    monkeypatch.setattr("time.sleep", lambda x: None)
    monkeypatch.setattr("agent.upload.park_for_next_run", MagicMock())

    mock_insert = MagicMock()
    mock_insert.execute.side_effect = RefreshError("invalid_grant: bad token")
    youtube = MagicMock()
    youtube.videos().insert.return_value = mock_insert
    monkeypatch.setattr("agent.upload._get_service", lambda: youtube)
    monkeypatch.setattr("agent.upload.MediaFileUpload", MagicMock())

    with pytest.raises(RefreshError):
        upload.upload_video("test.mp4", "title", "desc", [], {})

    lines = _annotation_lines(capsys.readouterr().out)
    assert len(lines) == 1
    assert "permanently" in lines[0]
    # The annotation has to say what to DO, not just that something broke.
    assert "YouTube login is dead" in lines[0]
    assert "YT_REFRESH_TOKEN" in lines[0]


def test_an_ambiguous_upload_is_annotated_for_manual_reconciliation(monkeypatch, capsys):
    monkeypatch.setattr("time.sleep", lambda x: None)
    monkeypatch.setattr("agent.upload.park_for_next_run", MagicMock())
    monkeypatch.setattr("agent.quota.record_units", MagicMock())

    error = HttpError(httplib2.Response({"status": 500}), b"Internal Server Error")
    mock_insert = MagicMock()
    mock_insert.execute.side_effect = error
    youtube = MagicMock()
    youtube.videos().insert.return_value = mock_insert
    monkeypatch.setattr("agent.upload._get_service", lambda: youtube)
    monkeypatch.setattr("agent.upload.MediaFileUpload", MagicMock())
    monkeypatch.setattr("agent.upload._landed_upload_id", lambda title, since: None)

    with pytest.raises(upload.AmbiguousUploadError):
        upload.upload_video("test.mp4", "title", "desc", [], {})

    # A blind retry can make a duplicate live video, so this is flagged rather
    # than silently retried.
    lines = _annotation_lines(capsys.readouterr().out)
    assert len(lines) == 1
    assert "permanently" in lines[0]


def test_measure_all_annotates_0_of_N_successes(monkeypatch, capsys):
    def failing_reading(record):
        raise ValueError("Failed")

    monkeypatch.setattr("agent.followup._take_reading", failing_reading)
    measured = followup.measure_all([{"video_id": "v1"}, {"video_id": "v2"}])

    assert measured == 0
    lines = _annotation_lines(capsys.readouterr().out)
    assert len(lines) == 1
    assert "Follow-up measured nothing" in lines[0]
    assert "All 2 video(s)" in lines[0]
    assert "ValueError" in lines[0]


def test_measure_all_is_quiet_on_1_of_N_successes(monkeypatch, capsys):
    def reading(record):
        if record["video_id"] == "v1":
            raise ValueError("Failed")

    monkeypatch.setattr("agent.followup._take_reading", reading)
    assert followup.measure_all([{"video_id": "v1"}, {"video_id": "v2"}]) == 1
    assert _annotation_lines(capsys.readouterr().out) == []


def test_followup_job_goes_red_when_every_reading_fails(monkeypatch):
    """followup.yml once exited 0 through two days of 36-for-36 failures. The
    red run is now the notification, so it has to happen."""
    monkeypatch.setattr(followup.store, "measurable_records", lambda: [{}, {}])
    monkeypatch.setattr(followup, "sweep", lambda: 0)
    assert followup.main() == 1


def test_followup_job_stays_green_when_nothing_was_due(monkeypatch):
    monkeypatch.setattr(followup.store, "measurable_records", lambda: [])
    monkeypatch.setattr(followup, "sweep", lambda: 0)
    assert followup.main() == 0


def test_followup_job_stays_green_on_a_partial_success(monkeypatch):
    monkeypatch.setattr(followup.store, "measurable_records", lambda: [{}, {}, {}])
    monkeypatch.setattr(followup, "sweep", lambda: 2)
    assert followup.main() == 0
