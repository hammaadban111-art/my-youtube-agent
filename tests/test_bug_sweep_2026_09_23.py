"""Regression tests for the full-codebase bug sweep of 2026-09-23.

One test (or a small group) per defect, each named for what went wrong. Every
one of these failed, or would have failed, against the code before the sweep.
"""
import json
import os
from unittest.mock import MagicMock

import httplib2
import pytest
from googleapiclient.errors import HttpError

from agent import (benchmark, checkpoint, ci_status, dashboard, grounding,
                   main, packet, script_writer, store, tts, upload, visuals)
from scripts import publish_parked
from scripts import resolve_data_conflicts as rdc
from scripts import reupload_video

ROOT = os.path.join(os.path.dirname(__file__), "..")


# ------------------------------------------------------------ fact-checking

def test_corroboration_matches_whole_words_not_substrings():
    """'war' inside 'software' and 'ship' inside 'relationship' used to count,
    pushing a claim the article never makes over the two-thirds line."""
    corpus = [("A", "Their relationship with the software warning was awarded in 1761.")]
    verdicts = grounding.corroborate(
        [{"segment_index": 0, "text": "A war ship sank in 1761"}], corpus)
    assert verdicts[0]["verdict"] == "SILENT"


def test_corroboration_still_folds_plurals_and_possessives():
    corpus = [("A", "The ships' crews found the lighthouse keepers missing in 1900.")]
    verdicts = grounding.corroborate(
        [{"segment_index": 0, "text": "The ship's keeper went missing in 1900"}], corpus)
    assert verdicts[0]["verdict"] == "SUPPORTED"


def test_a_string_segment_index_on_the_payoff_line_still_blocks():
    report = {"status": "checked", "final_segment_index": 4,
              "verdicts": [{"verdict": "CONTRADICTED", "segment_index": "4",
                            "claim": "wrong ending", "correction": None}]}
    with pytest.raises(grounding.ContradictedFinalSegment):
        grounding.enforce_publication_policy(report)


# ------------------------------------------------------------ topic categories

@pytest.mark.parametrize("title", [
    "Toward the Award", "An Effortless Comfort", "The Driver Who Kept Going",
])
def test_categories_do_not_match_inside_other_words(title):
    assert benchmark.categorize(title) is None


def test_category_stems_still_match_at_the_start_of_a_word():
    assert benchmark.categorize("The Sodder children disappearance") == "disappearance"
    assert benchmark.categorize("An archaeological dig in Chad") == "historical"


# ------------------------------------------------------------ conflict merges

def test_split_sides_handles_an_empty_side_between_two_hunks():
    text = ('{\n"a": [\n<<<<<<< HEAD\n=======\n  1\n>>>>>>> theirs\n],\n'
            '"b": [\n<<<<<<< HEAD\n  2\n=======\n  3\n>>>>>>> theirs\n]\n}\n')
    ours, theirs = rdc.split_sides(text)
    assert json.loads(ours) == {"a": [], "b": [2]}
    assert json.loads(theirs) == {"a": [1], "b": [3]}


def test_split_sides_drops_a_diff3_base_section():
    text = ('{\n<<<<<<< HEAD\n"a": 2\n||||||| base\n"a": 1\n=======\n"a": 3\n'
            '>>>>>>> theirs\n}\n')
    ours, theirs = rdc.split_sides(text)
    assert json.loads(ours) == {"a": 2} and json.loads(theirs) == {"a": 3}


def test_split_sides_rejects_an_unterminated_hunk():
    with pytest.raises(ValueError):
        rdc.split_sides('{\n<<<<<<< HEAD\n"a": 2\n=======\n"a": 3\n}\n')


