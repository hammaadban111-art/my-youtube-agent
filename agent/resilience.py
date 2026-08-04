"""
Shared retry/backoff and degradation tracking for the unattended pipeline.

This runs 4x/day with nobody watching, so the failure mode that matters is a
single transient upstream hiccup killing an entire slot's video. Every stage
that touches a third party (Pexels, edge-tts, Gemini, YouTube) retries with
backoff and, where a sane substitute exists, degrades instead of dying.

Degrading silently would be worse than failing: a video assembled from the
wrong footage or a fallback voice looks like a normal success in the logs.
So every fallback taken is recorded here, saved onto the video record, and
rendered on the dashboard - a degraded run is visible, not invisible.
"""
import time

# Collected per-process. The pipeline is a single short-lived process per run,
# so module state is the whole run's state; nothing leaks between runs.
_degradations: list[dict] = []


def record_degradation(stage: str, detail: str, fallback: str) -> None:
    """Notes that a stage did NOT do the normal thing, and what it did instead."""
    entry = {"stage": stage, "detail": detail[:300], "fallback": fallback[:200]}
    _degradations.append(entry)
    print(f"[degraded] {stage}: {detail[:160]} -> {fallback}")


def degradations() -> list[dict]:
    return list(_degradations)


def reset() -> None:
    _degradations.clear()


def retry(fn, *, label: str, attempts: int = 3, base_delay: float = 3.0,
          retry_on: tuple = (Exception,), dont_retry_on: tuple = ()):
    """Runs fn() with exponential backoff. Re-raises the last error if every
    attempt fails, so the caller decides whether to fall back or abort - this
    helper never silently swallows a failure."""
    last = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except dont_retry_on:
            raise
        except retry_on as e:  # noqa: PERF203 - retry loop, cost is irrelevant here
            last = e
            if attempt == attempts:
                break
            delay = base_delay * (2 ** (attempt - 1))
            print(f"[retry] {label}: {type(e).__name__}: {str(e)[:140]} "
                  f"(attempt {attempt}/{attempts}) — retrying in {delay:.0f}s")
            time.sleep(delay)
    raise last
