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
"""
import time
from google.genai import errors as genai_errors

# 429 (rate limit) and 5xx (server-side) are worth retrying; 400/401/403/404
# are request problems that will never succeed on retry.
RETRYABLE_CODES = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 6
BASE_DELAY_SECONDS = 10
MAX_DELAY_SECONDS = 160

# Both free-tier, no billing required — never swap in a "-pro" model here.
PRIMARY_MODEL = "gemini-flash-latest"
FALLBACK_MODEL = "gemini-flash-lite-latest"
FALLBACK_MAX_ATTEMPTS = 3


def _run_with_backoff(fn, model, max_attempts, label):
    for attempt in range(1, max_attempts + 1):
        try:
            return fn(model)
        except genai_errors.APIError as e:
            if e.code not in RETRYABLE_CODES or attempt == max_attempts:
                raise
            delay = min(BASE_DELAY_SECONDS * (2 ** (attempt - 1)), MAX_DELAY_SECONDS)
            print(f"[gemini] {label} ({model}): {e.code} {e.status} (attempt "
                  f"{attempt}/{max_attempts}) — retrying in {delay}s")
            time.sleep(delay)


def call_with_retry(fn, *, label: str):
    """Runs fn(model) with exponential backoff on transient Gemini errors,
    trying PRIMARY_MODEL first. If PRIMARY_MODEL exhausts every retry still
    hitting a retryable error, falls back to FALLBACK_MODEL with its own
    (shorter) retry budget before giving up. Re-raises immediately on
    anything not in RETRYABLE_CODES, from whichever model raised it."""
    try:
        return _run_with_backoff(fn, PRIMARY_MODEL, MAX_ATTEMPTS, label)
    except genai_errors.APIError as e:
        if e.code not in RETRYABLE_CODES:
            raise
        print(f"[gemini] {label}: {PRIMARY_MODEL} exhausted all retries, "
              f"falling back to {FALLBACK_MODEL}")
        return _run_with_backoff(fn, FALLBACK_MODEL, FALLBACK_MAX_ATTEMPTS, label)
