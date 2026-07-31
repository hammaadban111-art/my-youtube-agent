"""
View-count prediction, plus the day-5 gate that switches the channel from
"just logging" to "learning from itself".

Three stages, deliberately in plain Python rather than a modelling library:
the sample sizes here are tiny, every prediction has to be explainable
after the fact, and adding scikit-learn to a workflow that runs 720+ times
a month is a lot of CI time for arithmetic we can do ourselves.
"""
import os
import statistics
from datetime import date

from . import store

# Used before we have any measured videos to average. Intentionally modest —
# a wrong-but-small first guess is better than an anchor pulled from nowhere.
COLD_START_PREDICTION = 25
MIN_SAMPLES_FOR_BASELINE = 3
RECENT_WINDOW = 20

# The self-improving half stays dormant until the channel has this many days
# of real posting history behind it.
SELF_IMPROVE_MIN_DAYS = 5
MIN_SAMPLES_FOR_LEARNING = 8

# Manual brake on the day-5 gate, as an ISO date (YYYY-MM-DD) in the env:
# learning stays off until this date regardless of how much history exists.
# The day-5 gate assumed the only thing worth waiting for was sample size,
# but on 2026-07-30 the channel hit a distribution throttle - videos from
# that window sit near zero views for reasons that have nothing to do with
# their topic. Letting the model start learning mid-throttle would teach it
# that whatever was posted then is "bad", permanently.
SELF_IMPROVE_AFTER = os.getenv("SELF_IMPROVE_AFTER", "").strip()

# A video at or below this view count is treated as carrying NO SIGNAL rather
# than as evidence its topic failed. Calibrated against this channel's own
# real split, not picked for roundness: healthy videos measured 999-1055
# views, throttled ones measured 0-16, so anything in single/low-double
# digits is the throttle speaking, not the audience. Excluded from topic
# multipliers entirely - counting them as "bad topics" is how a throttle
# turns into a permanently poisoned model.
NO_SIGNAL_VIEW_THRESHOLD = 25
# Shrinkage strength: how many videos a topic needs before its own average is
# trusted over the channel average. Keeps one lucky video from swinging things.
SHRINKAGE_K = 5

# How much of the topic multiplier comes from retention SHAPE rather than raw
# view count, once retention data exists. Views are the outcome, but retention
# is the leading indicator - a video that holds viewers past the hook gets
# pushed harder. Held below 0.5 so views stay the primary signal.
RETENTION_WEIGHT = 0.4
# Retention is sampled as a fraction of video length; this is the point by
# which the hook has either worked or lost the viewer.
HOOK_WINDOW = 0.25
# Below this, a topic's early hold vs the channel average counts as an early
# drop-off, not noise. Below THAT, the topic caps at low confidence even if
# the raw view count for those videos looked fine - a topic that only "worked"
# because a couple of clips got lucky on the algorithm despite losing viewers
# in the first quarter isn't one we want to lean into.
RETENTION_CONFIDENCE_FLOOR = 0.85


def _retention_quality(record: dict) -> float | None:
    """A single 0-1 score for how well one video held its audience, or None
    when retention was never captured. Uses retention at the end of the hook
    window - the segment where ~half of all swipe-aways happen."""
    retention = (record.get("measurement") or {}).get("retention") or {}
    if not retention.get("available"):
        return None
    curve = retention.get("curve") or []
    if not curve:
        return None
    within_hook = [p["watch_ratio"] for p in curve if p["position"] <= HOOK_WINDOW]
    return min(within_hook) if within_hook else curve[0]["watch_ratio"]


def _retention_multiplier(records: list[dict], subject: str) -> tuple[float | None, int]:
    """How this topic's audience retention compares to the channel's own
    average, shrunk toward 1.0 the same way the view multiplier is. Returns
    (None, 0) when there isn't enough retention data to say anything."""
    scored = [(r, _retention_quality(r)) for r in records]
    scored = [(r, q) for r, q in scored if q is not None]
    if not scored:
        return None, 0

    channel_avg = statistics.fmean(q for _, q in scored)
    if channel_avg <= 0 or not subject:
        return None, 0

    same = [q for r, q in scored
            if r.get("topic_subject", "").strip().lower() == subject.strip().lower()]
    if not same:
        return None, 0

    raw = statistics.fmean(same) / channel_avg
    n = len(same)
    return 1 + (raw - 1) * (n / (n + SHRINKAGE_K)), n


