"""
View-count prediction, plus the day-5 gate that switches the channel from
"just logging" to "learning from itself".

Three stages, deliberately in plain Python rather than a modelling library:
the sample sizes here are tiny, every prediction has to be explainable
after the fact, and adding scikit-learn to a workflow that runs 720+ times
a month is a lot of CI time for arithmetic we can do ourselves.
"""
import json
import os
import statistics
from datetime import date

from . import benchmark, notify, store

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

# One-time human approval gate, separate from the two brakes above. The
# day-5/SELF_IMPROVE_AFTER gates are about whether there's enough DATA;
# this one is about consent - self-improving mode changes what the channel
# posts based on its own past performance, and that first flip should never
# happen silently. The repo Variable is set by hand in GitHub's UI after an
# email arrives asking for it (see _request_approval_once below); once set
# it stays set, so this only ever blocks the FIRST activation.
SELF_IMPROVE_APPROVED_ENV = "SELF_IMPROVE_APPROVED"
# Committed (survives the clean-checkout runner) so the approval email goes
# out exactly once, not on every run between when the gate opens and when
# the human gets around to approving it.
GATE_STATE_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "self_improve_gate.json")

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


# ---------------------------------------------------------------------------
# Which reading a given question should be asked of.
#
# Every record carries two: `measurement` is the FIRST reading (~5h after
# upload), frozen forever; `latest_measurement` is the most recent one. They
# answer different questions and mixing them up is a real modelling error in
# both directions:
#
#   "Was the prediction any good?"  -> the FROZEN reading. Predictions are made
#       for a video's 5h mark, so scoring them against a reading taken at an
#       arbitrary later age would just measure how long ago the video went up.
#       Every video has to be judged at the same age. See accuracy_summary().
#
#   "Was this TOPIC any good?"      -> the LATEST reading. A video's 5h number
#       is a snapshot taken before most of its growth has happened. A slow
#       starter that compounds for three days is a topic to make MORE of, but
#       its 5h reading says the opposite, and steering by that number teaches
#       the model to avoid exactly the subjects that work.
#
# The channel-wide baseline that predictions are built on stays on the frozen
# reading too - it sets the SCALE of the prediction, which has to remain "views
# at 5h" for the accuracy check above to mean anything. Only the topic
# multipliers, which are dimensionless ratios, move to the latest reading.
# ---------------------------------------------------------------------------

def _latest(record: dict) -> dict:
    """The most recent reading for a video. Falls back to the frozen first
    reading for records written before latest_measurement existed, so old
    records still contribute rather than silently dropping out."""
    return record.get("latest_measurement") or record.get("measurement") or {}


def _latest_views(record: dict) -> int | None:
    return _latest(record).get("actual_views")


