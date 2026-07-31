import copy
from agent import store


def test_first_reading_sets_initial_measurement_and_history():
    """Guards initial measurement field population and history tracking for fresh records."""
    record = store.new_record("vid_001", {"title": "Sample Title"})
    reading1 = {
        "measured_at": "2026-07-31T10:00:00Z",
        "hours_after_upload": 5,
        "actual_views": 150,
        "likes": 12,
        "comment_count": 3,
    }

    store.record_measurement(record, reading1)

    assert record["measurement"] == reading1
    assert record["latest_measurement"] == reading1
    assert len(record["measurement_history"]) == 1
    assert record["measurement_history"][0] == {
        "measured_at": "2026-07-31T10:00:00Z",
        "actual_views": 150,
    }


def test_second_reading_freezes_first_measurement():
    """Guards prediction training data against being overwritten by subsequent readings."""
    record = store.new_record("vid_002", {"title": "Sample Title"})
    reading1 = {
        "measured_at": "2026-07-31T10:00:00Z",
        "hours_after_upload": 5,
        "actual_views": 150,
        "likes": 12,
        "comment_count": 3,
    }
    store.record_measurement(record, reading1)
    frozen_measurement = copy.deepcopy(record["measurement"])

    reading2 = {
        "measured_at": "2026-08-01T10:00:00Z",
        "hours_after_upload": 29,
        "actual_views": 450,
        "likes": 40,
        "comment_count": 10,
    }
    store.record_measurement(record, reading2)

    assert record["measurement"] == frozen_measurement
    assert record["latest_measurement"] == reading2
    assert len(record["measurement_history"]) == 2


def test_three_readings_freeze_first_measurement():
    """Guards first measurement freeze across multiple sequential readings."""
    record = store.new_record("vid_003", {"title": "Sample Title"})
    reading1 = {
        "measured_at": "2026-07-31T10:00:00Z",
        "hours_after_upload": 5,
        "actual_views": 150,
    }
    store.record_measurement(record, reading1)
    frozen_measurement = copy.deepcopy(record["measurement"])

    reading2 = {
        "measured_at": "2026-08-01T10:00:00Z",
        "hours_after_upload": 29,
        "actual_views": 450,
    }
    store.record_measurement(record, reading2)

    reading3 = {
        "measured_at": "2026-08-02T10:00:00Z",
        "hours_after_upload": 53,
        "actual_views": 900,
    }
    store.record_measurement(record, reading3)

    assert record["measurement"] == frozen_measurement
    assert record["latest_measurement"] == reading3
    assert len(record["measurement_history"]) == 3


def test_legacy_record_history_fallback():
    """Guards against history loss or initial measurement overwrite when processing legacy records without measurement_history."""
    legacy_record = {
        "schema_version": 1,
        "video_id": "legacy_vid",
        "uploaded_at": "2026-07-01T00:00:00Z",
        "measurement": {
            "measured_at": "2026-07-01T05:00:00Z",
            "hours_after_upload": 5,
            "actual_views": 300,
            "likes": 20,
            "comment_count": 5,
        },
        "latest_measurement": {
            "measured_at": "2026-07-01T05:00:00Z",
            "hours_after_upload": 5,
            "actual_views": 300,
            "likes": 20,
            "comment_count": 5,
        },
    }
    # Explicitly ensure measurement_history key is missing from legacy record
    assert "measurement_history" not in legacy_record

    original_measurement = copy.deepcopy(legacy_record["measurement"])
    new_reading = {
        "measured_at": "2026-07-02T05:00:00Z",
        "hours_after_upload": 29,
        "actual_views": 750,
        "likes": 50,
        "comment_count": 12,
    }

    store.record_measurement(legacy_record, new_reading)

    assert len(legacy_record["measurement_history"]) == 2
    assert legacy_record["measurement_history"][0] == {
        "measured_at": "2026-07-01T05:00:00Z",
        "actual_views": 300,
    }
    assert legacy_record["measurement_history"][1] == {
        "measured_at": "2026-07-02T05:00:00Z",
        "actual_views": 750,
    }
    assert legacy_record["measurement"] == original_measurement
    assert legacy_record["latest_measurement"] == new_reading
