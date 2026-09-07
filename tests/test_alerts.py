import pytest
from unittest.mock import patch, MagicMock
from agent import notify, upload, followup
from googleapiclient.errors import HttpError
from google.auth.exceptions import RefreshError
import httplib2
import os


def test_notify_alert_never_raises_when_unset(monkeypatch):
    # Patches the MODULE ATTRIBUTES, not the environment. notify reads
    # RESEND_API_KEY and NOTIFY_TO once, at import, so monkeypatch.setenv here
    # changed nothing at all and this test only ever passed because the keys
    # happened to be absent from the shell that ran it.
    #
    # That is not hypothetical: the weekly maintenance job ran the suite with
    # the mail secrets exported, so on 2026-09-07 this test took the real
    # send path, actually emailed the owner, and failed on `assert True is
    # False`. Pinning both attributes makes the test assert what its name
    # claims regardless of the ambient environment.
    monkeypatch.setattr(notify, "RESEND_API_KEY", "")
    monkeypatch.setattr(notify, "NOTIFY_TO", "")
    # Should not raise
    assert notify.alert("Test", "body") is False


def test_notify_alert_is_a_no_op_without_a_recipient(monkeypatch):
    """A key with no NOTIFY_TO is the other half of "not configured", and it
    must be just as inert — send_email checks both."""
    monkeypatch.setattr(notify, "RESEND_API_KEY", "re_something")
    monkeypatch.setattr(notify, "NOTIFY_TO", "")
    assert notify.alert("Test", "body") is False


def test_alert_fires_on_permanent_upload_failure(monkeypatch):
    mock_alert = MagicMock()
    monkeypatch.setattr("agent.notify.alert", mock_alert)
    monkeypatch.setattr("time.sleep", lambda x: None)

    mock_park = MagicMock()
    monkeypatch.setattr("agent.upload.park_for_next_run", mock_park)

    mock_insert = MagicMock()
    mock_insert.execute.side_effect = RefreshError("invalid_grant: bad token")

    youtube = MagicMock()
    youtube.videos().insert.return_value = mock_insert
    monkeypatch.setattr("agent.upload._get_service", lambda: youtube)
    monkeypatch.setattr("agent.upload.MediaFileUpload", MagicMock())

    with pytest.raises(RefreshError):
        upload.upload_video("test.mp4", "title", "desc", [], {})

    mock_alert.assert_called_once()
    assert "permanently" in mock_alert.call_args[0][0]
    # The alert has to say what to DO, not just that something broke.
    assert "YouTube login is dead" in mock_alert.call_args[0][1]
    assert "YT_REFRESH_TOKEN" in mock_alert.call_args[0][1]


def test_alert_does_not_fire_on_transient_upload_failure(monkeypatch):
    mock_alert = MagicMock()
    monkeypatch.setattr("agent.notify.alert", mock_alert)
    monkeypatch.setattr("time.sleep", lambda x: None)

    mock_park = MagicMock()
    monkeypatch.setattr("agent.upload.park_for_next_run", mock_park)

    mock_record_units = MagicMock()
    monkeypatch.setattr("agent.quota.record_units", mock_record_units)

    resp = httplib2.Response({"status": 500})
    error = HttpError(resp, b"Internal Server Error")

    mock_insert = MagicMock()
    mock_insert.execute.side_effect = error

    youtube = MagicMock()
    youtube.videos().insert.return_value = mock_insert
    monkeypatch.setattr("agent.upload._get_service", lambda: youtube)
    monkeypatch.setattr("agent.upload.MediaFileUpload", MagicMock())

    with pytest.raises(HttpError):
        upload.upload_video("test.mp4", "title", "desc", [], {})

    mock_alert.assert_not_called()


def test_measure_all_alerts_on_0_of_N_successes(monkeypatch):
    mock_alert = MagicMock()
    monkeypatch.setattr("agent.notify.alert", mock_alert)

    def mock_take_reading(record):
        raise ValueError("Failed")

    monkeypatch.setattr("agent.followup._take_reading", mock_take_reading)

    due = [{"video_id": "v1"}, {"video_id": "v2"}]
    measured = followup.measure_all(due)

    assert measured == 0
    mock_alert.assert_called_once()
    assert "Follow-up" in mock_alert.call_args[0][0]
    assert "All 2 video(s)" in mock_alert.call_args[0][1]
    assert "ValueError" in mock_alert.call_args[0][1]


def test_measure_all_no_alert_on_1_of_N_successes(monkeypatch):
    mock_alert = MagicMock()
    monkeypatch.setattr("agent.notify.alert", mock_alert)

    def mock_take_reading(record):
        if record["video_id"] == "v1":
            raise ValueError("Failed")
        # v2 succeeds

    monkeypatch.setattr("agent.followup._take_reading", mock_take_reading)

    due = [{"video_id": "v1"}, {"video_id": "v2"}]
    measured = followup.measure_all(due)

    assert measured == 1
    mock_alert.assert_not_called()
