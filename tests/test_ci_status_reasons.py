import pytest
from datetime import datetime, timezone
from agent import ci_status

def test_extract_error_gemini_503():
    log_text = 'google.genai.errors.ServerError: 503 UNAVAILABLE. {"error": {"code": 503, "message": "This model is currently experiencing high demand."}}'
    r = ci_status._extract_error(log_text, "Run agent")
    assert "genai" not in r
    assert "503" not in r
    assert r.startswith("Run agent: ")
    assert "Google's AI model was overloaded" in r

def test_evicted_run_reason(monkeypatch):
    monkeypatch.setattr(ci_status, "_api_get", lambda path: {"jobs": []})
    reason, step = ci_status._cancelled_reason(31771716659)
    assert step is None
    assert "before it even started" in reason
    assert "shared lock" in reason
    assert "Unknown error" not in reason


def test_evicted_followup_does_not_claim_a_lost_video(monkeypatch):
    """Only daily.yml publishes. Saying "no video was published" about an
    evicted follow-up run would invent a loss that never happened."""
    monkeypatch.setattr(ci_status, "_api_get", lambda path: {"jobs": []})
    daily, _ = ci_status._cancelled_reason(31781223061, "daily.yml")
    followup, _ = ci_status._cancelled_reason(31771716659, "followup.yml")
    assert "No video was published" in daily
    assert "video" not in followup
    assert "shared lock" in followup

def test_hung_6_hour_run(monkeypatch):
    def mock_api_get(path):
        return {
            "jobs": [{
                "conclusion": "cancelled",
                "started_at": "2026-08-14T03:27:12Z",
                "completed_at": "2026-08-14T09:27:28Z",
                "steps": [
                    {"name": "Setup", "conclusion": "success"},
                    {"name": "Run agent", "conclusion": "cancelled"}
                ]
            }]
        }
    monkeypatch.setattr(ci_status, "_api_get", mock_api_get)
    reason, step = ci_status._cancelled_reason(31766841885)
    assert step == "Run agent"
    assert "hung and GitHub killed it after 6 hours" in reason
    assert "Unknown error" not in reason

def test_cancelled_run_in_failures(monkeypatch):
    def mock_api_get(path):
        if "runs/31766841885/jobs" in path:
            return {
                "jobs": [{
                    "conclusion": "cancelled",
                    "started_at": "2026-08-14T03:27:12Z",
                    "completed_at": "2026-08-14T09:27:28Z",
                    "steps": [
                        {"name": "Run agent", "conclusion": "cancelled"}
                    ]
                }]
            }
        
        # Return 1 cancelled, 1 success for recent_failures list
        return {
            "workflow_runs": [
                {
                    "id": 31766841885,
                    "html_url": "http://run/1",
                    "created_at": "2026-08-14T03:27:12Z",
                    "conclusion": "cancelled"
                },
                {
                    "id": 2,
                    "html_url": "http://run/2",
                    "created_at": "2026-08-14T03:00:00Z",
                    "conclusion": "success"
                }
            ]
        }
    monkeypatch.setattr(ci_status, "_api_get", mock_api_get)
    monkeypatch.setattr(ci_status, "_token", lambda: "fake")
    monkeypatch.setattr(ci_status, "_repo", lambda: "fake/repo")
    
    res = ci_status.recent_failures()
    assert res["available"] is True
    # WORKFLOWS = ["daily.yml", "followup.yml"], so 2 cancelled runs returned total
    assert len(res["failures"]) == 2
    assert all(f["run_id"] == 31766841885 for f in res["failures"])
    assert all("hung and GitHub killed it" in f["error"] for f in res["failures"])


def test_recovered_503_does_not_shadow_the_real_cause():
    """gemini_utils prints "503 UNAVAILABLE" on every retry, including ones
    that then SUCCEED. Keying the Gemini summary off that line reported "no
    video was published" for a run whose video was already live and which
    actually died later on a git conflict — the same wrong-diagnosis class
    that hid three live videos (1,360 views) until 2026-08-11. The marker has
    to be the raised traceback, and it has to sit below the post-upload
    causes."""
    log_text = (
        "[gemini] generate_script (attempt 1): 503 UNAVAILABLE "
        "(attempt 1/6) — retrying in 10s\n"
        "      Done: https://youtube.com/watch?v=abc123XYZ\n"
        "CONFLICT (content): Merge conflict in data/quota_ledger.json\n"
        "error: You must edit all merge conflicts and then mark them as resolved\n"
    )
    r = ci_status._extract_error(log_text, "Persist topic history and video record")
    assert "uploaded fine" in r
    assert "no video was published" not in r.lower()


def test_retried_503_that_never_raised_is_not_reported_as_a_gemini_abort():
    """A retry line alone is not a Gemini failure; the run died on footage."""
    log_text = ("[gemini] verify_claims (attempt 1): 503 UNAVAILABLE "
                "(attempt 2/6) — retrying in 20s\n"
                "No Pexels results for segment 3\n")
    r = ci_status._extract_error(log_text, "Run agent")
    assert "stock footage" in r
    assert "Google's AI model" not in r
