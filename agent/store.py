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
# How long a video stays on the every-run cadence. 48h because that is
# where this channel's view curve flattens - the first two days are when
# the numbers actually move and when a dashboard refresh is worth its 2
# units; after that a once-a-day reading loses nothing anyone watches.
FRESH_WINDOW_HOURS = 48
# The minimum gap between readings on the daily tier. Deliberately 20,
# NOT 24. Runs land every ~3h, so a 24h gate means a video read at 12:00
# is not eligible until 12:00 the next day, and the first run at-or-after
# that is 14:00 - which pushes the next window to 14:00, then 17:00, and
# so on until a day gets skipped entirely. 20h absorbs that drift and
# still yields one reading per day.
DAILY_RECHECK_AFTER_HOURS = 20
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
# UPDATE 2026-08-12: The tiering was reintroduced earlier today based on the
# false premise that a reading cost 3 units and an upload cost 1,600. It is now
# known that a reading costs 2 units, and uploads contribute ZERO to this pool
# (they are governed by a separate 100/day slot quota, of which this channel uses 4).
#
# The untiered steady state is actually comfortably fine:
#   120 active videos x 2 units x 12 runs/day = 2,880 reads/day (29% of 10,000 cap).
# This is exactly what the ORIGINAL 2026-08-02 note said before it was "corrected"
# on wrong grounds.
#
# The tiered steady state (what the code now does) uses even less:
#   8 active videos under 48h x 2 units x 12 runs/day = 192 reads/day
#   112 older videos x 2 units x 1 run/day            = 224 reads/day
#   -------------------------------------------------------------
#   steady-state total                                = 416 units/day (~4% of cap)
#
# The daily reading tier introduced on 2026-08-12 is therefore NOT required by
# quota pressure — it was adopted on a false premise. It is retained only
# because fewer needless API calls is still better.


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


def last_reading_at(record: dict) -> datetime | None:
    """When this video was last measured, or None if it never was.

    Falls back to the reading history for records written before
    latest_measurement existed. A timestamp that is missing or unparseable
    also reads as None, and None means DUE at the call site below - a record
    that cannot say when it was last read must be measured again, never
    silently skipped forever."""
    latest = record.get("latest_measurement") or {}
    measured_at = latest.get("measured_at")
    if not measured_at:
        history = _effective_history(record)
        if history and history[-1].get("measured_at"):
            measured_at = history[-1]["measured_at"]
    if not measured_at:
        return None
    try:
        return parse_ts(measured_at)
    except (ValueError, TypeError):
        return None


def reading_cadence(record: dict, now: datetime = None) -> str:
    """Which tier this video is on: "final", "every-run", or "daily".

    One definition, shared by measurable_records() below, followup.sweep()'s
    log line and the dashboard card - so the cadence a viewer is TOLD about
    can never drift from the cadence the pipeline actually applies."""
    if is_final(record):
        return "final"
    now = now or _utcnow()
    try:
        uploaded_at = parse_ts(record["uploaded_at"])
    except (ValueError, TypeError):
        # Unreadable timestamp: report the more frequent tier rather than the
        # slower one. measurable_records() reports the same record as a
        # problem, and over-reading one broken record is the cheap error.
        return "every-run"
    if (now - uploaded_at) >= timedelta(hours=FRESH_WINDOW_HOURS):
        return "daily"
    return "every-run"


def measurable_records(now: datetime = None) -> list[dict]:
    """Every record eligible for a fresh reading right now: past its
    first-measurement age, under AGE_FREEZE_DAYS old, and not already frozen.
    Additionally, videos past FRESH_WINDOW_HOURS are placed on a daily tier
    instead of being checked every run. This tiering was reintroduced on
    2026-08-12 because reading 120 videos 12 times a day costs 4,320 units,
    which alongside 4 uploads/day (6,400 units) exceeded the 10,000/day
    quota cap and caused scheduled uploads to be refused.

    Videos AGE_FREEZE_DAYS or older are handled separately by
    due_for_final_refresh() instead of here - see that function.
    """
    now = now or _utcnow()
    eligible = []
    for r in all_records():
        if is_final(r):
            continue
        try:
            age = now - parse_ts(r["uploaded_at"])
        except (ValueError, TypeError, KeyError) as e:
            # Said out loud rather than skipped quietly. A record with an
            # unreadable uploaded_at can be measured by nothing here and
            # finalized by nothing in due_for_final_refresh() either, so it
            # would drop off the channel's books entirely - which is the exact
            # failure mode (a video whose numbers silently stop) that the
            # freeze rules above exist to prevent.
            print(f"[store] skipping {r.get('video_id', '?')}: unreadable "
                  f"uploaded_at ({type(e).__name__}: {e})")
            continue

        if age < timedelta(hours=MEASURE_AFTER_HOURS):
            continue
        if age >= timedelta(days=AGE_FREEZE_DAYS):
            continue

        if age < timedelta(hours=FRESH_WINDOW_HOURS):
            eligible.append(r)
            continue

        # The daily tier. Never measured counts as due at any age under the
        # freeze - a video that missed its first reading because a run failed
        # must not be held back another 20h on top of that.
        last_read = last_reading_at(r)
        if last_read is None or (now - last_read) >= timedelta(hours=DAILY_RECHECK_AFTER_HOURS):
            eligible.append(r)
    return eligible


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
