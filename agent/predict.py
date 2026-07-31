"""
View-count prediction, plus the day-5 gate that switches the channel from
"just logging" to "learning from itself".

Three stages, deliberately in plain Python rather than a modelling library:
the sample sizes here are tiny, every prediction has to be explainable
after the fact, and adding scikit-learn to a workflow that runs 720+ times
a month is a lot of CI time for arithmetic we can do ourselves.
"""
import statistics
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


def self_improve_active(now=None) -> tuple[bool, int]:
    """(active, days_of_history) — a real date check against the first upload."""
    days = store.days_of_history(now)
    return days >= SELF_IMPROVE_MIN_DAYS, days


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
    records = store.measured_records()
    active, days = self_improve_active(now)

    if len(records) < MIN_SAMPLES_FOR_BASELINE:
        return {
            "predicted_views": COLD_START_PREDICTION,
            "model_version": "cold-start-v1",
            "confidence": "low",
            "basis": {
                "n_samples": len(records),
                "days_of_history": days,
                "self_improve_active": active,
                "note": "not enough measured videos yet; using fixed default",
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
                "median_recent": baseline,
                "days_of_history": days,
                "self_improve_active": active,
                "note": "median of recent videos; learning gated until day "
                        f"{SELF_IMPROVE_MIN_DAYS}",
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
    records = [r for r in store.measured_records() if r.get("topic_subject")]
    ranked = sorted(records, key=lambda r: r["measurement"]["actual_views"], reverse=True)
    return [
        {"subject": r["topic_subject"], "title": r["title"],
         "views": r["measurement"]["actual_views"]}
        for r in ranked[:limit]
    ]
