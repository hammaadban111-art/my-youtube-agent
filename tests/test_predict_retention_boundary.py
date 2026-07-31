import agent.predict
from agent.predict import predict


def test_predict_low_confidence_on_early_drop_off(monkeypatch):
    """Guards against high-view low-retention topics being assigned high confidence."""
    monkeypatch.setattr(agent.predict, "self_improve_active", lambda now=None: (True, 30))

    synthetic_records = [
        # 3 "other" records with views=100 and high hook retention (0.8)
        {
            "topic_subject": "other",
            "title": f"Other {i}",
            "measurement": {
                "actual_views": 100,
                "retention": {
                    "available": True,
                    "curve": [
                        {"position": 0.10, "watch_ratio": 0.8},
                        {"position": 0.25, "watch_ratio": 0.8},
                    ],
                },
            },
        }
        for i in range(3)
    ] + [
        # 5 "ghost ship" records with 4x views (400) but low hook retention (0.1)
        {
            "topic_subject": "ghost ship",
            "title": f"Ghost Ship {i}",
            "measurement": {
                "actual_views": 400,
                "retention": {
                    "available": True,
                    "curve": [
                        {"position": 0.10, "watch_ratio": 0.1},
                        {"position": 0.25, "watch_ratio": 0.1},
                    ],
                },
            },
        }
        for i in range(5)
    ]

    monkeypatch.setattr(agent.predict.store, "measured_records", lambda: synthetic_records)

    res = predict("ghost ship")
    assert res["confidence"] == "low"
    assert res["basis"]["early_drop_off"] is True


def test_predict_medium_confidence_on_normal_retention(monkeypatch):
    """Guards against false early drop-off flags when topic retention is normal."""
    monkeypatch.setattr(agent.predict, "self_improve_active", lambda now=None: (True, 30))

    synthetic_records = [
        # 3 "other" records with views=100 and normal hook retention (0.8)
        {
            "topic_subject": "other",
            "title": f"Other {i}",
            "measurement": {
                "actual_views": 100,
                "retention": {
                    "available": True,
                    "curve": [
                        {"position": 0.10, "watch_ratio": 0.8},
                        {"position": 0.25, "watch_ratio": 0.8},
                    ],
                },
            },
        }
        for i in range(3)
    ] + [
        # 5 "ghost ship" records with views=400 and normal hook retention (0.8)
        {
            "topic_subject": "ghost ship",
            "title": f"Ghost Ship {i}",
            "measurement": {
                "actual_views": 400,
                "retention": {
                    "available": True,
                    "curve": [
                        {"position": 0.10, "watch_ratio": 0.8},
                        {"position": 0.25, "watch_ratio": 0.8},
                    ],
                },
            },
        }
        for i in range(5)
    ]

    monkeypatch.setattr(agent.predict.store, "measured_records", lambda: synthetic_records)

    res = predict("ghost ship")
    assert res["confidence"] == "medium"
    assert res["basis"]["early_drop_off"] is False


def test_predict_no_retention_data_fallback(monkeypatch):
    """Guards against model version regression when retention data is missing."""
    monkeypatch.setattr(agent.predict, "self_improve_active", lambda now=None: (True, 30))

    synthetic_records = [
        {
            "topic_subject": "ghost ship",
            "title": f"Ghost Ship {i}",
            "measurement": {
                "actual_views": 200,
                "retention": {"available": False},
            },
        }
        for i in range(8)
    ]

    monkeypatch.setattr(agent.predict.store, "measured_records", lambda: synthetic_records)

    res = predict("ghost ship")
    assert res["model_version"] == "learned-v1"
    assert res["basis"]["retention_multiplier"] is None
