"""
Shared retry wrapper for Gemini API calls.

The GitHub Actions run at 2026-07-29T16:48Z failed outright on a single
google.genai.errors.ServerError: 503 UNAVAILABLE ("This model is currently
experiencing high demand") from script_writer's one Gemini call — a
transient, Google-side condition, not a bad request. Neither Gemini call
site (script_writer.generate_script, grounding.verify_claims) had any
retry, so one momentary overload aborted the entire 7-step pipeline for
that scheduled slot.

The initial 4-attempt/35-second window proved too short for runs at
2026-08-14T17:00Z and 2026-08-15T11:28Z, which both exhausted all attempts
against 503s. The workflow now has a 45-minute ceiling and a normal run
takes ~11 minutes, so we can afford roughly five minutes of backoff.

That still wasn't enough: five separate runs between 2026-08-16T11:35Z and
2026-08-17T16:44Z each exhausted all 6 PRIMARY_MODEL retries against 503
UNAVAILABLE — a sustained overload on that specific model, not a momentary
blip a longer wait would ride out. Confirmed live on 2026-08-18: while
PRIMARY_MODEL ("gemini-flash-latest") was refusing every attempt across
those runs, FALLBACK_MODEL ("gemini-flash-lite-latest") answered instantly
under the same key/quota — a separate model id sits on separate serving
capacity, so it can succeed during an outage specific to the primary one.
call_with_retry now falls back to it once PRIMARY_MODEL's own retry budget
is exhausted, before giving up for real.

Three further holes, each confirmed against a real failed run, are closed here:

1. TRANSPORT ERRORS WERE NOT RETRIED AT ALL. Run 33476553815 (2026-09-01T06:20Z)
   died on `httpx.RemoteProtocolError: Server disconnected without sending a
   response.` raised from _request_once. That is not a google.genai APIError, so
   the `except genai_errors.APIError` below never saw it and the very first
   disconnect propagated out of attempt 1 and killed the run. A server hanging
   up mid-request is the most transient failure there is — strictly more
   retryable than the 503s this module was written for. Both the httpx
   exceptions and their httpcore originals are now retried.

2. NO JITTER. Every retry slept exactly 10, 20, 40, 80... seconds. Four daily
   slots, the 3-hourly follow-up sweep and the weekly maintenance job all share
   one API key against one overloaded model, so lock-step ladders re-collide on
   the same second on every rung. Delays are now "equal jitter" — half the
   nominal delay plus a random share of the other half — which keeps the
   backoff growing while spreading the retries out.

3. THE BUDGET WAS TOO SHORT FOR THE JOB CEILING. Run 33886375100
   (2026-09-04T14:53Z) burned its whole ladder — 6 primary attempts against
   503/429 then 2 fallback attempts — in about six minutes, then gave up and
   lost the slot, inside a job allowed to run for 45. A normal run is ~11
   minutes, so there were ~28 unused minutes to wait in. The ladders are wider
   now and bounded by explicit wall-clock budgets (per call and per process)
   rather than by attempt count alone, so a widened ladder can never push the
   job into its own 45-minute timeout.
"""
import os
import random
import time

from google.genai import errors as genai_errors

# A dropped connection or a read timeout is a transport-layer failure: the
# request never got a verdict, so retrying it is always safe and always worth
# doing. httpx.TransportError is the base of RemoteProtocolError, ConnectError,
# ReadTimeout, ConnectTimeout and PoolTimeout. httpx normally maps its httpcore
# originals, but the httpcore classes do not inherit from it, so they are
# caught alongside rather than assumed to be wrapped.
_TRANSPORT_ERRORS: tuple = (ConnectionError, TimeoutError)
try:
    import httpx

    _TRANSPORT_ERRORS += (httpx.TransportError,)
except ImportError:  # pragma: no cover - httpx ships with google-genai
    pass
try:
    import httpcore

    _TRANSPORT_ERRORS += (httpcore.ProtocolError, httpcore.TimeoutException,
                          httpcore.NetworkError)
except ImportError:  # pragma: no cover - httpcore ships with httpx
    pass

# 429 (rate limit) and 5xx (server-side) are worth retrying; 400/401/403/404
# are request problems that will never succeed on retry.
RETRYABLE_CODES = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 7
BASE_DELAY_SECONDS = 10
MAX_DELAY_SECONDS = 240

# Both free-tier, no billing required — never swap in a "-pro" model here.
PRIMARY_MODEL = "gemini-flash-latest"
FALLBACK_MODEL = "gemini-flash-lite-latest"
FALLBACK_MAX_ATTEMPTS = 5

