"""The external prior, and the walk-forward backtest that justified shipping it.

The backtest at the bottom is the whole argument for this feature. It replays
the channel's real records in upload order, predicting each video from only
what was knowable before it, and compares the old system against the new one.
If the prior stops helping, that test fails and says so with numbers.
"""
import json
import math
import statistics

import pytest

from agent import benchmark, predict, store


# --- the blend, in isolation ------------------------------------------------

def test_weight_shifts_from_prior_to_own_data():
    """Thin data leans external, rich data leans on our own numbers."""
    assert benchmark.own_data_weight(0) == 0.0
    assert benchmark.own_data_weight(5, k=5) == 0.5
    assert benchmark.own_data_weight(45, k=5) == 0.9
    # Monotone, and never reaches 1.0 - our own data dominates but the anchor
    # is never formally discarded.
    weights = [benchmark.own_data_weight(n) for n in range(0, 60)]
    assert weights == sorted(weights)
    assert weights[-1] < 1.0


def test_blend_is_geometric_not_arithmetic():
    """View counts span orders of magnitude; an arithmetic mean would let the
    larger number dominate regardless of the weight it was given."""
    value, _ = benchmark.blend(own_estimate=100, n_samples=5)  # weight 0.5
    prior = benchmark.prior_views()
    assert value == pytest.approx(math.sqrt(100 * prior), rel=1e-6)
    arithmetic = (100 + prior) / 2
    assert value < arithmetic


def test_prior_dominates_with_no_data_and_fades_with_lots():
    prior = benchmark.prior_views()
    at_zero, _ = benchmark.blend(own_estimate=1, n_samples=0)
    assert at_zero == pytest.approx(prior)

    at_many, _ = benchmark.blend(own_estimate=1000, n_samples=500)
    assert abs(at_many - 1000) / 1000 < 0.05, "with 500 samples the anchor should be nearly gone"


def test_missing_benchmark_degrades_to_own_data(tmp_path):
    """A missing or corrupt snapshot must never take down a run."""
    assert benchmark.load(str(tmp_path / "nope.json")) is None
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert benchmark.load(str(bad)) is None

    value, basis = benchmark.blend(777, n_samples=2, path=str(tmp_path / "nope.json"))
    assert value == 777
    assert basis["prior_views"] is None


def test_shipped_snapshot_is_sane():
    """Guards the committed data file itself."""
    data = benchmark.load()
    assert data is not None
    assert data["sample"]["pooled_shorts"] >= 100
    p = data["percentiles_small_channels"]
    assert p["p1"] < p["p5"] < p["p10"] < p["p25"] < p["p50"]
    assert data["prior_views"] == p["p5"]


# --- the backtest -----------------------------------------------------------

def _log_error(predicted, actual):
    """How many orders of magnitude off. The right metric for a quantity that
    ranges from 0 to 5,000 on the same channel: a percentage error would be
    dominated entirely by whichever videos got throttled."""
    return abs(math.log10(max(predicted, 1) / max(actual, 1)))


def _walk_forward(records, use_prior: bool):
    """Predict each video from only the videos that preceded it."""
    errors = []
    for i, record in enumerate(records):
        past = [r["measurement"]["actual_views"]
                for r in records[:i] if predict.has_signal(r)]
        actual = record["measurement"]["actual_views"]
        n = len(past)
        if not use_prior:
            predicted = (statistics.median(past) if n >= predict.MIN_SAMPLES_FOR_BASELINE
                         else predict.COLD_START_PREDICTION)
        elif n == 0:
            predicted = benchmark.prior_views()
        else:
            predicted, _ = benchmark.blend(statistics.median(past), n)
        errors.append(_log_error(predicted, actual))
    return statistics.fmean(errors) if errors else 0.0


@pytest.fixture
def real_records():
    records = sorted(store.measured_records(), key=lambda r: r["uploaded_at"])
    if len(records) < 5:
        pytest.skip("not enough measured records to backtest")
    return records


