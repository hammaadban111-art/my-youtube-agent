"""Content-type segmentation, and the honest record of what it did NOT do.

Segmentation sounds smarter than a single number. Measured against this
channel's real outcomes it was WORSE as a view predictor, and the reason is
recorded here so nobody re-derives the idea and re-ships it: a category's raw
view numbers mostly record the size of whichever channels its query surfaced.

What survived is the residual after dividing channel size out. That separates
cleanly, but its between-category spread is about the same as its within-
category spread - it ranks kinds of story, it does not forecast a video. So it
is wired into topic selection and kept out of the arithmetic.
"""
import math
import statistics

import pytest

from agent import benchmark, predict, store


# --- selection is by content, not size --------------------------------------

def test_selection_does_not_filter_by_channel_size():
    data = benchmark.load()
    assert data["selection"]["channel_size_filter"] == "none"
    assert data["selection"]["by"] == "content type and format"
    assert len(data["selection"]["queries"]) >= 5, (
        "one query would let a single phrasing decide what the niche looks like")
    # The wide net has to actually reach small channels, or it is the old
    # big-channel-only sample wearing a new label.
    assert data["sample"]["channels_under_1k_subs"] >= 20


def test_categories_were_inferred_from_the_scan_not_from_our_topics():
    """Categories come from the scanned titles' own vocabulary. Our own topic
    list must not be the thing that defines them."""
    keywords = benchmark.load()["category_keywords"]
    assert len(keywords) >= 4
    our_subjects = {r["topic_subject"].lower()
                    for r in store.measured_records() if r.get("topic_subject")}
    for words in keywords.values():
        for w in words:
            assert not any(w == s for s in our_subjects), (
                f"category keyword {w!r} is one of our own topic names")


# --- the honest part: sparse categories say nothing -------------------------

def test_a_category_without_enough_data_returns_nothing():
    """No number is better than a fabricated one."""
    assert benchmark.category_signal("nonexistent_category") is None
    assert benchmark.category_signal(None) is None

    data = benchmark.load()
    for name, entry in data["categories"].items():
        if entry["n"] < benchmark.MIN_CATEGORY_SAMPLES:
            assert benchmark.category_signal(name) is None, (
                f"{name} has only {entry['n']} samples and must not be usable")


def test_categories_inside_their_own_error_bars_are_not_usable():
    """A category whose effect is smaller than its uncertainty is noise."""
    for name, entry in benchmark.load()["categories"].items():
        if abs(entry["residual_log10"]) <= 2 * entry["standard_error"]:
            assert not entry["usable"], f"{name} is within noise but marked usable"
            assert benchmark.category_signal(name) is None


# data/benchmark.json is no longer a frozen snapshot. Since 2026-09-12 the
# weekly niche scan recomputes its category block (scripts/refresh_category_signal.py),
# so tests that read the LIVE file may assert the invariants the code guarantees
# but not what the data happens to say that week.
#
# That distinction was learned the hard way. The first two tests below used to
# assert that at least one category separated from the noise — true of the
# 2026-08-01 snapshot, where science_nature sat at 3.94x. The 2026-09-14 scan
# puts every category inside its own error bars, and science_nature at 0.56x:
# the sign flipped. An empty ranking is therefore the honest current answer, and
# the suite went red on a correct result. (It went red silently, too: the data
# commit that changed the file is one tests.yml deliberately skips.)
_SEPARATING_FIXTURE = {
    "prior_views": 960,
    "category_keywords": {"science_nature": ["ocean"], "disappearance": ["vanish"],
                          "unexplained": ["strange"]},
    "categories": {
        "science_nature": {"n": 38, "residual_log10": 0.596, "multiplier": 3.94,
                           "standard_error": 0.121, "usable": True,
                           "within_category_sd_log10": 0.745},
        "disappearance": {"n": 74, "residual_log10": -0.237, "multiplier": 0.58,
                          "standard_error": 0.116, "usable": True,
                          "within_category_sd_log10": 0.997},
        "unexplained": {"n": 118, "residual_log10": 0.029, "multiplier": 1.07,
                        "standard_error": 0.092, "usable": False,
                        "within_category_sd_log10": 0.9},
    },
}


def test_live_ranking_only_contains_usable_categories():
    """The invariant, on whatever the latest scan produced — including nothing."""
    ranked = benchmark.category_ranking()
    for c in ranked:
        assert c["usable"]
        assert c["n"] >= benchmark.MIN_CATEGORY_SAMPLES
    multipliers = [c["multiplier"] for c in ranked]
    assert multipliers == sorted(multipliers, reverse=True)


