from datetime import datetime, timedelta, timezone

from agent import dashboard, ci_status, store

def _mk_run(started, status="completed", conclusion="success", error=None, finished_at=None):
    r = {
        "run_id": 123,
        "url": "http://run",
        "started_at": store.iso(started),
        "status": status,
        "conclusion": conclusion,
        "event": "schedule"
    }
    if error:
        r["error"] = error
    if status == "completed":
        if finished_at:
            r["finished_at"] = store.iso(finished_at)
        else:
            r["finished_at"] = store.iso(started + timedelta(minutes=10))
    return r

def _mk_record(uploaded_at, title="Vid", video_id="abc"):
    return {
        "title": title,
        "video_id": video_id,
        "url": f"http://vid/{video_id}",
        "uploaded_at": store.iso(uploaded_at)
    }

class MockDatetime(datetime):
    _now_val = None
    @classmethod
    def now(cls, tz=None):
        if cls._now_val:
            return cls._now_val
        return datetime.now(tz)

def test_published_with_record_in_window(monkeypatch):
    now = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
    MockDatetime._now_val = now
    monkeypatch.setattr(dashboard, "datetime", MockDatetime)
    
    started = now - timedelta(hours=5)
    run = _mk_run(started)
    rec = _mk_record(started + timedelta(minutes=5), title="My Video", video_id="xyz")
    
    monkeypatch.setattr(ci_status, "recent_upload_runs", lambda limit=12: {"available": True, "runs": [run]})
    
    res = dashboard._scheduled_uploads([rec])
    assert res["available"] is True
    slot = next(s for s in res["slots"] if s["started_at"] == run["started_at"])
    assert slot["state"] == "published"
    assert slot["title"] == "My Video"
    assert slot["url"] == "http://vid/xyz"

def test_failed_run_carries_plain_english_error(monkeypatch):
    now = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
    MockDatetime._now_val = now
    monkeypatch.setattr(dashboard, "datetime", MockDatetime)
    
    started = now - timedelta(hours=5)
    run = _mk_run(started, conclusion="failure", error="Quota exceeded today.")
    
    monkeypatch.setattr(ci_status, "recent_upload_runs", lambda limit=12: {"available": True, "runs": [run]})
    
    res = dashboard._scheduled_uploads([])
    slot = next(s for s in res["slots"] if s["started_at"] == run["started_at"])
    assert slot["state"] == "not_uploaded"
    assert slot["reason"] == "Quota exceeded today."

def test_succeeded_run_with_no_record(monkeypatch):
    now = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
    MockDatetime._now_val = now
    monkeypatch.setattr(dashboard, "datetime", MockDatetime)
    
    started = now - timedelta(hours=5)
    run = _mk_run(started, conclusion="success") # no error
    
    monkeypatch.setattr(ci_status, "recent_upload_runs", lambda limit=12: {"available": True, "runs": [run]})
    
    res = dashboard._scheduled_uploads([])
    slot = next(s for s in res["slots"] if s["started_at"] == run["started_at"])
    assert slot["state"] == "not_uploaded"
    assert "did not publish" in slot["reason"]

def test_cron_mark_older_than_4h_with_no_run(monkeypatch):
    now = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
    MockDatetime._now_val = now
    monkeypatch.setattr(dashboard, "datetime", MockDatetime)
    
    # cron marks are 1, 6, 11, 16.
    # The 06:07 mark is ~6 hours ago (older than 4h)
    monkeypatch.setattr(ci_status, "recent_upload_runs", lambda limit=12: {"available": True, "runs": []})
    
    res = dashboard._scheduled_uploads([])
    
    mark = now.replace(hour=6, minute=7)
    slot = next((s for s in res["slots"] if s["started_at"] == store.iso(mark)), None)
    assert slot is not None
    assert slot["state"] == "not_uploaded"
    assert "never started" in slot["reason"]

def test_cron_mark_only_1h_old_no_slot_emitted(monkeypatch):
    now = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
    MockDatetime._now_val = now
    monkeypatch.setattr(dashboard, "datetime", MockDatetime)
    
    # The 11:07 mark is only 53 minutes ago (less than 4h)
    monkeypatch.setattr(ci_status, "recent_upload_runs", lambda limit=12: {"available": True, "runs": []})
    
    res = dashboard._scheduled_uploads([])
    mark = now.replace(hour=11, minute=7)
    slot = next((s for s in res["slots"] if s["started_at"] == store.iso(mark)), None)
    assert slot is None # queued, not emitted

