"""Tests for the delete + re-upload path (scripts/reupload_video.py).

The failure this guards against is expensive and irreversible: a replace
deletes a live video and spends 1,650 quota units, 16.5% of the day's entire
allowance. Every refusal below is a refusal that happens BEFORE anything is
rendered, uploaded or deleted.

Offline like the rest of the suite — no Gemini, Pexels or YouTube call.
"""
import json

import pytest

from agent import quota, upload, velocity
from scripts import reupload_video


@pytest.fixture
def bundle():
    """The real shape scripts/export_reuse_bundle.py writes."""
    return {
        "schema_version": 1,
        "video_id": "abc123",
        "title": "The Cold War Spy Who Never Existed",
        "topic_subject": "Tamam Shud case",
        "niche": "unsolved mysteries and bizarre history",
        "script": {
            "segments": [
                {"narration": "A body on a beach, one clue in his pocket.",
                 "visual_keywords": "fog over coastline"},
                {"narration": "Nobody could name him.", "visual_keywords": "old dark alley"},
            ],
            "voice": "en-US-GuyNeural", "tts_rate": "+8%",
            "target_length_seconds": 35, "segment_count": 2, "word_count": 13,
        },
        "grounding": {"status": "checked"},
        "description": None,
        "reuse": {"gemini_calls_needed": 0, "pexels_downloads_needed": 6,
                  "visual_queries_are_legacy_generic": True},
    }


@pytest.fixture
def clean_quota(monkeypatch, tmp_path):
    monkeypatch.setattr(quota, "LEDGER_PATH", str(tmp_path / "ledger.json"))


@pytest.fixture
def passing_preflight(monkeypatch, clean_quota):
    """Everything green, so each test can break exactly one thing."""
    monkeypatch.setattr(reupload_video, "find_record_by_id",
                        lambda vid, base_dir=None: ({"video_id": vid}, "path.json"))
    monkeypatch.setattr(reupload_video.store, "all_records", lambda: [])
    monkeypatch.setattr(upload, "can_delete", lambda: (True, "token holds the scope"))
    monkeypatch.setattr(velocity, "check", lambda: {"uploads_last_24h": 2, "uploads_last_48h": 4})


def test_preflight_passes_when_everything_is_in_order(passing_preflight, bundle):
    record, _ = reupload_video.preflight("abc123", bundle)
    assert record["video_id"] == "abc123"


def test_refuses_without_a_stored_record(passing_preflight, monkeypatch, bundle):
    monkeypatch.setattr(reupload_video, "find_record_by_id", lambda vid, base_dir=None: None)
    with pytest.raises(reupload_video.PreflightFailed, match="no stored record"):
        reupload_video.preflight("abc123", bundle)


def test_refuses_when_the_day_cannot_afford_it(passing_preflight, bundle):
    """5,351 used leaves under 1,650 before the reserve line at 7,000."""
    quota.record_units(5351)
    with pytest.raises(reupload_video.PreflightFailed, match="quota headroom"):
        reupload_video.preflight("abc123", bundle)


def test_the_cost_it_budgets_for_is_the_pair_not_just_the_upload(bundle):
    assert reupload_video.REUPLOAD_UNITS == 1650
    assert reupload_video.REUPLOAD_UNITS == quota.UNITS_PER_UPLOAD + quota.UNITS_PER_DELETE


def test_refuses_when_the_token_cannot_delete(passing_preflight, monkeypatch, bundle):
    """Found before the render, not after the replacement is already live."""
    monkeypatch.setattr(upload, "can_delete",
                        lambda: (False, "the stored refresh token has no delete scope"))
    with pytest.raises(reupload_video.PreflightFailed, match="no delete scope"):
        reupload_video.preflight("abc123", bundle)


def test_refuses_when_the_velocity_guardrail_is_blocking(passing_preflight, monkeypatch, bundle):
    """A replace is still an upload — it must not be the one that re-triggers
    the distribution throttle this channel already hit once."""
    def _blocked():
        raise velocity.VelocityBlocked("10 uploads in the last 24h")
    monkeypatch.setattr(velocity, "check", _blocked)
    with pytest.raises(reupload_video.PreflightFailed, match="10 uploads"):
        reupload_video.preflight("abc123", bundle)


def test_a_dry_run_reports_the_live_gates_instead_of_enforcing_them(
        passing_preflight, monkeypatch, capsys, bundle):
    """A rehearsal spends nothing and deletes nothing, so it must still run on
    a token that has not been re-minted yet."""
    monkeypatch.setattr(upload, "can_delete", lambda: (False, "no delete scope"))
    quota.record_units(9999)
    record, _ = reupload_video.preflight("abc123", bundle, dry_run=True)
    assert record["video_id"] == "abc123"
    out = capsys.readouterr().out
    assert "DRY RUN would have been blocked" in out
    assert "delete scope MISSING" in out


def test_a_dry_run_still_refuses_without_a_record(passing_preflight, monkeypatch, bundle):
    monkeypatch.setattr(reupload_video, "find_record_by_id", lambda vid, base_dir=None: None)
    with pytest.raises(reupload_video.PreflightFailed):
        reupload_video.preflight("abc123", bundle, dry_run=True)


def test_missing_bundle_is_refused_by_name(monkeypatch, tmp_path):
    monkeypatch.setattr(reupload_video, "REUSE_DIR", str(tmp_path))
    with pytest.raises(reupload_video.PreflightFailed, match="export_reuse_bundle"):
        reupload_video.load_bundle("nope")


