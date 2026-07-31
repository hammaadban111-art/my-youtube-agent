"""
An outside anchor for view predictions, for while our own history is thin.

WHY THIS IS A FLOOR AND NOT AN AVERAGE
--------------------------------------
The obvious version of this idea - "find comparable channels and average them"
- does not survive contact with the data. Measured 2026-08-01 against 1,040
recent Shorts from 24 niche channels:

  * The smallest channel reachable through search had 38,700 subscribers. Ours
    had 15. There is no comparable-size channel to be found; YouTube search
    cannot filter by subscriber count, and small channels do not surface.
  * Views per subscriber ranged from 0.002 to 1.024 across those channels - a
    417x spread. So channel size does not predict views per Short well enough
    to scale a big channel's numbers down to ours.
  * Our own real median (1,022 views) sits at the 1st percentile of that pooled
    distribution, and the 2.8th percentile of the sub-1M-subscriber subset.

So the honest reading is not "we are like these channels but smaller". It is
"a channel with no audience yet lands at the bottom of what this niche
produces". The prior is therefore a low percentile of the smallest channels we
can see, not a central tendency - p5, which is grounded rather than fitted; the
exact percentile is only weakly supported by our own five outcomes.

KNOWN MISMATCH, STATED PLAINLY
------------------------------
The external number is LIFETIME views on someone else's Short. What we predict
is OUR views at the 5h mark. Those are not the same quantity, and nothing here
converts between them - the API exposes no view-count-over-time for videos we
do not own (see docstring in youtube_stats.py for what is and is not readable).
They are numerically close at our current scale by coincidence, not by
construction, and that coincidence will expire as the channel grows.

That is survivable because of how the prior is used: it is blended out in
proportion to how much of our own data exists, so the mismatch shrinks as it
starts to matter. It would NOT be survivable as a fixed anchor, which is why
this file exposes a weight rather than just a number.
"""
import json
import math
import os

DATA_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "benchmark.json")

# Shrinkage strength for the own-data-vs-prior blend: how many of our own
# measured videos it takes before our numbers carry as much weight as the
# outside anchor. Matches predict.SHRINKAGE_K, which does the same job for
# topic multipliers, so the codebase has one notion of "how much data is
# enough" rather than two.
#
# Backtesting on this channel's real records showed the choice barely matters:
# mean log10 error across the videos that got real distribution was 0.389 at
# K=2 and 0.440 at K=12, against 1.259 for the current system. The win comes
# from having any grounded anchor at all, not from tuning this number - which
# is good, because five outcomes could not support tuning it.
BLEND_K = 5


def load(path: str = None) -> dict | None:
    """The benchmark snapshot, or None if it is missing or unreadable.

    Returns None rather than raising: a missing or corrupt benchmark file must
    degrade the prediction to own-data-only, never take down the upload run.
    """
    try:
        with open(path or DATA_PATH) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data.get("prior_views"), (int, float)) else None


def prior_views(path: str = None) -> int | None:
    data = load(path)
    return int(data["prior_views"]) if data else None


def own_data_weight(n_samples: int, k: int = BLEND_K) -> float:
    """How much of the prediction should come from OUR numbers, 0.0 to 1.0.

    n/(n+k): 0 of our own videos leans entirely on the outside anchor, and the
    anchor fades as our own evidence accumulates. Deliberately a function of
    SAMPLE COUNT and not of elapsed time - a fortnight of uploads that all got
    throttled teaches us nothing, and a date-based switch would hand the model
    to that noise on schedule regardless.
    """
    n = max(0, int(n_samples))
    return n / (n + k) if (n + k) > 0 else 0.0


def blend(own_estimate: float, n_samples: int, path: str = None,
          k: int = BLEND_K) -> tuple[float, dict]:
    """Combine our own estimate with the outside anchor. Returns (value, basis).

    Geometric, not arithmetic. View counts on Shorts span orders of magnitude,
    so the meaningful distance between 1,000 and 10,000 is the same as between
    10,000 and 100,000. An arithmetic mean of numbers that far apart is
    dominated by the larger one no matter what weight it was given, which would
    make the weight decorative.
    """
    prior = prior_views(path)
    weight = own_data_weight(n_samples, k)
    basis = {
        "prior_views": prior,
        "own_data_weight": round(weight, 3),
        "n_samples": int(n_samples),
        "blend_k": k,
    }
    if prior is None or prior <= 0:
        basis["note"] = "no benchmark available - own data only"
        return own_estimate, basis
    if own_estimate <= 0:
        return prior, basis
    value = math.exp(weight * math.log(own_estimate) + (1 - weight) * math.log(prior))
    return value, basis