def retention_summary(limit: int = 20) -> dict:
    """Channel-wide drop-off shape for the dashboard: where viewers leave on
    average, across every video that has retention data."""
    records = store.measured_records()[-limit:]
    drops = [
        (r["measurement"]["retention"].get("biggest_drop_at"),
         r["measurement"]["retention"].get("biggest_drop_size"))
        for r in records
        if (r.get("measurement") or {}).get("retention", {}).get("available")
    ]
    drops = [(at, size) for at, size in drops if at is not None and size is not None]
    if not drops:
        return {"available": False, "n": 0}
    return {
        "available": True,
        "n": len(drops),
        "mean_drop_at": round(statistics.fmean(at for at, _ in drops), 3),
        "mean_drop_size": round(statistics.fmean(size for _, size in drops), 3),
    }


def _hold_until(today: date = None) -> str | None:
    """The SELF_IMPROVE_AFTER date if it is set and still in the future,
    otherwise None. A malformed value HOLDS rather than being ignored: the
    whole point of this brake is to stop learning on throttled data, so a
    typo must fail closed, not silently re-enable it."""
    if not SELF_IMPROVE_AFTER:
        return None
    today = today or date.today()
    try:
        hold = date.fromisoformat(SELF_IMPROVE_AFTER)
    except ValueError:
        return SELF_IMPROVE_AFTER  # unparseable - hold, and report the raw value
    return SELF_IMPROVE_AFTER if today < hold else None


def self_improve_active(now=None) -> tuple[bool, int]:
    """(active, days_of_history) — a real date check against the first upload,
    plus the manual SELF_IMPROVE_AFTER brake."""
    days = store.days_of_history(now)
    today = now.date() if hasattr(now, "date") else None
    if _hold_until(today):
        return False, days
    return days >= SELF_IMPROVE_MIN_DAYS, days


def has_signal(record: dict) -> bool:
    """Whether this video's numbers mean anything about its topic.

    A video suppressed by a platform-level distribution throttle carries no
    information about whether its subject was a good choice - it was never
    shown to enough people to find out. Treating it as a failed topic is
    strictly worse than ignoring it, because the model would then steer away
    from subjects for a reason that has nothing to do with them.
    """
    views = (record.get("measurement") or {}).get("actual_views")
    return views is not None and views > NO_SIGNAL_VIEW_THRESHOLD


def _recent_actuals(records: list[dict]) -> list[int]:
    return [r["measurement"]["actual_views"] for r in records[-RECENT_WINDOW:]]


def _topic_multiplier(records: list[dict], subject: str) -> tuple[float, int]:
    """How this topic historically performs vs the channel average, shrunk
    toward 1.0 by how little data supports it."""
    actuals = _recent_actuals(records)
    global_avg = statistics.fmean(actuals)
    if global_avg <= 0 or not subject:
        return 1.0, 0

    same = [
        r["measurement"]["actual_views"] for r in records
        if r.get("topic_subject", "").strip().lower() == subject.strip().lower()
    ]
    if not same:
        return 1.0, 0

    raw = statistics.fmean(same) / global_avg
    n = len(same)
    shrunk = 1 + (raw - 1) * (n / (n + SHRINKAGE_K))
    return shrunk, n


