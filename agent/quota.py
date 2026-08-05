"""
Self-tracked YouTube Data API quota usage.

Google's API does not expose real-time "quota remaining" anywhere - the
only way to know how much of today's 10,000-unit allowance is left is to
track our own spend. This is therefore a conservative SELF-ESTIMATE, not an
authoritative reading: it only counts the calls this codebase itself makes
(videos.list + commentThreads.list, 1 unit each - see youtube_stats.py, plus
videos.insert at 1,600 and videos.delete at 50 - see upload.py), and cannot
account for any other use on the same Google Cloud project.

Persisted (committed) so it survives the clean-checkout runner and stays
shared across the two workflows that both spend from it. Resets on the
API's OWN boundary - midnight Pacific (see upload.py's note on this) - not
UTC and not whatever timezone a given runner happens to be in.
"""
import json
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

LEDGER_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "quota_ledger.json")
DAILY_CAP = 10_000
PACIFIC = ZoneInfo("America/Los_Angeles")
# Real cost of one reading: videos.list (1 unit) + commentThreads.list (1 unit)
# + fetch_retention (1 unit). The retention scope (yt-analytics.readonly) is
# active and retention queries now succeed, reaching the Analytics API, so
# each reading costs 3 units.
UNITS_PER_READING = 3
# videos.insert is by far the most expensive call this project makes - 1,600
# units, 16% of the entire daily allowance for ONE upload. Four scheduled
# uploads a day is 6,400 units before a single view has been read.
UNITS_PER_UPLOAD = 1_600
# videos.delete. Cheap next to the insert, but a delete+re-upload pair costs
# 1,650 and that is the number the re-upload path has to budget for.
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
        return {"pacific_date": _pacific_today(), "units_used": 0, "uploads_recorded": []}
    with open(LEDGER_PATH) as f:
        ledger = json.load(f)
    if ledger.get("pacific_date") != _pacific_today():
        # A new Pacific day - matching Google's own reset boundary - starts
        # the count over rather than carrying yesterday's spend forward.
        return {"pacific_date": _pacific_today(), "units_used": 0, "uploads_recorded": []}
    ledger.setdefault("uploads_recorded", [])
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
    """Records one videos.insert against today's allowance, keyed by video id
    so it can never be double-counted. Idempotent on purpose: the upload path,
    the re-upload path and the reconciliation sweep below all call it for the
    same video, and a ledger that grows by 1,600 every time the dashboard
    rebuilds would be worse than one that under-counts. Returns True if this
    call is what recorded it."""
    ledger = _load()
    if video_id and video_id in ledger["uploads_recorded"]:
        return False
    ledger["units_used"] += UNITS_PER_UPLOAD
    if video_id:
        ledger["uploads_recorded"].append(video_id)
    _save(ledger)
    return True


def reconcile_uploads(records: list[dict]) -> int:
    """Books any of TODAY's uploads that are not in the ledger yet, and returns
    the units added. Takes records rather than importing store so this module
    stays dependency-free.

    This exists because the ledger was blind to uploads until now: every video
    published before this function existed cost 1,600 real units that were
    never written down. Only today's matter - the ledger resets at midnight
    Pacific, so yesterday's unbooked uploads are already forgiven - but for
    today they are the difference between "179 units used" and "1,779 used",
    which is the difference between a re-upload being safe and it running the
    account into the cap."""
    today = _pacific_today()
    added = 0
    for record in records:
        video_id = record.get("video_id")
        if not video_id:
            continue
        if _pacific_date_of(record.get("uploaded_at", "")) != today:
            continue
        if record_upload(video_id):
            added += UNITS_PER_UPLOAD
    return added


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
    30-day final-refreshes are pending."""
    return units_used_today() < DAILY_CAP * reserve_fraction


def has_headroom_for(units: int, reserve_fraction: float = 0.7) -> bool:
    """The same 70% reserve, asked forward: would spending `units` now still
    leave the day under the reserve line? For DISCRETIONARY spend - a re-upload
    the operator chose to trigger - where the 3,000-unit buffer for the rest of
    the day's scheduled reads must survive the decision."""
    return units_used_today() + units <= DAILY_CAP * reserve_fraction


def fits_in_cap(units: int) -> bool:
    """Whether `units` fits in what is left of the hard cap, ignoring the
    reserve. For NON-discretionary spend - the scheduled upload the pipeline
    exists to make - which should be refused only when it genuinely cannot
    succeed, not merely because the day is past its comfort threshold."""
    return units_used_today() + units <= DAILY_CAP
