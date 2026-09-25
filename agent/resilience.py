"""
Shared retry/backoff and degradation tracking for the unattended pipeline.

This runs 2x/day with nobody watching, so the failure mode that matters is a
single transient upstream hiccup killing an entire slot's video. Every stage
that touches a third party (Pexels, edge-tts, Gemini, YouTube) retries with
backoff and, where a sane substitute exists, degrades instead of dying.

Degrading silently would be worse than failing: a video assembled from the
wrong footage or a fallback voice looks like a normal success in the logs.
So every fallback taken is recorded here, saved onto the video record, and
rendered on the dashboard - a degraded run is visible, not invisible.
"""
import os
import random
import time

# Collected per-process. The pipeline is a single short-lived process per run,
# so module state is the whole run's state; nothing leaks between runs.
_degradations: list[dict] = []

# ---------------------------------------------------------------- run budget
#
# WHY A BUDGET EXISTS AT ALL.
#
# GitHub bills Actions by the minute and this repository is private, so the
# 2,000-minute monthly allowance is a real ceiling — August 2026 billed 2,449
# and the account ran out mid-month. Measured across September's 48 upload
# runs: 24 of them took 15 minutes or more and those 24 alone burned 610 of the
# month's 806 upload minutes. A HEALTHY run is 7 minutes, of which agent.main
# is 283 seconds; the pathological ones spent 1,575 seconds in agent.main
# (run 34038131359) and still reported success.
#
# The time is not spent rendering. It is spent SLEEPING between retries. Every
# ladder in this pipeline is individually reasonable and collectively enormous:
# grounding retries 5 times from an 8-second base (60-120s of sleep), edge-tts
# retries 3 times from 3 seconds PER SENTENCE across ~9 sentences (40-80s),
# Pexels retries 3 times from 4 seconds per segment across 5 segments and 3
# query rungs (up to 180s), and the upload adds 15-30s — roughly seven minutes
# of pure waiting before counting the failed calls themselves, each of which
# can take another 30-60s to time out.
#
# WHAT THIS DOES AND DOES NOT INTERRUPT.
#
# The budget is consulted ONLY in retry(), and only immediately before a sleep.
# A long but healthy moviepy render, a slow upload that is actually
# progressing, or any single in-flight call is never interrupted — the run is
# allowed to finish work it is genuinely doing. What it stops is starting
# ANOTHER attempt when the clock says the remaining attempts cannot plausibly
# help. The failure that follows is the one the ladder would have produced
# anyway, several billed minutes later.
#
# That is safe here specifically because a failed render is not a lost render:
# agent/upload.py parks a rendered-but-unuploaded video and the next run
# recovers it (see agent/main.py's _recover_parked_video and the recovery
# pre-flight in daily.yml), so giving up early costs minutes, not a video.
#
# Deliberately generous: 15 minutes is more than double a healthy run's total
# job time, so no run that is working normally will ever see it.
BUDGET_ENV = "PIPELINE_BUDGET_SECONDS"
DEFAULT_BUDGET_SECONDS = 900.0

_deadline: float | None = None


def start_budget(seconds: float = None) -> float:
    """Opens the run's retry budget and returns the number of seconds granted.

    Reads PIPELINE_BUDGET_SECONDS when no explicit value is given. A value of 0
    or less, or an unparseable one, disables the budget entirely rather than
    failing the run — a malformed environment variable must not be the thing
    that stops the channel publishing."""
    global _deadline
    if seconds is None:
        raw = os.getenv(BUDGET_ENV, "").strip()
        try:
            seconds = float(raw) if raw else DEFAULT_BUDGET_SECONDS
        except ValueError:
            print(f"[budget] {BUDGET_ENV}={raw!r} is not a number - "
                  f"using the {DEFAULT_BUDGET_SECONDS:.0f}s default")
            seconds = DEFAULT_BUDGET_SECONDS
    if seconds is None or seconds <= 0:
        _deadline = None
        return 0.0
    _deadline = time.monotonic() + seconds
    return seconds


def clear_budget() -> None:
    global _deadline
    _deadline = None


def remaining_budget() -> float | None:
    """Seconds left, or None when no budget is running."""
    if _deadline is None:
        return None
    return _deadline - time.monotonic()


def budget_exhausted() -> bool:
    remaining = remaining_budget()
    return remaining is not None and remaining <= 0


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
            # The ONLY place the run budget is consulted, and deliberately so:
            # immediately before a sleep, never during work. Sleeping `delay`
            # and then making another attempt has to fit in what is left, or
            # the ladder is just buying billed minutes to arrive at the same
            # failure. See the budget notes at the top of this module.
            left = remaining_budget()
            if left is not None and left <= delay:
                record_degradation(
                    "retry-budget",
                    f"{label} failed on attempt {attempt}/{attempts} with "
                    f"{type(e).__name__}, and the run's retry budget has "
                    f"{max(0.0, left):.0f}s left against a {delay:.0f}s backoff",
                    "stopped retrying instead of spending the rest of the "
                    "budget arriving at the same failure",
                )
                break
            print(f"[retry] {label}: {type(e).__name__}: {str(e)[:140]} "
                  f"(attempt {attempt}/{attempts}) — retrying in {delay:.0f}s")
            time.sleep(delay)
    raise last