def _retention_quality(record: dict) -> float | None:
    """A single 0-1 score for how well one video held its audience, or None
    when retention was never captured. Uses retention at the end of the hook
    window - the segment where ~half of all swipe-aways happen.

    Reads the LATEST reading: YouTube Analytics usually has no retention rows
    at all 5h in ("needs ~24-48h"), so the frozen reading is not just stale
    here, it is typically empty."""
    retention = _latest(record).get("retention") or {}
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
    average, across every video that has retention data.

    Reads the LATEST reading, matching _retention_quality(). If this stayed on
    the frozen reading the dashboard would report a different retention picture
    than the one the model is actually steering by — and would usually report
    nothing at all, since retention rarely exists yet at 5h."""
    records = store.measured_records()[-limit:]
    drops = [
        (_latest(r)["retention"].get("biggest_drop_at"),
         _latest(r)["retention"].get("biggest_drop_size"))
        for r in records
        if _latest(r).get("retention", {}).get("available")
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


def _load_gate_state() -> dict:
    if not os.path.exists(GATE_STATE_PATH):
        return {}
    try:
        with open(GATE_STATE_PATH) as f:
            state = json.load(f)
    except (OSError, ValueError, TypeError) as exc:
        # A malformed approval ledger must not crash an upload run or silently
        # make self-improving mode look approved.  Hold it closed and leave a
        # diagnostic the dashboard can surface.
        return {"invalid": f"{type(exc).__name__}: {exc}"}
    if not isinstance(state, dict):
        return {"invalid": "gate state is not a JSON object"}
    return state


def _save_gate_state(state: dict) -> None:
    os.makedirs(os.path.dirname(GATE_STATE_PATH), exist_ok=True)
    with open(GATE_STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)


def _is_approved() -> bool:
    return os.getenv(SELF_IMPROVE_APPROVED_ENV, "").strip().lower() == "true"


def _request_approval_once(days: int) -> None:
    """Sends the "self-improving mode wants to turn on" email the first time
    the day-5 gate opens, and never again. There is deliberately no reply-
    parsing here — approval is a repo Variable the human sets by hand, not
    an email reply, because nothing in this pipeline reads incoming mail."""
    state = _load_gate_state()
    if state.get("invalid"):
        print(f"[predict] self-improve gate state is invalid; holding closed: "
              f"{state['invalid']}")
        return
    if state.get("email_sent_at"):
        return
    sent = notify.send_email(
        subject="Your YouTube channel wants to turn on self-improving mode",
        text=(
            f"Your channel has {days} days of upload history now — enough for "
            f"self-improving mode to start steering future videos toward "
            f"whatever topics have performed best so far.\n\n"
            f"It will NOT turn on by itself. To approve it:\n"
            f"1. Go to github.com/hammaadban111-art/my-youtube-agent/settings/variables/actions\n"
            f"2. Add a repository variable named SELF_IMPROVE_APPROVED\n"
            f"3. Set its value to: true\n\n"
            f"Until you do that, the channel keeps running exactly as it does "
            f"today. This is a one-time approval, not a recurring setting — "
            f"once SELF_IMPROVE_APPROVED is set to true, self-improving mode "
            f"runs normally from then on and you won't get this email again."
        ),
    )
    if sent:
        state["email_sent_at"] = store.iso(store._utcnow())
        _save_gate_state(state)


def self_improve_active(now=None) -> tuple[bool, int]:
    """(active, days_of_history).

    Active requires BOTH the existing data gate (day-5 history, and not
    held by SELF_IMPROVE_AFTER) AND a one-time human approval. The first
    time the data gate would open, an email goes out asking for that
    approval instead of switching on silently — see _request_approval_once.
    Once SELF_IMPROVE_APPROVED is set, every later check just falls through
    to the ordinary data gate with no further emails."""
    days = store.days_of_history(now)
    today = now.date() if hasattr(now, "date") else None
    if _hold_until(today):
        return False, days
    if days < SELF_IMPROVE_MIN_DAYS:
        return False, days
    if _load_gate_state().get("invalid"):
        return False, days
    if _is_approved():
        return True, days
    _request_approval_once(days)
    return False, days


def self_improve_status(now=None) -> dict:
    """Richer status for the dashboard — distinguishes "not eligible yet"
    from "eligible and waiting on your email approval", which the plain
    (active, days) pair from self_improve_active() can't tell apart."""
    active, days = self_improve_active(now)
    today = now.date() if hasattr(now, "date") else None
    held = _hold_until(today)
    natural = days >= SELF_IMPROVE_MIN_DAYS and not held
    state = _load_gate_state()
    return {
        "active": active,
        "days_of_history": days,
        "self_improve_after_days": SELF_IMPROVE_MIN_DAYS,
        "held_until": held,
        "awaiting_approval": bool(natural and not active),
        "approval_email_sent_at": state.get("email_sent_at"),
        "gate_state_error": state.get("invalid"),
    }


def has_signal(record: dict) -> bool:
    """Whether this video's numbers mean anything about its topic.

    A video suppressed by a platform-level distribution throttle carries no
    information about whether its subject was a good choice - it was never
    shown to enough people to find out. Treating it as a failed topic is
    strictly worse than ignoring it, because the model would then steer away
    from subjects for a reason that has nothing to do with them.

    Judged on the LATEST reading. At 5h a perfectly healthy video can still be
    in single digits, and calling that "throttled" would throw away a real
    video's evidence; a genuinely suppressed one is still near zero days later.
    """
    views = _latest_views(record)
    return views is not None and views > NO_SIGNAL_VIEW_THRESHOLD


def _recent_actuals(records: list[dict]) -> list[int]:
    """FROZEN first readings — this sets the scale of the prediction, which
    has to stay "views at 5h" so accuracy_summary() can score against it."""
    return [r["measurement"]["actual_views"] for r in records[-RECENT_WINDOW:]]


def _topic_multiplier(records: list[dict], subject: str) -> tuple[float, int]:
    """How this topic historically performs vs the channel average, shrunk
    toward 1.0 by how little data supports it.

    Both sides come from the LATEST readings, so this compares each video at
    its current size rather than at its 5h snapshot. Both sides have to move
    together: dividing a topic's latest views by a 5h channel average would
    make every multiplier read high by however much the channel grows after
    the first few hours, which is not a fact about the topic at all."""
    latest = [v for v in (_latest_views(r) for r in records[-RECENT_WINDOW:])
              if v is not None]
    if not latest:
        return 1.0, 0
    global_avg = statistics.fmean(latest)
    if global_avg <= 0 or not subject:
        return 1.0, 0

    same = [
        v for v in (
            _latest_views(r) for r in records
            if r.get("topic_subject", "").strip().lower() == subject.strip().lower()
        ) if v is not None
    ]
    if not same:
        return 1.0, 0

    raw = statistics.fmean(same) / global_avg
    n = len(same)
    shrunk = 1 + (raw - 1) * (n / (n + SHRINKAGE_K))
    return shrunk, n