def test_channel_history_merges_by_day_and_keeps_a_good_reading():
    ours = {"schema_version": 1, "snapshots": [
        {"day": "2026-09-20", "available": True, "measured_at": "2026-09-20T01:00:00Z"},
        {"day": "2026-09-21", "available": True, "measured_at": "2026-09-21T01:00:00Z",
         "subscriber_count": 70}]}
    theirs = {"schema_version": 1, "snapshots": [
        {"day": "2026-09-21", "available": False, "measured_at": "2026-09-21T09:00:00Z"},
        {"day": "2026-09-22", "available": True, "measured_at": "2026-09-22T01:00:00Z"}]}
    merged = rdc.merge_channel_history(ours, theirs)
    assert [r["day"] for r in merged["snapshots"]] == ["2026-09-20", "2026-09-21", "2026-09-22"]
    assert merged["snapshots"][1]["subscriber_count"] == 70


# ------------------------------------------------------------ upload

def _youtube_returning(video_id="NEW"):
    insert = MagicMock()
    insert.execute.return_value = {"id": video_id}
    youtube = MagicMock()
    youtube.videos().insert.return_value = insert
    return youtube


@pytest.fixture
def upload_env(monkeypatch):
    youtube = _youtube_returning()
    monkeypatch.setattr(upload, "_get_service", lambda: youtube)
    monkeypatch.setattr(upload, "MediaFileUpload", MagicMock())
    park = MagicMock(return_value="parked.mp4")
    monkeypatch.setattr(upload, "park_for_next_run", park)
    monkeypatch.setattr("time.sleep", lambda s: None)
    return {"youtube": youtube, "park": park}


def test_a_ledger_error_after_a_successful_insert_does_not_upload_twice(upload_env, monkeypatch):
    def broken(*_):
        raise OSError("disk full")
    monkeypatch.setattr(upload.quota, "record_upload", broken)
    assert upload.upload_video("v.mp4", "t", "d") == "NEW"
    assert upload_env["youtube"].videos().insert.call_count == 1


def test_no_upload_slots_parks_the_render_instead_of_losing_it(upload_env, monkeypatch):
    monkeypatch.setattr(upload.quota, "can_upload", lambda: False)
    with pytest.raises(RuntimeError):
        upload.upload_video("v.mp4", "t", "d")
    upload_env["park"].assert_called_once()


def test_park_on_failure_false_does_not_copy_an_already_parked_render(upload_env, monkeypatch):
    insert = MagicMock()
    insert.execute.side_effect = HttpError(httplib2.Response({"status": 400}), b"bad")
    upload_env["youtube"].videos().insert.return_value = insert
    with pytest.raises(HttpError):
        upload.upload_video("v.mp4", "t", "d", park_on_failure=False)
    upload_env["park"].assert_not_called()


# ------------------------------------------------------------ parked recovery

def _park(tmp_path, name, **extra):
    video = tmp_path / f"{name}.mp4"
    video.write_bytes(b"mp4")
    meta = {"title": f"Title {name}", "description": "d", "video_file": video.name,
            "parked_at": name, "bundle_id": name, "reason": "x",
            "script": {"segments": [], "story_id": ""}, **extra}
    (tmp_path / f"{name}.json").write_text(json.dumps(meta))
    return tmp_path / f"{name}.json"


@pytest.fixture
def recovery(monkeypatch, tmp_path):
    monkeypatch.setattr(upload, "PENDING_DIR", str(tmp_path))
    monkeypatch.setattr(main.velocity, "check", lambda: {})
    monkeypatch.setattr(main.quota, "reconcile_uploads", lambda records: 0)
    monkeypatch.setattr(main.quota, "can_upload", lambda: True)
    monkeypatch.setattr(main.predict, "predict", lambda subject: {"predicted_views": 1})
    monkeypatch.setattr(main.upload, "build_tags", lambda script, niche: [])
    monkeypatch.setattr(main.store, "save_record", MagicMock())
    monkeypatch.setattr(main.history, "append_entry", MagicMock())
    monkeypatch.setattr(main.store, "all_records", lambda: [])
    uploader = MagicMock(return_value="LIVE")
    monkeypatch.setattr(main.upload, "upload_video", uploader)
    return {"dir": tmp_path, "upload": uploader}