def test_ranking_orders_and_filters_a_separating_benchmark(monkeypatch):
    """The populated path, pinned against a fixture so it cannot pass vacuously
    on a week where the live data has nothing usable."""
    monkeypatch.setattr(benchmark, "load", lambda path=None: _SEPARATING_FIXTURE)
    ranked = benchmark.category_ranking()
    assert [c["category"] for c in ranked] == ["science_nature", "disappearance"]
    assert all(c["usable"] for c in ranked)


def test_an_all_noise_benchmark_ranks_nothing(monkeypatch):
    """The 2026-09-14 shape. Returning [] is the correct, honest answer — the
    editorial brief then shows no tie-breaker rather than an invented one."""
    noise = dict(_SEPARATING_FIXTURE)
    noise["categories"] = {
        name: dict(entry, usable=False)
        for name, entry in _SEPARATING_FIXTURE["categories"].items()}
    monkeypatch.setattr(benchmark, "load", lambda path=None: noise)
    assert benchmark.category_ranking() == []


# --- segmentation must stay out of the prediction arithmetic ----------------

def test_category_signal_never_reaches_the_predicted_number():
    """The measured result: per-category priors made predictions worse (0.738
    against 0.604 mean log10 error). The prediction path must not consult
    them."""
    a = predict.predict("Sodder children disappearance")   # disappearance, 0.58x
    b = predict.predict("Tunguska event")                  # science_nature, 3.94x
    assert benchmark.categorize("Sodder children disappearance") != \
           benchmark.categorize("Tunguska event"), "fixture needs two different categories"
    assert a["predicted_views"] == b["predicted_views"], (
        "predictions differ by content category - the category multiplier has "
        "leaked into the view arithmetic, which backtested worse than not doing it")


def test_between_category_spread_is_no_bigger_than_within():
    """The reason segmentation cannot forecast a single video. If this ever
    stops being true the signal has become sharp enough to reconsider.

    Needs at least two usable categories to be a question at all. On a week
    where the scan separates nothing — 2026-09-14 is one — there is no spread to
    compare, and that is itself the strongest possible version of this test's
    point, so it skips rather than failing on max() of nothing."""
    cats = [c for c in benchmark.load()["categories"].values() if c["usable"]]
    if len(cats) < 2:
        pytest.skip(f"only {len(cats)} usable categories in the current scan; "
                    "no between-category spread exists to measure")
    residuals = [c["residual_log10"] for c in cats]
    between = max(residuals) - min(residuals)
    within = statistics.fmean(c["within_category_sd_log10"] for c in cats)
    assert between < within * 2, (
        f"between-category spread {between:.2f} vs within-category {within:.2f}")


# --- the display band -------------------------------------------------------

def test_every_prediction_carries_a_range():
    result = predict.predict("anything at all")
    r = result["predicted_range"]
    assert r["low"] < result["predicted_views"] < r["high"]
    assert r["text"] == f"{r['low']:,}-{r['high']:,}"
    assert "either way" in r["basis"]


def test_the_band_narrows_as_our_own_data_grows():
    thin = benchmark.prediction_range(1000, n_samples=0)
    some = benchmark.prediction_range(1000, n_samples=8)
    lots = benchmark.prediction_range(1000, n_samples=200)
    assert thin["band_factor"] > some["band_factor"] > lots["band_factor"]
    # Never collapses to false precision, however much data arrives.
    assert lots["band_factor"] >= 10 ** benchmark.RICH_BAND_LOG10 - 0.1
    assert lots["high"] / lots["low"] > 5


def test_the_band_reflects_real_backtested_error():
    """The cold band must be at least as wide as the error actually measured on
    real videos, or it is understating what is known to happen."""
    records = sorted(store.measured_records(), key=lambda r: r["uploaded_at"])
    signal = [r for r in records if predict.has_signal(r)]
    if len(signal) < 3:
        pytest.skip("not enough measured records")

    errors = []
    for i, record in enumerate(signal):
        past = [r["measurement"]["actual_views"] for r in signal[:i]]
        actual = record["measurement"]["actual_views"]
        if not past:
            predicted = benchmark.prior_views()
        else:
            predicted, _ = benchmark.blend(statistics.median(past), len(past))
        errors.append(abs(math.log10(max(predicted, 1) / max(actual, 1))))

    mean_error = statistics.fmean(errors)
    assert benchmark.COLD_BAND_LOG10 >= mean_error, (
        f"cold band {benchmark.COLD_BAND_LOG10} is narrower than the measured "
        f"mean error {mean_error:.3f} - the range would be claiming more "
        "precision than the backtest supports")