def test_record_grace_window(monkeypatch):
    now = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
    MockDatetime._now_val = now
    monkeypatch.setattr(dashboard, "datetime", MockDatetime)
    
    started = now - timedelta(hours=5)
    finished_at = started + timedelta(minutes=10)
    run = _mk_run(started, finished_at=finished_at)
    
    # Uploaded 3 minutes after finish
    rec = _mk_record(finished_at + timedelta(minutes=3))
    
    monkeypatch.setattr(ci_status, "recent_upload_runs", lambda limit=12: {"available": True, "runs": [run]})
    res = dashboard._scheduled_uploads([rec])
    slot = next(s for s in res["slots"] if s["started_at"] == run["started_at"])
    assert slot["state"] == "published"

def test_ci_status_unavailable(monkeypatch):
    monkeypatch.setattr(ci_status, "recent_upload_runs", lambda limit=12: {"available": False})
    res = dashboard._scheduled_uploads([])
    assert res["available"] is False
    assert res["published_24h"] == 0
    assert res["expected_24h"] == 0
    assert res["slots"] == []
    
def test_published_24h_counts_only_last_24h(monkeypatch):
    now = datetime(2026, 8, 12, 18, 0, tzinfo=timezone.utc)
    MockDatetime._now_val = now
    monkeypatch.setattr(dashboard, "datetime", MockDatetime)
    
    # Marks in the last 24h for 18:00: Aug 12 1:07, 6:07, 11:07, 16:07.
    # 16:07 is within 4h, so it's ignored if missing.
    # We will provide runs for 1:07, 6:07, and 11:07 (all < 24h).
    # We will also provide a run for Aug 11 11:07 (31h ago, > 24h).
    old_run = _mk_run(now.replace(day=11, hour=11, minute=7)) # > 24h old
    r1 = _mk_run(now.replace(hour=1, minute=7))
    r2 = _mk_run(now.replace(hour=6, minute=7))
    r3 = _mk_run(now.replace(hour=11, minute=7))
    
    old_rec = _mk_record(now.replace(day=11, hour=11, minute=12))
    rec1 = _mk_record(now.replace(hour=1, minute=12))
    rec2 = _mk_record(now.replace(hour=6, minute=12))
    rec3 = _mk_record(now.replace(hour=11, minute=12))
    
    monkeypatch.setattr(ci_status, "recent_upload_runs", lambda limit=12: {"available": True, "runs": [old_run, r1, r2, r3]})
    
    res = dashboard._scheduled_uploads([old_rec, rec1, rec2, rec3])
    # The old run is > 24h old (ignored). r1, r2, r3 are in the last 24h.
    assert res["expected_24h"] == 3
    assert res["published_24h"] == 3


def test_a_run_that_died_after_uploading_is_not_called_not_uploaded(monkeypatch):
    """The orphan-record case. daily.yml uploads in "Run agent" and saves the
    record in a LATER step, so a failure in that later step means the video is
    already live on the channel - which is how three videos (1,360 views) sat
    invisible until 2026-08-11. Labelling that "Not uploaded" would send
    someone hunting for a video that is already public, and it contradicts the
    plain-English cause for a conflict failure, which says the upload was
    fine."""
    monkeypatch.setattr(ci_status, "recent_upload_runs", lambda limit=12: {
        "available": True,
        "runs": [{
            "run_id": 1, "url": "https://example/run/1",
            "started_at": "2026-08-11T07:32:06Z",
            "finished_at": "2026-08-11T07:43:00Z",
            "status": "completed", "conclusion": "failure", "event": "schedule",
            "failed_step": "Persist topic history and video record",
            "error": "Persist topic history and video record: Two runs overlapped...",
        }],
    })
    result = dashboard._scheduled_uploads([])
    slot = next(s for s in result["slots"] if s.get("run_url") == "https://example/run/1")
    assert slot["state"] == "uploaded_no_record"
    assert "live on the channel" in slot["reason"]
    assert "backfill_orphan_records.py" in slot["reason"]


def test_a_run_that_died_during_the_upload_step_is_not_uploaded(monkeypatch):
    """The other half of the same distinction: "Run agent" IS the step that
    publishes, so a failure there means no video exists."""
    monkeypatch.setattr(ci_status, "recent_upload_runs", lambda limit=12: {
        "available": True,
        "runs": [{
            "run_id": 2, "url": "https://example/run/2",
            "started_at": "2026-08-12T03:26:43Z",
            "finished_at": "2026-08-12T03:28:30Z",
            "status": "completed", "conclusion": "failure", "event": "schedule",
            "failed_step": "Run agent",
            "error": "Run agent: Skipped on purpose: not enough YouTube quota left today.",
        }],
    })
    result = dashboard._scheduled_uploads([])
    slot = next(s for s in result["slots"] if s.get("run_url") == "https://example/run/2")
    assert slot["state"] == "not_uploaded"
    assert "quota" in slot["reason"]