def test_backtest_prior_beats_the_old_constant(real_records):
    """The claim being shipped, on this channel's real outcomes.

    Measured 2026-08-01 on 10 records: 1.900 old vs 1.529 blended overall, and
    1.259 vs 0.419 on the videos that actually got distribution. Asserted as an
    inequality rather than pinned to those figures, since real records keep
    arriving - the point is that the prior must not stop helping."""
    old = _walk_forward(real_records, use_prior=False)
    new = _walk_forward(real_records, use_prior=True)
    assert new < old, (
        f"external prior no longer beats the constant: {new:.3f} vs {old:.3f} "
        "mean log10 error. If this is a real regime change, re-run the "
        "backtest and update the prior rather than deleting this test.")


def test_backtest_on_videos_that_got_real_distribution(real_records):
    """Throttled videos are unpredictable by construction - has_signal() exists
    to say so. This is the same backtest with them excluded, which is where the
    improvement is actually visible."""
    signal_only = [r for r in real_records if predict.has_signal(r)]
    if len(signal_only) < 3:
        pytest.skip("not enough non-throttled records")

    old = _walk_forward(signal_only, use_prior=False)
    new = _walk_forward(signal_only, use_prior=True)
    assert new < old, f"prior stopped helping: {old:.3f} -> {new:.3f}"

    # No minimum margin is asserted, deliberately. The size of the improvement
    # is SUPPOSED to shrink: the blend weights the prior out as our own sample
    # grows, so as this channel accumulates history the two systems converge
    # and any pinned ratio would eventually fail for the exact reason the
    # design is working. First measured 1.259 -> 0.419 (67% at n=5), then
    # 1.118 -> 0.604 (46% at n=8) two videos later. The direction is the
    # guarantee; the magnitude is a moving target by construction.


def test_improvement_is_not_an_artifact_of_the_blend_constant(real_records):
    """If the win depended on a finely tuned K it would be overfitting to five
    outcomes. It should hold across a wide range of K instead."""
    old = _walk_forward(real_records, use_prior=False)
    for k in (2, 3, 5, 8, 12, 20):
        errors = []
        for i, record in enumerate(real_records):
            past = [r["measurement"]["actual_views"]
                    for r in real_records[:i] if predict.has_signal(r)]
            actual = record["measurement"]["actual_views"]
            if not past:
                predicted = benchmark.prior_views()
            else:
                predicted, _ = benchmark.blend(statistics.median(past), len(past), k=k)
            errors.append(_log_error(predicted, actual))
        assert statistics.fmean(errors) < old, f"K={k} failed to beat the old system"


# --- integration with predict() ---------------------------------------------

def test_predict_reports_the_blend_in_its_basis(monkeypatch):
    """Every prediction has to be explainable after the fact, so the weight and
    the anchor it used are recorded alongside the number."""
    records = [{"topic_subject": "t", "title": "t",
                "measurement": {"actual_views": 500, "retention": {"available": False}},
                "latest_measurement": {"actual_views": 500, "retention": {"available": False}}}
               for _ in range(4)]
    monkeypatch.setattr(predict.store, "measured_records", lambda: records)
    monkeypatch.setattr(predict, "self_improve_active", lambda now=None: (False, 2))

    basis = predict.predict("t")["basis"]
    assert basis["median_recent"] == 500          # our own number, unblended
    assert basis["prior_views"] == benchmark.prior_views()
    assert basis["own_data_weight"] == pytest.approx(4 / 9, abs=5e-4)  # stored rounded
    assert basis["blended_baseline"] != 500       # and the blend actually applied


def test_cold_start_uses_the_benchmark_not_the_constant(monkeypatch):
    """The specific thing this replaces: an arbitrary 25 as the first guess."""
    monkeypatch.setattr(predict.store, "measured_records", lambda: [])
    monkeypatch.setattr(predict, "self_improve_active", lambda now=None: (False, 0))

    result = predict.predict("anything")
    assert result["predicted_views"] == benchmark.prior_views()
    assert result["model_version"] == "benchmark-v1"
    assert result["predicted_views"] != predict.COLD_START_PREDICTION
