"""
Persistent per-video records, committed to git so they survive between
GitHub Actions runs (which always start from a clean checkout).

One JSON file per video, sharded by month:
    data/videos/2026-07/<video_id>.json

Deliberately NOT a single JSON file or a committed SQLite database: the
upload workflow and the hourly follow-up workflow write concurrently, and
they touch *different* files here, so git merges them cleanly. A binary
SQLite file cannot be merged at all — one run would silently clobber the
other's data — and a single shared JSON file has the same contention in a
milder form. At ~1,100 small files/year, scanning the directory is cheap.
"""
import glob
import json
import os
from datetime import datetime, timedelta, timezone

SCHEMA_VERSION = 1
DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "videos")

# Which era of the pipeline produced a video. Bumped by hand when a change
# lands that could plausibly move the numbers, so later analysis can compare
# like with like instead of averaging across eras that aren't comparable.
# The channel hit a real distribution throttle on 2026-07-30 after too many
# uploads in too short a window; videos from that window are near-zero-view
# for reasons that have nothing to do with their content, and silently mixing
# them into "how are we doing" is how a wrong conclusion gets drawn.
SYSTEM_VERSION = "phase1-stabilize"
# Stamped onto records that predate this field entirely.
LEGACY_SYSTEM_VERSION = "pre-phase0"

# How long after upload we take the first "real" view-count reading.
MEASURE_AFTER_HOURS = 5
# Age at which a video stops being refreshed every run and instead gets ONE
# more attempt (quota permitting - see agent/quota.py) before freezing for
# good. There used to be no cutoff at all: two earlier ones (a 7-day window,
# a per-video reading cap) silently froze a video's numbers while the real
# video kept accumulating views on YouTube for months, with nothing on
# screen to say so. Both were removed entirely rather than fixed, because
# removing only one moved the freeze from day 7 to day 12 without removing
# it.
#
# This cutoff is different in kind, not a reintroduction of those: it is
# DELIBERATE and LABELLED - a frozen video's dashboard card says so in plain
# text ("final as of 30 days") rather than just quietly stopping.
AGE_FREEZE_DAYS = 30
#
# UPDATE 2026-08-02: there used to also be a recheck-interval TIER here (every
# 3h under 48h old, daily after) so an old video cost one reading a day rather
# than one per run. That tiering was removed by request in favor of "every
# eligible video, every run" - then THIS 30-day freeze was added the same day
# because that made the cost of following a video forever scale with the
# CHANNEL'S TOTAL LIFETIME video count, which grows without bound. Freezing
# at 30 days instead bounds the "actively refreshed" population to roughly
# (uploads/day x 30), which does NOT grow as the channel ages - the real
# numbers, computed 2026-08-02 against this channel's actual 4 uploads/day
# and 12 refresh-runs/day (4 upload + 8 follow-up):
#
#   steady-state active population   = 4 x 30                = 120 videos
#   steady-state daily reading cost  = 120 x 2 units x 12 runs = 2,880 units/day
#   + ~4/day videos aging into a one-time final read x 2 units =    +8 units/day
#   -----------------------------------------------------------------------
#   steady-state total                                        ~ 2,888 units/day
#
# against the 10,000/day cap - under 29%, FOREVER, regardless of how large
# the channel's total lifetime video count grows. (Units-per-reading and the
# runs/day figure are documented next to their own sources: agent/quota.py
# and .github/workflows/daily.yml + followup.yml.) The same math run backwards
# says this holds up to ~13.8 uploads/day before the cap binds at all - about
# 3.5x this channel's current pace.


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_ts(value: str) -> datetime:
    """Parses an ISO timestamp, tolerating the trailing 'Z' form."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_record(video_id: str, script: dict, uploaded_at: datetime = None) -> dict:
    uploaded_at = uploaded_at or _utcnow()
    return {
        "schema_version": SCHEMA_VERSION,
        "system_version": SYSTEM_VERSION,
        "video_id": video_id,
        "title": script.get("title", ""),
        "url": f"https://youtube.com/watch?v={video_id}",
        "uploaded_at": iso(uploaded_at),
        "topic_subject": script.get("topic_subject", ""),
        "prediction": {},
        # The FIRST reading (~5h after upload), frozen once set - this is
        # what predict.py trains on, so predictions stay comparable to a
        # consistent point in a video's life rather than drifting as later
        # readings come in. See latest_measurement for the current numbers.
        "measurement": {
            "measured_at": None,
            "hours_after_upload": None,
            "actual_views": None,
            "likes": None,
            "comment_count": None,
        },
        # The MOST RECENT reading (same shape as measurement) - what the
        # dashboard displays as "actual", since a video keeps accumulating
        # views for days after the first check.
        "latest_measurement": {
            "measured_at": None,
            "hours_after_upload": None,
            "actual_views": None,
            "likes": None,
            "comment_count": None,
        },
        # Lightweight timestamp+views point for every reading taken, for a
        # future per-video growth curve. Kept separate from the full
        # measurement dicts above so this doesn't duplicate the (larger)
        # retention curve on every entry - which matters more now that this
        # list grows for the life of the video rather than stopping at 24.
        "measurement_history": [],
        "comments": [],
        "script": {},
        "grounding": {},
        # Set by freeze_record() once this video crosses AGE_FREEZE_DAYS -
        # False for every video's whole active life before that.
        "final": False,
        "finalized_at": None,
    }


def record_path(record: dict) -> str:
    month = parse_ts(record["uploaded_at"]).strftime("%Y-%m")
    return os.path.join(DATA_DIR, month, f"{record['video_id']}.json")


def save_record(record: dict) -> str:
    path = record_path(record)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(record, f, indent=2)
    return path


def all_records() -> list[dict]:
    """Every record on disk, oldest first."""
    records = []
    for path in glob.glob(os.path.join(DATA_DIR, "*", "*.json")):
        with open(path) as f:
            records.append(json.load(f))
    return sorted(records, key=lambda r: r["uploaded_at"])


def measured_records() -> list[dict]:
    """Records that have a real view count recorded."""
    return [r for r in all_records() if r["measurement"].get("actual_views") is not None]


def _effective_history(record: dict) -> list[dict]:
    """The record's reading history as [{measured_at, actual_views}, ...].

    Falls back to synthesizing a 1-entry history from the legacy single
    `measurement` field for records written before measurement_history
    existed, so an old record resumes the recheck cycle from wherever it
    already was instead of losing its first reading or restarting from zero."""
    history = record.get("measurement_history")
    if history:
        return list(history)
    old = record.get("measurement") or {}
    if old.get("actual_views") is not None:
        return [{"measured_at": old.get("measured_at"), "actual_views": old.get("actual_views")}]
    return []


def record_measurement(record: dict, reading: dict) -> None:
    """Applies one point-in-time reading to a record: appends it to the
    lightweight history, updates latest_measurement to it, and — only the
    very first time — freezes it into `measurement` too, since that field is
    what predict.py trains on and must stay pinned to a consistent point in
    each video's life rather than drifting as later readings come in."""
    history = _effective_history(record)
    is_first = not history
    history.append({"measured_at": reading.get("measured_at"),
                     "actual_views": reading.get("actual_views")})
    record["measurement_history"] = history
    record["latest_measurement"] = reading
    if is_first:
        record["measurement"] = reading