def test_an_already_published_oldest_bundle_does_not_hide_the_next_one(recovery, monkeypatch):
    first = _park(recovery["dir"], "20260901T000000Z-aaaa")
    _park(recovery["dir"], "20260902T000000Z-bbbb")
    monkeypatch.setattr(main.store, "all_records", lambda: [
        {"video_id": "OLD", "recovered_from_parked": {"bundle_id": "20260901T000000Z-aaaa"}}])
    assert main._recover_parked_video() is True
    assert recovery["upload"].call_args.args[1] == "Title 20260902T000000Z-bbbb"
    assert not first.exists()          # the published leftover was cleaned up


def test_a_guardrail_refusal_leaves_no_claim_behind(recovery, monkeypatch):
    meta_path = _park(recovery["dir"], "20260901T000000Z-aaaa")
    monkeypatch.setattr(main.quota, "can_upload", lambda: False)
    with pytest.raises(RuntimeError):
        main._recover_parked_video()
    assert not os.path.exists(str(meta_path).replace(".json", ".claimed.json"))
    recovery["upload"].assert_not_called()


def test_a_refused_recovery_upload_releases_the_claim(recovery):
    meta_path = _park(recovery["dir"], "20260901T000000Z-aaaa")
    recovery["upload"].side_effect = HttpError(httplib2.Response({"status": 403}),
                                               b"quotaExceeded")
    with pytest.raises(HttpError):
        main._recover_parked_video()
    assert recovery["upload"].call_args.kwargs["park_on_failure"] is False
    assert not os.path.exists(str(meta_path).replace(".json", ".claimed.json"))
    assert meta_path.exists()          # still parked, still retryable


def test_an_ambiguous_recovery_upload_keeps_the_claim_and_flags_the_bundle(recovery):
    meta_path = _park(recovery["dir"], "20260901T000000Z-aaaa")
    recovery["upload"].side_effect = upload.AmbiguousUploadError("lost response")
    with pytest.raises(upload.AmbiguousUploadError):
        main._recover_parked_video()
    assert os.path.exists(str(meta_path).replace(".json", ".claimed.json"))
    assert json.loads(meta_path.read_text())["requires_manual_reconciliation"] is True
    assert len(list(recovery["dir"].glob("*.mp4"))) == 1   # no second copy


# ------------------------------------------------------------ stale resume

def test_a_publish_checkpoint_whose_video_is_recorded_is_not_recorded_again(monkeypatch, tmp_path):
    checkpoint.load()
    checkpoint.record("script", {"title": "t", "story_id": ""})
    checkpoint.record("publish", {"video_id": "LIVE", "duration_seconds": 35,
                                  "voice": "v", "tts_rate": "+8%"})
    monkeypatch.setattr(main.store, "all_records", lambda: [{"video_id": "LIVE"}])
    finish = MagicMock()
    monkeypatch.setattr(main, "_record_and_finish", finish)
    monkeypatch.setattr(main, "_recover_parked_video", lambda: True)
    main.run()
    finish.assert_not_called()
    assert not checkpoint.has("publish")


# ------------------------------------------------------------ small ones

def test_an_atomic_write_that_fails_leaves_the_old_file_intact(tmp_path):
    path = str(tmp_path / "rec.json")
    store.write_json_atomic(path, {"ok": 1})
    with pytest.raises(TypeError):
        store.write_json_atomic(path, {"bad": object()})
    assert json.loads(open(path).read()) == {"ok": 1}


def test_url_scaffolding_earns_no_relevance():
    video = {"url": "https://www.pexels.com/video/foggy-forest-123/"}
    assert visuals._relevance_score("old video footage.", video) == 0
    assert visuals._relevance_score("foggy forest dawn", video) == 2


def test_a_voice_switch_mid_narration_resynthesises_in_one_voice(monkeypatch):
    calls = []

    def fake_once(script):
        calls.append(1)
        tts._pass_voices.clear()
        tts._pass_voices.update({"a", "b"} if len(calls) == 1 else {"b"})
        return [], 35.0, 0.0
    monkeypatch.setattr(tts, "_synthesize_pass_once", fake_once)
    tts._synthesize_pass({"segments": []})
    assert len(calls) == 2


