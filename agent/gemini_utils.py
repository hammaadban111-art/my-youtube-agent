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
"""
import time
from google.genai import errors as genai_errors

# 429 (rate limit) and 5xx (server-side) are worth retrying; 400/401/403/404
# are request problems that will never succeed on retry.
RETRYABLE_CODES = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 6
BASE_DELAY_SECONDS = 10
MAX_DELAY_SECONDS = 160


def call_with_retry(fn, *, label: str):
    """Runs fn() with exponential backoff on transient Gemini errors.
    Re-raises immediately on anything not in RETRYABLE_CODES."""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return fn()
        except genai_errors.APIError as e:
            if e.code not in RETRYABLE_CODES or attempt == MAX_ATTEMPTS:
                raise
            delay = min(BASE_DELAY_SECONDS * (2 ** (attempt - 1)), MAX_DELAY_SECONDS)
            print(f"[gemini] {label}: {e.code} {e.status} (attempt {attempt}/"
                  f"{MAX_ATTEMPTS}) — retrying in {delay}s")
            time.sleep(delay)
