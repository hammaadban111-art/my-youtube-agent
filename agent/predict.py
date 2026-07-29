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
    return {
        "predicted_views": max(1, round(baseline * multiplier)),
        "model_version": "learned-v1",
        "confidence": "medium" if n_topic else "low",
        "basis": {
            "n_samples": len(records),
            "median_recent": baseline,
            "topic_multiplier": round(multiplier, 3),
            "topic_samples": n_topic,
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