def test_skipping_every_due_story_is_not_reported_as_an_off_slot_run(monkeypatch):
    story = {"story_id": "st-1", "topic_subject": "Dyatlov Pass",
             "slot": {"utc": "2026-09-01T0107Z"}}
    later = {"story_id": "st-2", "topic_subject": "Other",
             "slot": {"utc": "2099-09-01T0107Z"}}
    monkeypatch.setattr(packet, "load_packet", lambda: {"stories": [story, later]})
    monkeypatch.setattr(packet.history, "published_subjects", lambda: ["Dyatlov Pass"])
    with pytest.raises(packet.PacketError, match="Every story that is due"):
        packet.claim_script(allow_early=False)


def test_a_whitespace_subject_does_not_crash_visual_query_rebuild():
    rebuilt = script_writer.derive_visual_queries("   ", ["a b"])
    assert len(rebuilt[0]["visual_query"].split()) >= 3


def test_ci_minutes_count_every_workflow(monkeypatch):
    monkeypatch.setattr(ci_status, "_token", lambda: "t")
    monkeypatch.setattr(ci_status, "_repo", lambda: "o/r")
    monkeypatch.setattr(ci_status, "_repo_runs", lambda created_since=None: [
        {"id": 1, "status": "completed", "path": ".github/workflows/daily.yml"},
        {"id": 2, "status": "completed", "path": ".github/workflows/tests.yml"},
        {"id": 3, "status": "completed", "path": ".github/workflows/weekly.yml"},
    ])
    monkeypatch.setattr(ci_status, "_run_billable_ms", lambda run_id: (60000, "billable"))
    result = ci_status.ci_minutes_this_month()
    assert result["used_minutes"] == 3.0
    assert set(result["per_workflow_minutes"]) == {"daily.yml", "tests.yml", "weekly.yml"}


def test_the_dashboard_counts_only_supported_claims_as_verified():
    assert "function supportedClaims(fc)" in dashboard.PAGE
    assert "checked - contradicted}/${checked}" not in dashboard.PAGE
    # The replace dialog printed "undefined upload" — the field never existed.
    assert "q.upload_cost" not in dashboard.PAGE


def test_the_published_shell_matches_the_template():
    with open(os.path.join(ROOT, "public", "index.html")) as f:
        assert f.read() == dashboard.PAGE


def test_force_overrides_a_stale_claim(monkeypatch, tmp_path):
    meta_path = _park(tmp_path, "20260901T000000Z-aaaa")
    claim = tmp_path / "20260901T000000Z-aaaa.claimed.json"
    claim.write_text("{}")
    meta = publish_parked.discover(str(tmp_path))[0]
    monkeypatch.setattr(publish_parked.upload, "upload_video", MagicMock(return_value="LIVE"))
    monkeypatch.setattr(publish_parked.store, "save_record", MagicMock())
    monkeypatch.setattr(publish_parked.packet, "mark_published", MagicMock())
    monkeypatch.setattr(publish_parked.history, "append_entry", MagicMock())
    monkeypatch.setattr(publish_parked.predict, "predict", lambda s: {})
    assert publish_parked.publish_one(meta, "Subject", dry_run=False, force=True) == "LIVE"
    assert meta_path.exists()


def test_a_replacement_keeps_its_fallback_footage_and_voice():
    bundle = {"title": "t", "topic_subject": "Lake Natron",
              "script": {"voice": "en-GB-RyanNeural", "segments": [
                  {"narration": "n", "visual_keywords": "lake natron red water",
                   "visual_fallback": "salt lake"}]}}
    script = reupload_video.build_script(bundle, regenerate_visuals=False)
    assert script["segments"][0]["visual_fallback"] == "salt lake"
    assert tts.voice_for(script) == "en-GB-RyanNeural"


def test_the_parked_upload_cache_is_saved_even_when_emptied():
    with open(os.path.join(ROOT, ".github", "workflows", "daily.yml")) as f:
        text = f.read()
    step = text.split("- name: Check for a parked upload to preserve", 1)[1]
    step = step.split("- name:", 1)[0]
    assert "mkdir -p workdir/pending_upload" in step
    assert "present=true" in step