# Wall-clock ceilings, the real guard on the widened ladders above.
#
# LADDER_BUDGET_SECONDS bounds one model's ladder. Primary's six sleeps come to
# at most 10+20+40+80+160+240 = 550s, so 600 covers a full ladder with room for
# the requests themselves. Each ladder gets its OWN budget rather than sharing
# one: a shared budget would let the primary's nine minutes starve the fallback,
# and the fallback is the rung that actually recovered the 2026-08-16/17 outage.
#
# PROCESS_BUDGET_SECONDS is the global cap, and the one that matters. The job
# times out at 45 minutes (.github/workflows/daily.yml) and a normal run takes
# ~11, so every Gemini call site in the process together — script generation,
# its corrective re-ask, and grounding — may spend at most 15 minutes waiting.
# 11 + 15 leaves nine minutes of headroom under the job ceiling.
#
# Overridable by environment variable so tests do not have to monkeypatch them.
LADDER_BUDGET_SECONDS = float(os.getenv("GEMINI_LADDER_BUDGET_SECONDS", "600"))
PROCESS_BUDGET_SECONDS = float(os.getenv("GEMINI_PROCESS_BUDGET_SECONDS", "900"))

# Spent waiting (not requesting) across every call_with_retry in this process.
_process_seconds_slept = 0.0


def reset_budget() -> None:
    """Forgets time already spent waiting. One process is one pipeline run, so
    this is only for tests and for a long-lived caller that runs the pipeline
    more than once."""
    global _process_seconds_slept
    _process_seconds_slept = 0.0


def budget_spent() -> float:
    """Seconds this process has spent sleeping between Gemini retries."""
    return _process_seconds_slept


def is_retryable(error: Exception) -> bool:
    """Whether `error` is a transient condition a later attempt could survive.

    Transport failures always are: the request never reached a verdict. API
    errors are only when the status code says the server, not the request, was
    the problem."""
    if isinstance(error, _TRANSPORT_ERRORS):
        return True
    if isinstance(error, genai_errors.APIError):
        return error.code in RETRYABLE_CODES
    return False


def _delay_for(attempt: int) -> float:
    """Equal-jitter backoff: half the nominal delay, plus a random share of the
    other half. Keeps the exponential growth (so a long outage really is waited
    out) while making it impossible for two runs that started together to keep
    retrying on the same second."""
    nominal = min(BASE_DELAY_SECONDS * (2 ** (attempt - 1)), MAX_DELAY_SECONDS)
    return nominal / 2 + random.uniform(0, nominal / 2)


def _sleep(delay: float) -> None:
    global _process_seconds_slept
    _process_seconds_slept += delay
    time.sleep(delay)


def _budget_left(ladder_deadline: float) -> float:
    """Seconds still available for waiting, under whichever budget binds first."""
    return min(
        ladder_deadline - time.monotonic(),
        PROCESS_BUDGET_SECONDS - _process_seconds_slept,
    )


def _run_with_backoff(fn, model, max_attempts, label):
    ladder_deadline = time.monotonic() + LADDER_BUDGET_SECONDS
    for attempt in range(1, max_attempts + 1):
        try:
            return fn(model)
        except Exception as e:  # noqa: BLE001 - re-raised below unless retryable
            if not is_retryable(e) or attempt == max_attempts:
                raise
            delay = _delay_for(attempt)
            # Never start a sleep the budget cannot pay for. Giving up now,
            # with the real error, beats being killed mid-wait by the job's
            # own timeout — which loses the log line explaining why.
            if delay > _budget_left(ladder_deadline):
                print(f"[gemini] {label} ({model}): {type(e).__name__} — retry "
                      f"budget exhausted, giving up after attempt {attempt}")
                raise
            detail = (f"{e.code} {e.status}" if isinstance(e, genai_errors.APIError)
                      else type(e).__name__)
            print(f"[gemini] {label} ({model}): {detail} (attempt "
                  f"{attempt}/{max_attempts}) — retrying in {delay:.0f}s")
            _sleep(delay)


def call_with_retry(fn, *, label: str):
    """Runs fn(model) with exponential backoff on transient Gemini errors,
    trying PRIMARY_MODEL first. If PRIMARY_MODEL exhausts every retry still
    hitting a retryable error, falls back to FALLBACK_MODEL with its own
    (shorter) retry budget before giving up. Re-raises immediately on
    anything not retryable, from whichever model raised it.

    The fallback still gets its first REQUEST even when the process budget is
    already spent — only sleeps are charged against it, and a different model
    id sits on different serving capacity, so that one request is the cheapest
    real chance of surviving an outage specific to the primary."""
    try:
        return _run_with_backoff(fn, PRIMARY_MODEL, MAX_ATTEMPTS, label)
    except Exception as e:  # noqa: BLE001 - re-raised below unless retryable
        if not is_retryable(e):
            raise
        print(f"[gemini] {label}: {PRIMARY_MODEL} exhausted all retries, "
              f"falling back to {FALLBACK_MODEL}")
        return _run_with_backoff(fn, FALLBACK_MODEL, FALLBACK_MAX_ATTEMPTS, label)
