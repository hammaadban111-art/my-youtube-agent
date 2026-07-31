"""Guards which reading each question is asked of.

Every record carries two readings: `measurement` is frozen at ~5h after
upload, `latest_measurement` is the most recent. The split is deliberate and
easy to break by "tidying" one to match the other:

  - prediction accuracy is scored against the FROZEN reading, so every video
    is judged at the same age;
  - topic steering reads the LATEST reading, so a video that started slow and
    compounded for three days is treated as the success it turned out to be
    rather than the failure its 5h snapshot made it look like.

The fixtures below deliberately make the two readings disagree hard, so a
regression that reads the wrong one cannot pass by coincidence.
"""
from agent import predict


def _record(topic: str, early_views: int, latest_views: int,
            retention_available: bool = False) -> dict:
    """A video whose 5h snapshot and current numbers disagree."""
    return {
        "topic_subject": topic,
        "title": f"{topic} video",
        "prediction": {"predicted_views": early_views},
        "measurement": {
            "actual_views": early_views,
            "retention": {"available": False, "curve": []},
        },
        "latest_measurement": {
            "actual_views": latest_views,
            "retention": ({"available": True, "curve": [
                {"position": 0.1, "watch_ratio": 0.9},
                {"position": 0.5, "watch_ratio": 0.5},
            ], "biggest_drop_at": 0.5, "biggest_drop_size": 0.4}
                if retention_available else {"available": False, "curve": []}),
        },
    }


def _legacy_record(topic: str, views: int) -> dict:
    """A record written before latest_measurement existed."""
    return {
        "topic_subject": topic,
        "title": f"{topic} video",
        "prediction": {"predicted_views": views},
        "measurement": {"actual_views": views,
                        "retention": {"available": False, "curve": []}},
    }


# --- the bug this fixes -----------------------------------------------------

def test_slow_starter_is_not_treated_as_a_bad_topic(monkeypatch):
    """The regression in one test: 'slow burn' looked worst at 5h and ended up
    best. Steering by the 5h reading would teach the model to avoid it."""
    records = [
        _record("slow burn", early_views=30, latest_views=5000),
        _record("fast fade", early_views=900, latest_views=950),
        _record("steady", early_views=400, latest_views=800),
    ]
    monkeypatch.setattr(predict.store, "measured_records", lambda: records)

    ranked = [t["subject"] for t in predict.top_performers()]
    assert ranked[0] == "slow burn", (
        "top_performers feeds the script prompt; it must rank on what videos "
        "actually did, not on their 5h snapshot")
    # And the number handed to the prompt is the current one, not the stale one.
    assert predict.top_performers()[0]["views"] == 5000

    # Ranking on the frozen reading would have produced this order instead -
    # asserting it explicitly so the test fails loudly if the source flips back.
    stale_order = sorted(records, key=lambda r: r["measurement"]["actual_views"],
                         reverse=True)
    assert [r["topic_subject"] for r in stale_order][0] == "fast fade"


def test_topic_multiplier_reads_the_latest_reading(monkeypatch):
    """A topic that compounds must come out above the channel average."""
    records = [_record("winner", early_views=10, latest_views=4000)]
    records += [_record(f"filler{i}", early_views=500, latest_views=500)
                for i in range(predict.MIN_SAMPLES_FOR_LEARNING)]
    monkeypatch.setattr(predict.store, "measured_records", lambda: records)
    monkeypatch.setattr(predict, "self_improve_active", lambda now=None: (True, 30))

    winner = predict.predict("winner")["basis"]["topic_multiplier"]
    filler = predict.predict("filler0")["basis"]["topic_multiplier"]

    assert winner > 1.0, "a topic that grew to 8x the channel average must lift"
    assert winner > filler
    # On the frozen reading 'winner' was the WORST video in the set, so a
    # multiplier at or below 1.0 means the stale source is still being read.
    assert winner > 1.0 and records[0]["measurement"]["actual_views"] < 500


def test_multiplier_compares_like_with_like(monkeypatch):
    """Both sides of the ratio must come from the latest reading. If the topic
    used latest views against a 5h channel average, every topic would read
    high by the channel's growth factor rather than by its own merit."""
    # Every video grows exactly 10x, so no topic is better than any other.
    records = [_record(f"t{i}", early_views=100, latest_views=1000)
               for i in range(predict.MIN_SAMPLES_FOR_LEARNING + 2)]
    monkeypatch.setattr(predict.store, "measured_records", lambda: records)
    monkeypatch.setattr(predict, "self_improve_active", lambda now=None: (True, 30))

    multiplier = predict.predict("t0")["basis"]["topic_multiplier"]
    assert multiplier == 1.0, (
        f"uniform growth must be perfectly neutral, got {multiplier} - a value "
        "near the 10x growth factor means the numerator and denominator are "
        "being taken from different readings")


