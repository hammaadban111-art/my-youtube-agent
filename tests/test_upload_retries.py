import pytest
from unittest.mock import patch, MagicMock
from googleapiclient.errors import HttpError
from google.auth.exceptions import RefreshError
from agent import upload, quota
import httplib2


@pytest.fixture
def mock_sleep(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda x: None)


@pytest.fixture
def mock_deps(monkeypatch):
    mock_get_service = MagicMock()
    mock_media = MagicMock()
    mock_park = MagicMock(return_value="parked.mp4")

    monkeypatch.setattr("agent.upload._get_service", mock_get_service)
    monkeypatch.setattr("agent.upload.MediaFileUpload", mock_media)
    monkeypatch.setattr("agent.upload.park_for_next_run", mock_park)

    mock_record_upload = MagicMock()
    monkeypatch.setattr("agent.quota.record_upload", mock_record_upload)
    mock_record_failed = MagicMock()
    monkeypatch.setattr("agent.quota.record_failed_upload", mock_record_failed)

    return {
        "service": mock_get_service,
        "park": mock_park,
        "record_upload": mock_record_upload,
        "record_failed_upload": mock_record_failed,
    }


def test_upload_400_httperror_is_permanent(mock_deps, mock_sleep):
    # 400 is in NON_RETRYABLE_STATUS
    resp = httplib2.Response({"status": 400})
    error = HttpError(resp, b"Bad Request")

    mock_insert = MagicMock()
    mock_insert.execute.side_effect = error

    youtube = MagicMock()
    youtube.videos().insert.return_value = mock_insert
    mock_deps["service"].return_value = youtube

    with pytest.raises(HttpError):
        upload.upload_video("test.mp4", "title", "desc", [], {})

    # ONE attempt means insert was called exactly once
    assert youtube.videos().insert.call_count == 1
    # ONE attempt means quota slot was booked exactly once
    assert mock_deps["record_failed_upload"].call_count == 1
    assert mock_deps["record_upload"].call_count == 0
    # The video must still be parked
    mock_deps["park"].assert_called_once()


def test_upload_quota_exceeded_httperror_is_permanent(mock_deps, mock_sleep):
    # Quota exceeded error (even with e.g. a 403 status which isn't in NON_RETRYABLE_STATUS)
    resp = httplib2.Response({"status": 403})
    error = HttpError(resp, b"quotaExceeded")

    mock_insert = MagicMock()
    mock_insert.execute.side_effect = error

    youtube = MagicMock()
    youtube.videos().insert.return_value = mock_insert
    mock_deps["service"].return_value = youtube

    with pytest.raises(HttpError):
        upload.upload_video("test.mp4", "title", "desc", [], {})

    assert youtube.videos().insert.call_count == 1
    assert mock_deps["record_failed_upload"].call_count == 1
    assert mock_deps["record_upload"].call_count == 0
    mock_deps["park"].assert_called_once()


def test_upload_refresherror_is_permanent(mock_deps, mock_sleep):
    error = RefreshError("invalid_grant: Token has been expired or revoked")

    # We can mock _get_service to raise this, or mock insert to raise it.
    mock_deps["service"].side_effect = error

    with pytest.raises(RefreshError):
        upload.upload_video("test.mp4", "title", "desc", [], {})

    # _get_service called exactly once
    assert mock_deps["service"].call_count == 1
    # Quota shouldn't be booked if it failed before calling insert, but the requirement is "exactly one attempt" overall.
    # The requirement says "produces exactly one attempt".
    # And "the video is still parked in all of those cases".
    mock_deps["park"].assert_called_once()


def test_upload_500_httperror_is_parked_as_ambiguous_not_retried(mock_deps, mock_sleep,
                                                                  monkeypatch):
    # A 500 can arrive after videos.insert created the video. There is no
    # caller-provided idempotency key, so a second insert could duplicate it.
    resp = httplib2.Response({"status": 500})
    error = HttpError(resp, b"Internal Server Error")

    mock_insert = MagicMock()
    mock_insert.execute.side_effect = error

    youtube = MagicMock()
    youtube.videos().insert.return_value = mock_insert
    mock_deps["service"].return_value = youtube
    monkeypatch.setattr(upload, "_landed_upload_id", lambda title, since: None)

    with pytest.raises(upload.AmbiguousUploadError):
        upload.upload_video("test.mp4", "title", "desc", [], {})

    assert youtube.videos().insert.call_count == 1
    assert mock_deps["record_failed_upload"].call_count == 1
    assert mock_deps["record_upload"].call_count == 0
    mock_deps["park"].assert_called_once()
    assert mock_deps["park"].call_args.kwargs["requires_manual_reconciliation"] is True
