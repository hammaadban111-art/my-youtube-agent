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
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    prior = data.get("prior_views")
    if (isinstance(prior, bool) or not isinstance(prior, (int, float))
            or not math.isfinite(prior) or prior <= 0):
        return None
    return data


def prior_views(path: str = None) -> int | None:
    data = load(path)
    return max(1, int(data["prior_views"])) if data else None


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


# ---------------------------------------------------------------------------
# Content-type signal, and why it is kept away from the view prediction.
#
# The scan is selected by content type and format only - eight topic-type
# queries, Shorts under 90 seconds, no channel-size filter. That reaches 410
# channels spanning 0 to 60M subscribers, including 89 under 1,000 subs. (An
# earlier pass concluded no small channels were reachable; that was an artifact
# of ordering results by view count, not a fact about YouTube.)
#
# Raw per-category view numbers off that scan are NOT usable, and this was
# measured rather than assumed. Median subscriber count per category ranged
# from 1,860 (disappearance) to 988,500 (science_nature), so a category's raw
# median mostly records which channels its query happened to surface. Feeding
# those in as per-category priors made predictions WORSE in backtest: mean
# log10 error 0.738 against 0.604 for a single global anchor.
#
# What does survive is the residual after dividing out channel size: how a
# category performs against same-size peers. Those separate cleanly, four of
# five outside two standard errors. But the spread BETWEEN categories (0.93
# log10) is about the same as the spread WITHIN one (0.97 log10) - so they can
# rank topic types and cannot forecast an individual video. They are therefore
# exposed for topic SELECTION only, and never multiply the predicted views.
# ---------------------------------------------------------------------------

# Minimum residual sample before a category is allowed to influence anything.
MIN_CATEGORY_SAMPLES = 25


# Extra keywords for OUR OWN catalogue, merged on top of the scanned niche's
# category_keywords in categorize().
#
# WHY THIS LIVES IN CODE AND NOT IN benchmark.json. The keywords in the data
# file are derived from the scanned niche and are regenerated wholesale by
# scripts/refresh_benchmark.py; anything added there is destroyed by the next
# refresh. These are additive and survive it.
#
# WHY IT WAS NEEDED. Measured 2026-09-09 against all 114 published videos: 67 of
# them (59%) matched no category at all, and the misses were not random. They
# were almost entirely natural phenomena and physical history — Blood Falls,
# Lake Nyos, the Crooked Forest, Catatumbo lightning, the Iron Pillar of Delhi,
# the Boston Molasses Flood — which is to say the two categories the niche scan
# rates HIGHEST (science_nature 3.94x, historical 2.68x). The channel looked
# like it had no opinion about its own best-paying content because the
# classifier could not see it.
#
# Deliberately conservative: every term here is a concrete noun from the
# subject matter, not a mood word. "strange" and "mysterious" appear in almost
# every title this channel has ever published and would classify everything as
# `unexplained`, which is the category whose signal is already too weak to use.
CATEGORY_KEYWORDS_SUPPLEMENT = {
    "science_nature": [
        "lake", "glacier", "volcano", "crater", "geyser", "waterfall", "river",
        "forest", "desert", "cave", "island", "mountain", "reef", "tsunami",
        "earthquake", "eruption", "lightning", "storm", "aurora", "meteor",
        "comet", "asteroid", "submarine", "submersible", "deep sea", "trench",
        "whale", "insect", "fungus", "bacteria", "dna", "genetic", "chemical",
        "radiation", "radium", "mercury", "acid", "crystal", "mineral",
        "fossil", "dinosaur", "glacial", "permafrost", "sinkhole", "geology",
        "gravity", "magnetic", "solar", "eclipse", "atmospheric", "weather",
    ],
    "historical": [
        "roman", "greek", "egypt", "viking", "colony", "expedition", "voyage",
        "shipwreck", "galleon", "castle", "monastery", "temple", "tomb",
        "manuscript", "scroll", "codex", "artifact", "excavation",
        "pillar", "monument", "obelisk", "statue", "aqueduct", "catacomb",
        "archaeolog", "ruins", "dynasty", "kingdom", "revolution", "treaty",
        "plague", "famine", "flood of", "explosion", "disaster", "factory",
        "railway", "lighthouse", "fort", "battle", "siege", "soldier",
        "1600", "1700", "bronze age", "iron age", "stone age", "prehistoric",
    ],
}


def _merged_keywords(data: dict) -> dict:
    """The scanned niche's keywords, extended by our own supplement.

    The data file's list is checked FIRST for every category, so a term the
    niche scan itself derived still wins the classification. The supplement only
    ever catches what would otherwise have fallen through to None."""
    scanned = data.get("category_keywords") or {}
    if not isinstance(scanned, dict):
        scanned = {}
    merged = {}
    for category, keywords in scanned.items():
        if isinstance(category, str) and isinstance(keywords, list):
            merged[category] = [k for k in keywords if isinstance(k, str)]
    for category, extra in CATEGORY_KEYWORDS_SUPPLEMENT.items():
        merged.setdefault(category, [])
        merged[category] = merged[category] + list(extra)
    return merged


