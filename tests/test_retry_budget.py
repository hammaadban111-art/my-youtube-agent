"""The run-level retry budget added 2026-09-12.

The cost problem it solves, measured rather than assumed: across September's 48
upload runs, 24 took 15 minutes or more and those 24 alone burned 610 of the
month's 806 billed upload minutes. A healthy run is 7 minutes with agent.main
taking 283 seconds; run 34038131359 spent 1,575 seconds in agent.main and still
reported success. The extra time was not rendering — it was sleeping between
retries, across four ladders that are individually sensible and collectively
about seven minutes of backoff before counting the failed calls themselves.

The rule these tests pin: the budget is consulted ONLY before a sleep, so work
that is genuinely in flight is never interrupted; and a budget that is absent,
zero or malformed disables the mechanism rather than breaking the run.
"""
import time

import pytest

from agent import resilience


@pytest.fixture(autouse=True)
def _clean_budget():
    resilience.clear_budget()
    resilience.reset()
    yield
    resilience.clear_budget()
    resilience.reset()


def test_no_budget_means_the_ladder_is_unchanged():
    """Every existing caller and every test written before this feature must
    behave exactly as before when no budget is running."""
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("transient")
        return "ok"

    assert resilience.remaining_budget() is None
    assert resilience.retry(flaky, label="t", attempts=3, base_delay=0.001) == "ok"
    assert len(calls) == 3


def test_an_exhausted_budget_stops_the_ladder_early():
    calls = []

    def always_fails():
        calls.append(1)
        raise RuntimeError("upstream is down")

    resilience.start_budget(0.001)
    time.sleep(0.01)
    with pytest.raises(RuntimeError, match="upstream is down"):
        # base_delay is large: without the budget this would sleep for minutes.
        resilience.retry(always_fails, label="pexels", attempts=5, base_delay=60.0)
    # One attempt made, then the budget stopped it rather than sleeping 30s+.
    assert len(calls) == 1


def test_stopping_early_still_raises_the_real_error():
    """The caller must see the upstream failure, not a budget error — every
    fallback path in the pipeline branches on the original exception type."""
    class Upstream(RuntimeError):
        pass

    def boom():
        raise Upstream("pexels 429")

    resilience.start_budget(0.001)
    time.sleep(0.01)
    with pytest.raises(Upstream):
        resilience.retry(boom, label="pexels", attempts=3, base_delay=30.0)


def test_giving_up_early_is_recorded_as_a_degradation():
    """A run that quietly did less work than usual must say so — the same rule
    every other fallback in this module follows."""
    resilience.start_budget(0.001)
    time.sleep(0.01)
    with pytest.raises(RuntimeError):
        resilience.retry(lambda: (_ for _ in ()).throw(RuntimeError("x")),
                         label="edge-tts", attempts=3, base_delay=30.0)
    stages = [d["stage"] for d in resilience.degradations()]
    assert "retry-budget" in stages


def test_a_healthy_run_never_sees_the_budget():
    """The whole point: a generous budget must not change normal behaviour."""
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) < 2:
            raise RuntimeError("one blip")
        return "fine"

    resilience.start_budget(900)
    assert resilience.retry(flaky, label="t", attempts=3, base_delay=0.001) == "fine"
    assert resilience.degradations() == []


def test_a_long_in_flight_call_is_not_interrupted():
    """The budget gates SLEEPS, never work. A slow moviepy render or a slow but
    progressing upload must be allowed to finish even past the deadline."""
    resilience.start_budget(0.05)

    def slow_but_successful():
        time.sleep(0.2)          # outlives the budget
        return "rendered"

    assert resilience.retry(slow_but_successful, label="render",
                            attempts=3, base_delay=30.0) == "rendered"
    assert resilience.budget_exhausted() is True


def test_budget_reads_the_environment(monkeypatch):
    monkeypatch.setenv(resilience.BUDGET_ENV, "120")
    assert resilience.start_budget() == 120.0
    assert 0 < resilience.remaining_budget() <= 120.0


def test_a_malformed_budget_falls_back_to_the_default(monkeypatch):
    """A typo in a repository variable must not be the thing that stops the
    channel publishing."""
    monkeypatch.setenv(resilience.BUDGET_ENV, "twenty minutes")
    assert resilience.start_budget() == resilience.DEFAULT_BUDGET_SECONDS


def test_zero_disables_the_budget(monkeypatch):
    monkeypatch.setenv(resilience.BUDGET_ENV, "0")
    assert resilience.start_budget() == 0.0
    assert resilience.remaining_budget() is None
    assert resilience.budget_exhausted() is False


def test_the_default_is_generous_against_a_real_run():
    """283s is agent.main on a healthy run. The default must be far above it,
    or a normal slow-ish day starts failing."""
    assert resilience.DEFAULT_BUDGET_SECONDS >= 3 * 283
