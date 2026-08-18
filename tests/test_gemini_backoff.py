import pytest
from unittest.mock import MagicMock
from google.genai import errors as genai_errors
from agent import gemini_utils


class FakeAPIError(genai_errors.APIError):
    def __init__(self, code, status):
        super().__init__(code, {})
        self.code = code
        self.status = status


@pytest.fixture
def mock_sleep(monkeypatch):
    sleeps = []
    def record_sleep(duration):
        sleeps.append(duration)
    monkeypatch.setattr("time.sleep", record_sleep)
    return sleeps


def test_gemini_backoff_max_attempts_and_delays(mock_sleep):
    # a) a call that raises 503 every time exhausts PRIMARY_MODEL's retries,
    # falls back to FALLBACK_MODEL, exhausts its (shorter) retry budget too,
    # and only then raises — total calls is the sum of both budgets;
    # b) no individual sleep exceeds MAX_DELAY_SECONDS;
    mock_fn = MagicMock(side_effect=FakeAPIError(503, "UNAVAILABLE"))

    with pytest.raises(genai_errors.APIError) as excinfo:
        gemini_utils.call_with_retry(mock_fn, label="test")

    assert excinfo.value.code == 503
    assert mock_fn.call_count == gemini_utils.MAX_ATTEMPTS + gemini_utils.FALLBACK_MAX_ATTEMPTS
    assert mock_sleep == [10, 20, 40, 80, 160, 10, 20]

    # Every call after PRIMARY_MODEL's own retries got FALLBACK_MODEL.
    models_called = [c.args[0] for c in mock_fn.call_args_list]
    assert models_called == (
        [gemini_utils.PRIMARY_MODEL] * gemini_utils.MAX_ATTEMPTS
        + [gemini_utils.FALLBACK_MODEL] * gemini_utils.FALLBACK_MAX_ATTEMPTS
    )

    # Verify condition (b) explicitly
    for duration in mock_sleep:
        assert duration <= gemini_utils.MAX_DELAY_SECONDS


def test_gemini_backoff_falls_back_after_primary_exhausted(mock_sleep):
    # PRIMARY_MODEL fails every time; FALLBACK_MODEL succeeds on its first try.
    mock_fn = MagicMock(side_effect=(
        [FakeAPIError(503, "UNAVAILABLE")] * gemini_utils.MAX_ATTEMPTS
        + ["success from fallback!"]
    ))

    result = gemini_utils.call_with_retry(mock_fn, label="test")

    assert result == "success from fallback!"
    assert mock_fn.call_count == gemini_utils.MAX_ATTEMPTS + 1
    models_called = [c.args[0] for c in mock_fn.call_args_list]
    assert models_called[-1] == gemini_utils.FALLBACK_MODEL
    assert models_called[:-1] == [gemini_utils.PRIMARY_MODEL] * gemini_utils.MAX_ATTEMPTS


def test_gemini_backoff_non_retryable(mock_sleep):
    # c) a 403 (non-retryable) raises after exactly ONE call and zero sleeps;
    mock_fn = MagicMock(side_effect=FakeAPIError(403, "PERMISSION_DENIED"))

    with pytest.raises(genai_errors.APIError) as excinfo:
        gemini_utils.call_with_retry(mock_fn, label="test")

    assert excinfo.value.code == 403
    assert mock_fn.call_count == 1
    assert mock_sleep == []


def test_gemini_backoff_success_after_retries(mock_sleep):
    # d) a call that fails 503 twice then succeeds returns the value and sleeps only twice.
    mock_fn = MagicMock(side_effect=[
        FakeAPIError(503, "UNAVAILABLE"),
        FakeAPIError(503, "UNAVAILABLE"),
        "success!"
    ])

    result = gemini_utils.call_with_retry(mock_fn, label="test")

    assert result == "success!"
    assert mock_fn.call_count == 3
    assert mock_sleep == [10, 20]
