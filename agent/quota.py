"""
Self-tracked YouTube Data API quota usage.

Google's API does not expose real-time "quota remaining" anywhere - the
only way to know how much of today's 10,000-unit allowance is left is to
track our own spend. This is therefore a conservative SELF-ESTIMATE, not an
authoritative reading: it only counts the calls this codebase itself makes
(videos.list + commentThreads.list, 1 unit each - see youtube_stats.py), and
cannot account for any other use on the same Google Cloud project.

Persisted (committed) so it survives the clean-checkout runner and stays
shared across the two workflows that both spend from it. Resets on the
API's OWN boundary - midnight Pacific (see upload.py's note on this) - not
UTC and not whatever timezone a given runner happens to be in.
"""
import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

LEDGER_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "quota_ledger.json")
DAILY_CAP = 10_000
PACIFIC = ZoneInfo("America/Los_Angeles")
# Real cost of one reading: videos.list (1 unit) + commentThreads.list (1 unit)
# + fetch_retention (1 unit). The retention scope (yt-analytics.readonly) is
# active and retention queries now succeed, reaching the Analytics API, so
# each reading costs 3 units.
UNITS_PER_READING = 3


def _pacific_today() -> str:
    return datetime.now(PACIFIC).strftime("%Y-%m-%d")


def _load() -> dict:
    if not os.path.exists(LEDGER_PATH):
        return {"pacific_date": _pacific_today(), "units_used": 0}
    with open(LEDGER_PATH) as f:
        ledger = json.load(f)
    if ledger.get("pacific_date") != _pacific_today():
        # A new Pacific day - matching Google's own reset boundary - starts
        # the count over rather than carrying yesterday's spend forward.
        return {"pacific_date": _pacific_today(), "units_used": 0}
    return ledger


def _save(ledger: dict) -> None:
    os.makedirs(os.path.dirname(LEDGER_PATH), exist_ok=True)
    with open(LEDGER_PATH, "w") as f:
        json.dump(ledger, f, indent=2)


def record_units(n: int) -> None:
    ledger = _load()
    ledger["units_used"] += n
    _save(ledger)


def units_used_today() -> int:
    return _load()["units_used"]


def has_headroom(reserve_fraction: float = 0.7) -> bool:
    """True if today's tracked usage is still below reserve_fraction of the
    daily cap. Reuses the same 70% "warning" threshold this codebase already
    shows on the CI-minutes budget (agent/dashboard.py) rather than inventing
    a new number. Leaves the remaining 30% (3,000 units) as a buffer for the
    rest of the day's regular reads, regardless of how many discretionary
    30-day final-refreshes are pending."""
    return units_used_today() < DAILY_CAP * reserve_fraction