def categorize(text: str) -> str | None:
    """Which content type a topic belongs to, or None if it matches nothing.

    Keyword-matched against categories inferred from the scanned titles rather
    than from our own topic list - the point is to describe the niche as it is,
    not to project our assumptions onto it - plus
    CATEGORY_KEYWORDS_SUPPLEMENT, which exists because the scanned set alone
    could not see 59% of what this channel actually publishes.
    """
    data = load()
    if not data:
        return None
    lowered = (text or "").lower()
    keywords_by_category = _merged_keywords(data)
    if not keywords_by_category:
        return None
    # Two passes so the ORDER of the scanned categories still decides ties the
    # way it always did, rather than the supplement's insertion order.
    for source in (data.get("category_keywords") or {}, keywords_by_category):
        if not isinstance(source, dict):
            continue
        for category, keywords in source.items():
            if not isinstance(category, str) or not isinstance(keywords, list):
                continue
            if any(isinstance(k, str) and k.lower() in lowered for k in keywords):
                return category
    return None


def categorization_coverage(texts: list[str]) -> dict:
    """How much of a body of titles the classifier can actually see.

    Reported rather than assumed: a category ranking built on a classifier that
    misses most of the catalogue is a ranking of the minority it happens to
    match. Used by the editorial brief to decide whether to show the ranking at
    all."""
    total = len(texts)
    if not total:
        return {"total": 0, "classified": 0, "coverage": 0.0, "by_category": {}}
    by_category = {}
    classified = 0
    for text in texts:
        category = categorize(text)
        if category:
            classified += 1
            by_category[category] = by_category.get(category, 0) + 1
    return {
        "total": total,
        "classified": classified,
        "coverage": round(classified / total, 3),
        "by_category": dict(sorted(by_category.items(),
                                   key=lambda kv: -kv[1])),
    }


def category_signal(category: str | None) -> dict | None:
    """Size-controlled performance of a content type, or None when the sample
    cannot support a claim. Returning None is a real answer here: a category
    with too few videos, or one whose effect is inside its own error bars, has
    nothing to say and must not be dressed up with a number anyway."""
    data = load()
    if not data or not category:
        return None
    categories = data.get("categories") or {}
    if not isinstance(categories, dict):
        return None
    entry = categories.get(category)
    if not isinstance(entry, dict) or not entry.get("usable"):
        return None
    return dict(entry, category=category)


def category_ranking() -> list[dict]:
    """Every content type with a usable signal, strongest first. Fed to topic
    selection as one input among several - it never overrides what this
    channel's own results say."""
    data = load()
    if not data:
        return []
    categories = data.get("categories") or {}
    if not isinstance(categories, dict):
        return []
    ranked = []
    for key, value in categories.items():
        multiplier = value.get("multiplier") if isinstance(value, dict) else None
        if (not isinstance(key, str) or not isinstance(value, dict)
                or not value.get("usable") or isinstance(multiplier, bool)
                or not isinstance(multiplier, (int, float))
                or not math.isfinite(multiplier) or multiplier <= 0):
            continue
        ranked.append(dict(value, category=key))
    return sorted(ranked, key=lambda c: c["multiplier"], reverse=True)


# ---------------------------------------------------------------------------
# Honest uncertainty on a prediction.
#
# A bare "predicted_views: 1200" claims a precision this does not have. Real
# backtested error on this channel, on the videos that got distribution, was a
# mean of 0.515 log10 - a factor of 3.3 - with a worst case of 2.298, a factor
# of 199. Reporting a point estimate from a model with that spread invites the
# number to be read as a forecast rather than an order-of-magnitude guess.
#
# The band narrows as our own sample grows. That part is by design rather than
# measured: per-sample-count error buckets here hold two or three videos each,
# which cannot establish a trend. What justifies narrowing is structural - the
# prior carries a known unit mismatch (lifetime views on other channels' videos
# against our views at 5h) and is weighted out as our own data arrives.
# ---------------------------------------------------------------------------

# Band half-width in log10, at no own data and at plenty of it. The cold figure
# sits between the measured mean error and the measured worst case; the rich
# figure is roughly the measured mean.
COLD_BAND_LOG10 = 1.0    # x10
RICH_BAND_LOG10 = 0.5    # x3.2


def prediction_range(point: float, n_samples: int, k: int = BLEND_K) -> dict:
    """A low-high band around a point estimate, plus how to say it out loud."""
    weight = own_data_weight(n_samples, k)
    band = COLD_BAND_LOG10 - (COLD_BAND_LOG10 - RICH_BAND_LOG10) * weight
    factor = 10 ** band
    low = max(1, int(round(point / factor)))
    high = int(round(point * factor))
    return {
        "point": int(round(point)),
        "low": low,
        "high": high,
        "band_factor": round(factor, 1),
        "text": f"{low:,}-{high:,}",
        "basis": (f"about {factor:.0f}x either way, from backtested error "
                  f"({n_samples} of our own videos so far)"),
    }
