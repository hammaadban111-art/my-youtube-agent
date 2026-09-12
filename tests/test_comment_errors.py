"""Guards the comment-read classification added 2026-09-09.

The bug: fetch_comments() wrapped its API call in `except Exception: return []`.
Every failure — an expired scope, an exhausted quota, a DNS blip, a suspended
account — was therefore written into the video's record as an empty comment
list, which reads identically to a video nobody commented on. The channel shows
27 comments across 114 videos and there was no way to know how much of that
silence was real.

Comments being DISABLED is the interesting edge: that is a true fact about the
video, discovered successfully, and must not be filed as a failure of ours.
"""
import pytest

from agent import followup, store, youtube_stats


class _HttpishError(Exception):
    """Stands in for googleapiclient.errors.HttpError, which needs a real
    httplib2 response object to construct. Only the message text matters — that
    is what the classifier reads."""


def _stub_threads(monkeypatch, result=None, raises=None):
    class _Req:
        def execute(self):
            if raises is not None:
                raise raises
            return result

    class _Threads:
        def list(self, **kw):
            return _Req()

    class _Service:
        def commentThreads(self):
            return _Threads()

    monkeypatch.setattr(youtube_stats, "_get_service", lambda: _Service())
    monkeypatch.setattr(youtube_stats.quota, "record_units", lambda n: None)


def _thread(author, text, likes):
    return {"snippet": {"topLevelComment": {"snippet": {
        "authorDisplayName": author, "textDisplay": text,
        "likeCount": likes, "publishedAt": "2026-09-09T00:00:00Z"}}}}


def test_a_video_with_real_comments_reads_them_back(monkeypatch):
    _stub_threads(monkeypatch, result={"items": [
        _thread("a", "first", 2), _thread("b", "second", 9)]})
    out = youtube_stats.fetch_comments_result("vid")
    assert out["available"] is True
    assert out["reason"] is None
    assert [c["likes"] for c in out["items"]] == [9, 2]   # most-liked first


def test_genuinely_zero_comments_is_a_successful_read(monkeypatch):
    _stub_threads(monkeypatch, result={"items": []})
    out = youtube_stats.fetch_comments_result("vid")
    assert out["available"] is True
    assert out["items"] == []
    assert out["reason"] is None


def test_an_auth_failure_is_not_zero_comments(monkeypatch):
    """The regression. Before this, both of these returned []."""
    _stub_threads(monkeypatch, raises=_HttpishError(
        "insufficient authentication scopes"))
    out = youtube_stats.fetch_comments_result("vid")
    assert out["available"] is False
    assert out["items"] == []
    assert "insufficient authentication scopes" in out["reason"]


def test_a_quota_failure_is_not_zero_comments(monkeypatch):
    _stub_threads(monkeypatch, raises=_HttpishError("quotaExceeded"))
    out = youtube_stats.fetch_comments_result("vid")
    assert out["available"] is False
    assert "quotaExceeded" in out["reason"]


def test_comments_disabled_is_a_successful_answer_about_the_video(monkeypatch):
    """Not our failure: the video really does have no comment section."""
    _stub_threads(monkeypatch, raises=_HttpishError(
        "commentsDisabled: The video has disabled comments."))
    out = youtube_stats.fetch_comments_result("vid")
    assert out["available"] is True
    assert out["disabled"] is True
    assert out["items"] == []


def test_the_list_shim_still_works_for_old_callers(monkeypatch):
    _stub_threads(monkeypatch, result={"items": [_thread("a", "hi", 1)]})
    assert [c["text"] for c in youtube_stats.fetch_comments("vid")] == ["hi"]


def test_a_failed_read_does_not_erase_comments_already_collected(monkeypatch, tmp_path):
    """A token blip on the 21:00 sweep must not delete real comments that the
    09:00 sweep collected."""
    monkeypatch.setattr(store, "DATA_DIR", str(tmp_path))
    record = {
        "video_id": "v1", "title": "t", "uploaded_at": "2026-09-01T00:00:00Z",
        "comments": [{"author": "a", "text": "kept", "likes": 3,
                      "published_at": "2026-09-01T01:00:00Z"}],
        "measurement_history": [], "measurement": {}, "latest_measurement": {},
    }
    monkeypatch.setattr(followup.youtube_stats, "fetch_stats",
                        lambda vid: {"actual_views": 10, "likes": 1, "comment_count": 1})
    monkeypatch.setattr(followup.youtube_stats, "fetch_comments_result",
                        lambda vid, limit=5: {"available": False,
                                              "reason": "RefreshError: nope",
                                              "disabled": False, "items": []})
    monkeypatch.setattr(followup.youtube_stats, "fetch_retention",
                        lambda vid, uploaded_at_date=None: {"available": False,
                                                            "reason": "test"})
    followup._take_reading(record)

    assert [c["text"] for c in record["comments"]] == ["kept"]
    assert record["comments_status"]["available"] is False
    assert "RefreshError" in record["comments_status"]["reason"]
