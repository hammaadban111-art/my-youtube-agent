"""
Self-tracked YouTube Data API quota usage.

Google's API does not expose real-time "quota remaining" anywhere - the
only way to know how much of today's allowance is left is to track our own spend.

OBSERVED REALITY 2026-08-12 (from Google Cloud Console, youtubve-503911):
There are THREE entirely separate quota pools, not one:
1. YouTube Data API v3 "Queries per day": Limit 10,000. Used by videos.list,
   commentThreads.list (1 unit each), videos.delete (50 units).
2. YouTube Data API v3 "Video Uploads per day": Limit 100. videos.insert uses ONE
   slot of this, and ZERO units of the 10,000 pool.
3. YouTube Analytics API "Queries per day": Limit 100,000. fetch_retention uses
   this. It does not touch the 10,000 pool either (observed 0.24% used).

This ledger tracks the 10,000 Data API pool (units_used) and the 100 Uploads
pool (uploads_recorded). It resets on the API's OWN boundary - midnight Pacific
(see upload.py's note on this) - not UTC.
"""
import json
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

LEDGER_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "quota_ledger.json")
DAILY_CAP = 10_000
PACIFIC = ZoneInfo("America/Los_Angeles")
# Real cost of one reading: videos.list (1 unit) + commentThreads.list (1 unit).
# The retention query (fetch_retention) goes to the YouTube Analytics API, which
# has its own 100,000/day pool (observed 2026-08-12 at 0.24% used). Therefore one
# reading costs 2 Data API units, not 3. followup.py has only ever BOOKED 2 per
# reading.
UNITS_PER_READING = 2

# Observed 2026-08-12: videos.insert does NOT consume from the 10,000 Data API cap.
# It draws from a separate 'Video Uploads per day' quota of 100 slots.
UPLOADS_PER_DAY_CAP = 100

# videos.delete IS a Data API write and does draw on the 10,000.
UNITS_PER_DELETE = 50


def _pacific_today() -> str:
    return datetime.now(PACIFIC).strftime("%Y-%m-%d")