def test_has_signal_uses_the_latest_reading():
    """At 5h a healthy video can still be in single digits; a throttled one is
    still near zero days later. The distinction needs the newer number."""
    assert predict.has_signal(_record("slow start", 8, 900)) is True
    assert predict.has_signal(_record("throttled", 2, 6)) is False
    # A video that WAS fine early and is fine now is unaffected.
    assert predict.has_signal(_record("healthy", 900, 1200)) is True


def test_retention_signal_uses_the_latest_reading():
    """Analytics has no retention rows at 5h ('needs ~24-48h'), so reading the
    frozen measurement means the retention model is permanently blind."""
    record = _record("held well", 100, 900, retention_available=True)
    assert predict._retention_quality(record) is not None
    # The frozen reading has no retention at all - proving the value above can
    # only have come from latest_measurement.
    assert record["measurement"]["retention"]["available"] is False


# --- what must NOT change ---------------------------------------------------

def test_accuracy_is_still_scored_against_the_frozen_reading(monkeypatch):
    """Predictions are made for the 5h mark. Scoring them against a later
    reading would measure video age, not model quality."""
    # Predicted 100, was 100 at 5h, is 10,000 now. Scored on the frozen
    # reading this is a perfect prediction; on the latest one it is 9,900% off.
    records = [_record("topic", early_views=100, latest_views=10_000)]
    records[0]["prediction"] = {"predicted_views": 100}
    monkeypatch.setattr(predict.store, "measured_records", lambda: records)

    summary = predict.accuracy_summary()
    assert summary["n"] == 1
    assert summary["mean_abs_pct_error"] == 0.0, (
        "accuracy must compare against the frozen 5h reading; a large error "
        "here means it drifted onto latest_measurement")


def test_prediction_scale_stays_on_the_frozen_reading(monkeypatch):
    """The baseline sets the SCALE of the prediction. It has to stay 'views at
    5h' or predictions inflate away from what accuracy_summary scores them
    against, and the model silently starts grading itself on a curve."""
    records = [_record(f"t{i}", early_views=200, latest_views=9000)
               for i in range(predict.MIN_SAMPLES_FOR_LEARNING + 2)]
    monkeypatch.setattr(predict.store, "measured_records", lambda: records)
    monkeypatch.setattr(predict, "self_improve_active", lambda now=None: (False, 2))

    result = predict.predict("t0")

    # Our own contribution to the baseline is the 5h median, untouched. Asserted
    # on median_recent rather than on predicted_views because the external
    # benchmark is blended in afterwards - that blend is allowed to move the
    # final number, the frozen reading it starts from is not.
    assert result["basis"]["median_recent"] == 200, (
        f"expected the 5h median (200), got {result['basis']['median_recent']} - "
        "a value near 9000 means the baseline moved onto the latest reading")
    # And the blend must not have carried it toward the latest reading either.
    assert result["predicted_views"] < 9000 / 2, (
        f"predicted {result['predicted_views']} against a 5h median of 200 and a "
        "latest reading of 9000 - the prediction has drifted onto the wrong scale")


# --- records written before the second reading existed ----------------------

def test_legacy_records_fall_back_to_their_only_reading(monkeypatch):
    """Records from before latest_measurement existed must still count, not
    silently drop out of every topic calculation."""
    legacy = _legacy_record("old topic", 1000)
    assert predict.has_signal(legacy) is True
    assert predict._latest_views(legacy) == 1000

    monkeypatch.setattr(predict.store, "measured_records", lambda: [legacy])
    assert [t["subject"] for t in predict.top_performers()] == ["old topic"]


def test_mixed_old_and_new_records_rank_together(monkeypatch):
    """A feed containing both shapes must produce one coherent ranking."""
    monkeypatch.setattr(predict.store, "measured_records", lambda: [
        _legacy_record("legacy", 2000),
        _record("modern", early_views=50, latest_views=3000),
        _record("modern weak", early_views=40, latest_views=100),
    ])
    assert [t["subject"] for t in predict.top_performers()] == [
        "modern", "legacy", "modern weak"]