def test_the_description_is_rebuilt_because_none_was_ever_stored(bundle):
    """No record on this channel ever persisted a YouTube description, so a
    replace has to write one — and its hashtags are what carry the subject
    into the video's tags."""
    assert bundle["description"] is None
    desc = reupload_video.rebuild_description(bundle)
    assert "A body on a beach" in desc
    assert "#TamamShudCase" in desc
    assert "#shorts" in desc


def test_the_rebuilt_description_produces_usable_tags(bundle):
    script = {"topic_subject": bundle["topic_subject"],
              "description": reupload_video.rebuild_description(bundle)}
    tags = upload.build_tags(script, bundle["niche"])
    assert "tamam shud case" in tags
    assert "tamamshudcase" in tags
    assert all(len(t) <= 100 for t in tags)
    assert len(tags) <= 15


def test_hashtags_are_deduplicated():
    """A niche containing "shorts" must not yield "#Shorts #shorts"."""
    desc = reupload_video.rebuild_description(
        {"topic_subject": "Shorts", "niche": "shorts", "title": "T",
         "script": {"segments": [{"narration": "N."}]}})
    tags = [w for w in desc.split() if w.startswith("#")]
    assert len(tags) == len({t.lower() for t in tags})


def test_build_script_maps_stored_keywords_to_the_render_field(bundle, monkeypatch):
    script = reupload_video.build_script(bundle, regenerate_visuals=False)
    assert [s["visual_query"] for s in script["segments"]] == ["fog over coastline", "old dark alley"]
    assert script["topic_subject"] == "Tamam Shud case"
    assert script["title"] == bundle["title"]


def test_legacy_generic_queries_are_regenerated_not_reused(bundle, monkeypatch):
    """Re-rendering qnAWjYhNlck's stored queries as-is would reproduce exactly
    the generic footage the anchored-visual fix removed."""
    called = {}

    def _fake(subject, narrations):
        called["subject"] = subject
        return [{"visual_query": "glenelg beach adelaide jetty",
                 "visual_fallback": "empty beach"} for _ in narrations]

    monkeypatch.setattr(reupload_video.script_writer, "regenerate_visual_queries", _fake)
    script = reupload_video.build_script(bundle, regenerate_visuals=True)
    assert called["subject"] == "Tamam Shud case"
    assert all(s["visual_query"] == "glenelg beach adelaide jetty" for s in script["segments"])
    assert all(s["visual_fallback"] == "empty beach" for s in script["segments"])


def test_an_empty_bundle_is_refused_rather_than_rendered(bundle):
    bundle["script"]["segments"] = []
    with pytest.raises(reupload_video.PreflightFailed, match="no segments"):
        reupload_video.build_script(bundle, regenerate_visuals=False)


def test_confirm_must_repeat_the_video_id(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["reupload_video.py", "abc123", "--confirm", "abc124"])
    assert reupload_video.main() == 2
    assert "Refusing" in capsys.readouterr().err


def test_a_live_run_without_confirm_is_refused(monkeypatch):
    monkeypatch.setattr("sys.argv", ["reupload_video.py", "abc123"])
    assert reupload_video.main() == 2


def test_delete_books_its_fifty_units(monkeypatch, clean_quota):
    """videos.delete is metered too — 50 units — and the ledger has to see it."""
    class _Videos:
        def delete(self, id):  # noqa: A002 - mirrors the API's own kwarg
            class _Req:
                def execute(self_inner):
                    return {}
            return _Req()

    class _Service:
        def videos(self):
            return _Videos()

    monkeypatch.setattr(upload, "build", lambda *a, **k: _Service())
    monkeypatch.setattr(upload, "_credentials", lambda scopes: None)
    upload.delete_video("abc123")
    assert quota.units_used_today() == quota.UNITS_PER_DELETE


def test_a_failed_delete_still_books_the_units(monkeypatch, clean_quota):
    """The request reached the API, so the units are gone whatever it said."""
    from googleapiclient.errors import HttpError

    class _Resp:
        status = 403
        reason = "forbidden"

    class _Videos:
        def delete(self, id):  # noqa: A002
            class _Req:
                def execute(self_inner):
                    raise HttpError(_Resp(), b"{}")
            return _Req()

    class _Service:
        def videos(self):
            return _Videos()

    monkeypatch.setattr(upload, "build", lambda *a, **k: _Service())
    monkeypatch.setattr(upload, "_credentials", lambda scopes: None)
    with pytest.raises(HttpError):
        upload.delete_video("abc123")
    assert quota.units_used_today() == quota.UNITS_PER_DELETE


def test_can_delete_reads_the_granted_scopes_not_the_requested_ones(monkeypatch):
    """Asking for a scope in Credentials(...) does not grant it — the grant was
    fixed when the token was minted."""
    monkeypatch.setattr(upload, "granted_scopes",
                        lambda: ["https://www.googleapis.com/auth/youtube.upload",
                                 "https://www.googleapis.com/auth/youtube.readonly"])
    ok, why = upload.can_delete()
    assert ok is False
    assert "get_refresh_token.py" in why

    monkeypatch.setattr(upload, "granted_scopes",
                        lambda: ["https://www.googleapis.com/auth/youtube.force-ssl"])
    assert upload.can_delete()[0] is True


def test_can_delete_treats_a_broken_refresh_as_a_no(monkeypatch):
    def _boom():
        raise RuntimeError("invalid_grant")
    monkeypatch.setattr(upload, "granted_scopes", _boom)
    ok, why = upload.can_delete()
    assert ok is False
    assert "invalid_grant" in why