def _pacific_date_of(iso_utc: str) -> str | None:
    """The Pacific calendar date an ISO-8601 UTC timestamp falls on. The ledger
    resets on Google's boundary, so a video uploaded at 04:26Z counts against
    the PREVIOUS Pacific day - which is exactly the off-by-one that makes an
    upload look untracked when it was simply spent on yesterday's allowance."""
    if not iso_utc:
        return None
    try:
        dt = datetime.strptime(iso_utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None
    return dt.astimezone(PACIFIC).strftime("%Y-%m-%d")


def _load() -> dict:
    if not os.path.exists(LEDGER_PATH):
        return {"pacific_date": _pacific_today(), "units_used": 0,
                "uploads_recorded": [], "failed_upload_attempts": 0}
    with open(LEDGER_PATH) as f:
        ledger = json.load(f)
    if ledger.get("pacific_date") != _pacific_today():
        # A new Pacific day - matching Google's own reset boundary - starts
        # the count over rather than carrying yesterday's spend forward.
        return {"pacific_date": _pacific_today(), "units_used": 0,
                "uploads_recorded": [], "failed_upload_attempts": 0}
    ledger.setdefault("uploads_recorded", [])
    ledger.setdefault("failed_upload_attempts", 0)
    return ledger


def _save(ledger: dict) -> None:
    os.makedirs(os.path.dirname(LEDGER_PATH), exist_ok=True)
    with open(LEDGER_PATH, "w") as f:
        json.dump(ledger, f, indent=2)


def record_units(n: int) -> None:
    ledger = _load()
    ledger["units_used"] += n
    _save(ledger)


def record_upload(video_id: str) -> bool:
    """Records one upload against today's 100-slot allowance, keyed by video id
    so it can never be double-counted. Idempotent on purpose.
    NO LONGER adds units to units_used, because uploads do not cost Data API
    units. Records purely so the day's upload count can be known.
    Returns True if this call is what recorded it."""
    ledger = _load()
    if video_id and video_id in ledger["uploads_recorded"]:
        return False
    if video_id:
        ledger["uploads_recorded"].append(video_id)
    _save(ledger)
    return True


def reconcile_uploads(records: list[dict]) -> int:
    """Books any of TODAY's uploads that are not in the ledger yet, and returns
    the count newly recorded. Takes records rather than importing store so this
    module stays dependency-free."""
    today = _pacific_today()
    added_count = 0
    for record in records:
        video_id = record.get("video_id")
        if not video_id:
            continue
        if _pacific_date_of(record.get("uploaded_at", "")) != today:
            continue
        if record_upload(video_id):
            added_count += 1
    return added_count


def units_used_today() -> int:
    return _load()["units_used"]


def remaining_units() -> int:
    """Units left against the hard 10,000 cap. Never negative - a ledger that
    over-counts (see record_upload's note on failed inserts) should read as
    "nothing left", not as a negative allowance."""
    return max(0, DAILY_CAP - units_used_today())


def has_headroom(reserve_fraction: float = 0.7) -> bool:
    """True if today's tracked usage is still below reserve_fraction of the
    daily cap. Reuses the same 70% "warning" threshold this codebase already
    shows on the CI-minutes budget (agent/dashboard.py) rather than inventing
    a new number. Leaves the remaining 30% (3,000 units) as a buffer for the
    rest of the day's regular reads, regardless of how many discretionary
    30-day final-refreshes are pending.

    NOTE: This does NOT govern uploads, which have a separate 100/day pool."""
    return units_used_today() < DAILY_CAP * reserve_fraction


def has_headroom_for(units: int, reserve_fraction: float = 0.7) -> bool:
    """The same 70% reserve, asked forward: would spending `units` now still
    leave the day under the reserve line? For DISCRETIONARY spend - a re-upload
    the operator chose to trigger - where the 3,000-unit buffer for the rest of
    the day's scheduled reads must survive the decision.

    NOTE: This does NOT govern uploads, which have a separate 100/day pool."""
    return units_used_today() + units <= DAILY_CAP * reserve_fraction


def fits_in_cap(units: int) -> bool:
    """Whether `units` fits in what is left of the hard cap, ignoring the
    reserve. For NON-discretionary spend.

    NOTE: This does NOT govern uploads, which have a separate 100/day pool."""
    return units_used_today() + units <= DAILY_CAP


def record_failed_upload() -> None:
    """Books one videos.insert that reached the API and came back an error.

    Counted, deliberately, even though nobody has confirmed whether Google
    charges a failed insert against the 100/day slot pool - the console shows
    the pool's TOTAL, not what each request did to it. Booking it is the
    conservative reading, and this module's standing asymmetry applies: an
    over-count costs at most one skipped slot, an under-count publishes into a
    wall.

    Kept as a COUNTER rather than a synthetic entry in uploads_recorded, which
    is a list of real video ids - it is unioned by
    scripts/resolve_data_conflicts.py, read back by reconcile_uploads(), and
    would grow without bound if every failed attempt added a row to it."""
    ledger = _load()
    ledger["failed_upload_attempts"] = ledger.get("failed_upload_attempts", 0) + 1
    _save(ledger)


def uploads_today() -> int:
    """Upload slots consumed today: real uploads plus failed inserts that
    reached the API (see record_failed_upload)."""
    ledger = _load()
    return len(ledger.get("uploads_recorded", [])) + ledger.get("failed_upload_attempts", 0)


def upload_slots_remaining() -> int:
    """Upload slots left against the 100/day cap."""
    return max(0, UPLOADS_PER_DAY_CAP - uploads_today())


def can_upload() -> bool:
    """True if there is at least one upload slot left today."""
    return upload_slots_remaining() > 0
