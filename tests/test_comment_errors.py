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
import json

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

    monkeypatch.setattr(youtube_stats, "_comments_service", lambda: _Service())
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


# ------------------------------------------- structured reason parsing (bug fix)

class _Resp:
    def __init__(self, status):
        self.status = status


class _RealisticHttpError(Exception):
    """Shaped like googleapiclient.errors.HttpError: the str() form opens with
    the request URL, and the machine-readable reason lives in .content."""

    def __init__(self, status, reason, video_id="abc123"):
        self.resp = _Resp(status)
        self.content = json.dumps({
            "error": {"code": status, "errors": [{"reason": reason,
                                                  "domain": "youtube.commentThread"}]}
        }).encode()
        super().__init__(
            f"<HttpError {status} when requesting "
            f"https://youtube.googleapis.com/youtube/v3/commentThreads"
            f"?part=snippet&videoId={video_id}&maxResults=20&order=relevance"
            f"&textFormat=plainText&alt=json returned '{reason}'>")


def test_the_reason_survives_the_length_cap(monkeypatch):
    """The bug this fixes, from live run 34689042038: 54 videos in one sweep
    failed with a 403 whose str() begins with the request URL. The 300-character
    cap fell before the reason did, so the stored message said nothing useful."""
    # Tested on a genuine FAILURE, because the disabled path deliberately
    # replaces the raw reason with a plain-English one — it is an answer about
    # the video, not an error to diagnose.
    _stub_threads(monkeypatch, raises=_RealisticHttpError(403, "forbidden"))
    out = youtube_stats.fetch_comments_result("abc123")
    assert "forbidden" in out["reason"]
    assert out["reason"].startswith("HTTP 403")
    # The old implementation put the URL first and capped at 300 characters, so
    # the reason fell off the end entirely.
    assert len(out["reason"]) <= 300


def test_a_realistic_disabled_error_is_classified_as_disabled(monkeypatch):
    """Before the fix the marker sat beyond the truncation point, so a video
    with comments switched off was filed as a failed read."""
    _stub_threads(monkeypatch, raises=_RealisticHttpError(403, "commentsDisabled"))
    out = youtube_stats.fetch_comments_result("abc123")
    assert out["available"] is True
    assert out["disabled"] is True


def test_a_realistic_scope_error_is_still_a_failure(monkeypatch):
    """The other half: a 403 that is OUR problem must not be mistaken for a
    video with comments turned off."""
    _stub_threads(monkeypatch, raises=_RealisticHttpError(403, "forbidden"))
    out = youtube_stats.fetch_comments_result("abc123")
    assert out["available"] is False
    assert "forbidden" in out["reason"]


def test_quota_errors_keep_their_reason_code(monkeypatch):
    _stub_threads(monkeypatch, raises=_RealisticHttpError(403, "quotaExceeded"))
    out = youtube_stats.fetch_comments_result("abc123")
    assert out["available"] is False
    assert "quotaExceeded" in out["reason"]


def test_an_unparseable_body_falls_back_to_the_text(monkeypatch):
    """No JSON body, no reason codes — the classifier must still answer rather
    than raise."""
    class _Bad(Exception):
        content = b"<html>gateway timeout</html>"
        resp = _Resp(504)
    _stub_threads(monkeypatch, raises=_Bad("boom"))
    out = youtube_stats.fetch_comments_result("abc123")
    assert out["available"] is False
    assert "HTTP 504" in out["reason"]


def test_reason_codes_are_extracted_from_the_body():
    err = _RealisticHttpError(403, "commentsDisabled")
    assert youtube_stats._api_error_reasons(err) == ["commentsDisabled"]


def test_an_exception_with_no_body_yields_no_reasons():
    assert youtube_stats._api_error_reasons(RuntimeError("plain")) == []


def test_comment_reads_ask_for_the_force_ssl_scope(monkeypatch):
    """commentThreads.list under OAuth needs youtube.force-ssl. google-auth
    narrows every refreshed access token to the scopes REQUESTED, so asking for
    the general SCOPES (upload + readonly) produced 403 insufficientPermissions
    on every video from the day comments were first read — even though the
    stored token held force-ssl all along. Verified against the live API on
    2026-09-22: same video, same token, only the requested scope changed."""
    requested = []

    def fake_credentials(scopes):
        requested.append(list(scopes))
        return object()

    monkeypatch.setattr(youtube_stats, "_credentials", fake_credentials)
    monkeypatch.setattr(youtube_stats, "build", lambda *a, **k: object())
    youtube_stats._comments_service()
    assert requested == [["https://www.googleapis.com/auth/youtube.force-ssl"]]
    assert youtube_stats.COMMENTS_SCOPE not in youtube_stats.SCOPES
