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
import random
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


def backoff_delay(attempt: int, base_delay: float, max_delay: float = 300.0) -> float:
    """Equal-jitter exponential backoff: half the nominal delay, plus a random
    share of the other half.

    The jitter is not cosmetic. Every retry ladder in this repo used to sleep
    exactly base, 2x, 4x... so two callers that failed on the same upstream
    hiccup — the four daily slots, the 3-hourly follow-up sweep and the weekly
    maintenance job all share one Pexels key, one Gemini key and one YouTube
    token — would wake on the same second and collide again on every rung.
    Halving-plus-jitter keeps the exponential growth (a long outage is still
    genuinely waited out) while spreading the wake-ups across the interval.

    Capped so a long ladder cannot sleep past a job's own timeout."""
    nominal = min(base_delay * (2 ** (attempt - 1)), max_delay)
    return nominal / 2 + random.uniform(0, nominal / 2)


def retry(fn, *, label: str, attempts: int = 3, base_delay: float = 3.0,
          retry_on: tuple = (Exception,), dont_retry_on: tuple = (),
          retry_if: callable = None):
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
            if retry_if and not retry_if(e):
                print(f"[retry] {label}: {type(e).__name__}: {str(e)[:140]} "
                      f"- error is permanent, not retrying")
                raise
            if attempt == attempts:
                break
            delay = backoff_delay(attempt, base_delay)
            print(f"[retry] {label}: {type(e).__name__}: {str(e)[:140]} "
                  f"(attempt {attempt}/{attempts}) — retrying in {delay:.0f}s")
            time.sleep(delay)
    raise last