def is_final(record: dict) -> bool:
    return bool(record.get("final"))


def measurable_records(now: datetime = None) -> list[dict]:
    """Every record eligible for a fresh reading right now: past its
    first-measurement age, under AGE_FREEZE_DAYS old, and not already frozen.
    No recheck-interval tiering - every eligible video is refreshed on every
    run (upload or follow-up), so every dashboard card reflects the latest
    known numbers every time the site rebuilds. See the cost-math note above
    AGE_FREEZE_DAYS for why this stays affordable forever rather than
    growing with the channel's total lifetime video count.

    Videos AGE_FREEZE_DAYS or older are handled separately by
    due_for_final_refresh() instead of here - see that function.
    """
    now = now or _utcnow()
    return [r for r in all_records()
            if not is_final(r)
            and now - parse_ts(r["uploaded_at"]) >= timedelta(hours=MEASURE_AFTER_HOURS)
            and now - parse_ts(r["uploaded_at"]) < timedelta(days=AGE_FREEZE_DAYS)]


def due_for_final_refresh(now: datetime = None) -> list[dict]:
    """Videos that just crossed AGE_FREEZE_DAYS and have not been frozen yet -
    each gets exactly ONE more decision (not a retry loop): take a final
    reading if there's quota headroom that day, or freeze immediately at
    whatever numbers are already on record if there isn't. Either way the
    record is marked final and never appears here (or in measurable_records)
    again."""
    now = now or _utcnow()
    return [r for r in all_records()
            if not is_final(r)
            and now - parse_ts(r["uploaded_at"]) >= timedelta(days=AGE_FREEZE_DAYS)]


def freeze_record(record: dict, now: datetime = None) -> None:
    """Marks a record final - no more reads, ever, regardless of whether this
    call also took a fresh reading first. Saves the record itself."""
    now = now or _utcnow()
    record["final"] = True
    record["finalized_at"] = iso(now)
    save_record(record)


def recent_upload_count(hours: int, now: datetime = None) -> int:
    """How many videos were uploaded in the last `hours`.

    Counts saved records, which are written only after a successful upload,
    so this is a count of what actually reached YouTube - not of attempts.
    """
    now = now or _utcnow()
    cutoff = now - timedelta(hours=hours)
    # Bounded at BOTH ends. Without the upper bound, evaluating an earlier
    # point in time counts uploads that hadn't happened yet, which silently
    # makes any historical replay meaningless (and would misread clock skew
    # on a runner as a burst).
    return sum(1 for r in all_records()
               if cutoff <= parse_ts(r["uploaded_at"]) <= now)


def days_of_history(now: datetime = None) -> int:
    """Whole days between the first upload and now. Drives the day-5 gate."""
    records = all_records()
    if not records:
        return 0
    now = now or _utcnow()
    return (now - parse_ts(records[0]["uploaded_at"])).days