def predict(topic_subject: str = "", now=None) -> dict:
    """A prediction, with an honest band around it.

    `predicted_views` is the point estimate and is what the accuracy check and
    the blend arithmetic use - unchanged. `predicted_range` is what anything
    showing this to a human should print instead. A bare figure out of a model
    whose backtested error runs from 3x to 199x reads as a forecast when it is
    an order-of-magnitude guess, and the point number on its own gives a reader
    no way to know which they are looking at.
    """
    result = _predict_point(topic_subject, now)
    n_samples = result.get("basis", {}).get("n_samples", 0)
    result["predicted_range"] = benchmark.prediction_range(
        result["predicted_views"], n_samples)
    return result


def _predict_point(topic_subject: str = "", now=None) -> dict:
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
        # With nothing of our own worth averaging, fall back to the outside
        # anchor rather than to a number someone picked. COLD_START_PREDICTION
        # is only reached now if the benchmark file is missing or unreadable.
        anchor = benchmark.prior_views()
        return {
            "predicted_views": anchor or COLD_START_PREDICTION,
            "model_version": "benchmark-v1" if anchor else "cold-start-v1",
            "confidence": "low",
            "basis": {
                "n_samples": len(records),
                "n_excluded_no_signal": n_no_signal,
                "days_of_history": days,
                "self_improve_active": active,
                "self_improve_held_until": held_until,
                "prior_views": anchor,
                "own_data_weight": 0.0,
                "note": ("no measured videos of our own yet; using the external "
                         "niche benchmark" if anchor else
                         "no measured videos and no benchmark file; using the "
                         "fixed default"),
            },
        }

    # Our own number, then pulled toward the outside anchor by however little
    # of our own data stands behind it. The pull decays as samples accumulate,
    # so this converges on pure own-data without a date-based switch.
    own_baseline = statistics.median(_recent_actuals(records))
    baseline, blend_basis = benchmark.blend(own_baseline, len(records))

    if not (active and len(records) >= MIN_SAMPLES_FOR_LEARNING):
        return {
            "predicted_views": max(1, round(baseline)),
            "model_version": "baseline-v2-benchmark",
            "confidence": "medium",
            "basis": {
                "n_samples": len(records),
                "n_excluded_no_signal": n_no_signal,
                "median_recent": own_baseline,
                "blended_baseline": round(baseline),
                **blend_basis,
                "days_of_history": days,
                "self_improve_active": active,
                "self_improve_held_until": held_until,
                "note": (f"median of recent videos blended with the niche benchmark; "
                         f"learning held until {held_until} by SELF_IMPROVE_AFTER"
                         if held_until else
                         "median of recent videos blended with the niche benchmark; "
                         f"learning gated until day {SELF_IMPROVE_MIN_DAYS}"),
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
            "median_recent": own_baseline,
            "blended_baseline": round(baseline),
            "topic_multiplier": round(multiplier, 3),
            "topic_samples": n_topic,
            "retention_multiplier": (round(ret_multiplier, 3)
                                      if ret_multiplier is not None else None),
            "retention_samples": n_retention,
            **blend_basis,
            "early_drop_off": early_drop_off,
            "days_of_history": days,
            "self_improve_active": True,
        },
    }


def accuracy_summary(limit: int = 20) -> dict:
    """Rolling predicted-vs-actual error, for notifications and the dashboard.

    Deliberately scored against the FROZEN 5h reading, never the latest one.
    Predictions are made for the 5h mark, so this is the only comparison that
    holds video age constant; swapping in latest_measurement here would make
    the channel's "accuracy" drift purely as a function of how long the videos
    in the window have been up. Do not "modernise" this to match the topic
    signal below - they are measuring different things on purpose."""
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
    self-improving stage is active.

    Ranked on LATEST views. This list is the most direct topic-steering signal
    there is: whatever lands here is what the next script is asked to be more
    like. Ranking it on 5h snapshots would hand the prompt whichever videos
    happened to start fast, not the ones that actually did well."""
    records = [r for r in store.measured_records()
               if r.get("topic_subject") and has_signal(r)
               and _latest_views(r) is not None]
    ranked = sorted(records, key=_latest_views, reverse=True)
    return [
        {"subject": r["topic_subject"], "title": r["title"],
         "views": _latest_views(r)}
        for r in ranked[:limit]
    ]
