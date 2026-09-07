import httpx
import pytest
from unittest.mock import MagicMock
from google.genai import errors as genai_errors
from agent import gemini_utils


class FakeAPIError(genai_errors.APIError):
    def __init__(self, code, status):
        super().__init__(code, {})
        self.code = code
        self.status = status


@pytest.fixture(autouse=True)
def fresh_budget():
    """Each test starts with the whole process budget unspent. Without this the
    first test to exhaust a ladder would leave nothing for the next one."""
    gemini_utils.reset_budget()
    yield
    gemini_utils.reset_budget()


@pytest.fixture
def mock_sleep(monkeypatch):
    sleeps = []
    def record_sleep(duration):
        sleeps.append(duration)
    monkeypatch.setattr("time.sleep", record_sleep)
    return sleeps


def nominal_delays(count):
    """The un-jittered ladder the delays must sit under."""
    return [min(gemini_utils.BASE_DELAY_SECONDS * (2 ** i), gemini_utils.MAX_DELAY_SECONDS)
            for i in range(count)]


def assert_jittered(actual, expected_nominal):
    """Each delay must land in [nominal/2, nominal] — the equal-jitter band.

    Asserted as a band rather than as exact numbers on purpose: pinning exact
    delays is what made this suite reject jitter, and jitter is the point (see
    agent/gemini_utils.py on lock-step ladders colliding across the four daily
    slots and the follow-up sweep)."""
    assert len(actual) == len(expected_nominal)
    for got, nominal in zip(actual, expected_nominal):
        assert nominal / 2 <= got <= nominal, f"{got} outside [{nominal/2}, {nominal}]"


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
    assert_jittered(
        mock_sleep,
        nominal_delays(gemini_utils.MAX_ATTEMPTS - 1)
        + nominal_delays(gemini_utils.FALLBACK_MAX_ATTEMPTS - 1),
    )

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
    assert_jittered(mock_sleep, nominal_delays(2))


# --- Transport errors -------------------------------------------------------
#
# Run 33476553815 (2026-09-01T06:20Z) died on httpx.RemoteProtocolError raised
# out of the FIRST attempt: it is not a google.genai APIError, so the old
# `except genai_errors.APIError` never saw it and there was no retry at all.
# A server that hangs up without answering is the most transient failure there
# is — the request never even got a verdict.

@pytest.mark.parametrize("error", [
    httpx.RemoteProtocolError("Server disconnected without sending a response."),
    httpx.ReadTimeout("timed out"),
    httpx.ConnectError("connection refused"),
    ConnectionError("connection reset by peer"),
])
def test_transport_errors_are_retried(mock_sleep, error):
    mock_fn = MagicMock(side_effect=[error, error, "recovered"])

    result = gemini_utils.call_with_retry(mock_fn, label="test")

    assert result == "recovered"
    assert mock_fn.call_count == 3
    assert_jittered(mock_sleep, nominal_delays(2))


def test_transport_error_exhausts_both_ladders_then_raises(mock_sleep):
    # The exact failure from run 33476553815, sustained: it must now cost the
    # full two-model ladder before the run is allowed to die.
    disconnect = httpx.RemoteProtocolError("Server disconnected without sending a response.")
    mock_fn = MagicMock(side_effect=disconnect)

    with pytest.raises(httpx.RemoteProtocolError):
        gemini_utils.call_with_retry(mock_fn, label="test")

    assert mock_fn.call_count == gemini_utils.MAX_ATTEMPTS + gemini_utils.FALLBACK_MAX_ATTEMPTS


def test_programming_errors_are_never_retried(mock_sleep):
    # The retry loop catches broad Exception to see transport errors, so it has
    # to be provably unable to swallow a bug in the callable it is running.
    mock_fn = MagicMock(side_effect=ValueError("bad payload"))

    with pytest.raises(ValueError):
        gemini_utils.call_with_retry(mock_fn, label="test")

    assert mock_fn.call_count == 1
    assert mock_sleep == []


# --- Budgets ----------------------------------------------------------------

def test_process_budget_stops_the_ladder_early(monkeypatch, mock_sleep):
    # The widened ladders are only safe because the job cannot be pushed past
    # its 45-minute timeout by them. With almost no budget left, the very first
    # retry is refused and the real error is raised while there is still time
    # to log it.
    monkeypatch.setattr(gemini_utils, "PROCESS_BUDGET_SECONDS", 1.0)
    mock_fn = MagicMock(side_effect=FakeAPIError(503, "UNAVAILABLE"))

    with pytest.raises(genai_errors.APIError):
        gemini_utils.call_with_retry(mock_fn, label="test")

    # One request per model, no sleeps the budget could not pay for. The
    # fallback still gets its request: only waiting is charged against budget.
    assert mock_fn.call_count == 2
    assert mock_sleep == []


def test_budget_is_only_charged_for_sleeping(mock_sleep):
    mock_fn = MagicMock(side_effect=[FakeAPIError(503, "UNAVAILABLE"), "ok"])

    assert gemini_utils.budget_spent() == 0
    gemini_utils.call_with_retry(mock_fn, label="test")

    assert gemini_utils.budget_spent() == pytest.approx(sum(mock_sleep))
    assert gemini_utils.budget_spent() > 0


def test_worst_case_wait_fits_inside_the_job_timeout():
    """The whole point of PROCESS_BUDGET_SECONDS: no combination of ladders
    across every Gemini call site in a run can reach daily.yml's 45-minute
    timeout. A normal run is ~11 minutes."""
    job_timeout_seconds = 45 * 60
    normal_run_seconds = 11 * 60
    assert normal_run_seconds + gemini_utils.PROCESS_BUDGET_SECONDS < job_timeout_seconds


def test_one_ladder_fits_inside_its_own_budget():
    primary = sum(nominal_delays(gemini_utils.MAX_ATTEMPTS - 1))
    fallback = sum(nominal_delays(gemini_utils.FALLBACK_MAX_ATTEMPTS - 1))
    assert primary <= gemini_utils.LADDER_BUDGET_SECONDS
    assert fallback <= gemini_utils.LADDER_BUDGET_SECONDS


def test_jitter_actually_varies():
    """Two ladders started together must not sleep in lock step — that is the
    collision the jitter exists to break."""
    seen = {gemini_utils._delay_for(5) for _ in range(50)}
    assert len(seen) > 1