def predict(topic_subject: str = "", now=None) -> dict:
    """Returns the prediction plus the basis for it, so a later accuracy
    review can tell which model produced which number."""
    all_measured = store.measured_records()
    # Suppressed videos are dropped before ANY averaging - they would drag the
    # baseline toward zero just as hard as they'd distort a topic multiplier.
    records = [r for r in all_measured if has_signal(r)]
    n_no_signal = len(all_measured) - len(records)
    active, days = self_improve_active(now)
    held_until = _hold_until(now.date() if hasattr(now, "date") else None)

    if len(records) < MIN_SAMPLES_FOR_BASELINE:
        return {
            "predicted_views": COLD_START_PREDICTION,
            "model_version": "cold-start-v1",
            "confidence": "low",
            "basis": {
                "n_samples": len(records),
                "n_excluded_no_signal": n_no_signal,
                "days_of_history": days,
                "self_improve_active": active,
                "self_improve_held_until": held_until,
                "note": "not enough measured videos with real distribution yet; "
                        "using fixed default",
            },
        }

    baseline = statistics.median(_recent_actuals(records))

    if not (active and len(records) >= MIN_SAMPLES_FOR_LEARNING):
        return {
            "predicted_views": max(1, round(baseline)),
            "model_version": "baseline-v1",
            "confidence": "medium",
            "basis": {
                "n_samples": len(records),
                "n_excluded_no_signal": n_no_signal,
                "median_recent": baseline,
                "days_of_history": days,
                "self_improve_active": active,
                "self_improve_held_until": held_until,
                "note": (f"median of recent videos; learning held until "
                         f"{held_until} by SELF_IMPROVE_AFTER" if held_until else
                         "median of recent videos; learning gated until day "
                         f"{SELF_IMPROVE_MIN_DAYS}"),
            },
        }

    multiplier, n_topic = _topic_multiplier(records, topic_subject)

    # Blend in retention shape when it exists. With no retention data this
    # branch is skipped entirely and the result is bit-for-bit what the
    # view-only model produced, so enabling the Analytics API later changes
    # predictions but never silently rewrites past behaviour.
    ret_multiplier, n_retention = _retention_multiplier(records, topic_subject)
    if ret_multiplier is not None:
        blended = (multiplier * (1 - RETENTION_WEIGHT)
                   + ret_multiplier * RETENTION_WEIGHT)
        model_version = "learned-v2-retention"
    else:
        blended, model_version = multiplier, "learned-v1"

    # Early drop-off caps confidence regardless of how the raw view multiplier
    # looks - a topic whose videos lose viewers in the first quarter isn't one
    # we want to trust just because a couple of them still racked up views.
    early_drop_off = ret_multiplier is not None and ret_multiplier < RETENTION_CONFIDENCE_FLOOR
    confidence = "low" if (not n_topic or early_drop_off) else "medium"

    return {
        "predicted_views": max(1, round(baseline * blended)),
        "model_version": model_version,
        "confidence": confidence,
        "basis": {
            "n_samples": len(records),
            "n_excluded_no_signal": n_no_signal,
            "median_recent": baseline,
            "topic_multiplier": round(multiplier, 3),
            "topic_samples": n_topic,
            "retention_multiplier": (round(ret_multiplier, 3)
                                      if ret_multiplier is not None else None),
            "retention_samples": n_retention,
            "early_drop_off": early_drop_off,
            "days_of_history": days,
            "self_improve_active": True,
        },
    }


def accuracy_summary(limit: int = 20) -> dict:
    """Rolling predicted-vs-actual error, for notifications and the dashboard."""
    records = [r for r in store.measured_records() if r.get("prediction")][-limit:]
    errors = []
    for r in records:
        predicted = r["prediction"].get("predicted_views")
        actual = r["measurement"].get("actual_views")
        if predicted and actual is not None:
            errors.append(abs(predicted - actual) / max(predicted, 1) * 100)
    if not errors:
        return {"n": 0, "mean_abs_pct_error": None}
    return {"n": len(errors), "mean_abs_pct_error": round(statistics.fmean(errors), 1)}


def top_performers(limit: int = 5) -> list[dict]:
    """Best-performing past topics — fed back into the script prompt once the
    self-improving stage is active."""
    records = [r for r in store.measured_records()
               if r.get("topic_subject") and has_signal(r)]
    ranked = sorted(records, key=lambda r: r["measurement"]["actual_views"], reverse=True)
    return [
        {"subject": r["topic_subject"], "title": r["title"],
         "views": r["measurement"]["actual_views"]}
        for r in ranked[:limit]
    ]
