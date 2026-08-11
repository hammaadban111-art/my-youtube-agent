"""Tests for scripts/resolve_data_conflicts.py.

The regression: daily.yml failed on 2026-08-09, 08-10 and 08-11 with

    You must edit all merge conflicts and then mark them as resolved
    ! [rejected]  main -> main (non-fast-forward)

In each case the video had ALREADY been uploaded. Only the commit that
records it failed, which is how three videos ended up live with no record.

The first version of the resolver handled quota_ledger.json and topics.json
only. The 08-11 run reported 147 conflicted paths, nearly all of them
data/videos/*.json, because every run re-measures every tracked video. Those
went unresolved, the rebase could not continue, and the push was rejected.
"""
import json

from scripts import resolve_data_conflicts as rdc


def conflicted(ours: str, theirs: str) -> str:
    return f"<<<<<<< HEAD\n{ours}\n=======\n{theirs}\n>>>>>>> commit\n"


def test_split_sides_reconstructs_both_whole_documents():
    text = '{\n  "a": 1,\n' + conflicted('  "b": 2', '  "b": 3') + "}\n"
    ours, theirs = rdc.split_sides(text)
    assert json.loads(ours) == {"a": 1, "b": 2}
    assert json.loads(theirs) == {"a": 1, "b": 3}


def test_split_sides_returns_none_when_clean():
    assert rdc.split_sides('{"a": 1}') is None


def test_ledger_takes_the_newer_pacific_day():
    """A stale day says nothing about today's remaining quota."""
    old = {"pacific_date": "2026-08-08", "units_used": 4948, "uploads_recorded": ["a"]}
    new = {"pacific_date": "2026-08-09", "units_used": 156, "uploads_recorded": []}
    assert rdc.merge_ledger(old, new) == new
    assert rdc.merge_ledger(new, old) == new


def test_ledger_within_one_day_keeps_the_higher_spend_and_every_upload():
    """Over-counting quota only makes the agent more careful; under-counting
    gets it rate-limited mid-day."""
    a = {"pacific_date": "2026-08-09", "units_used": 3200, "uploads_recorded": ["v1", "v2"]}
    b = {"pacific_date": "2026-08-09", "units_used": 1700, "uploads_recorded": ["v1", "v3"]}
    merged = rdc.merge_ledger(a, b)
    assert merged["units_used"] == 3200
    assert merged["uploads_recorded"] == ["v1", "v2", "v3"]


def test_ledger_does_not_sum_the_two_sides():
    """Both sides include the history they branched from, so adding them
    would double-count every call made before the split."""
    a = {"pacific_date": "2026-08-09", "units_used": 3200, "uploads_recorded": []}
    b = {"pacific_date": "2026-08-09", "units_used": 3300, "uploads_recorded": []}
    assert rdc.merge_ledger(a, b)["units_used"] == 3300


def test_topics_keeps_both_sides():
    """Dropping either side would let a published topic be repeated."""
    a = [{"title": "A", "topic_subject": "Tunguska event"}]
    b = [{"title": "B", "topic_subject": "The Bloop"}]
    merged = rdc.merge_topics(a, b)
    assert [e["title"] for e in merged] == ["A", "B"]


def test_topics_does_not_duplicate_shared_entries():
    shared = {"title": "A", "topic_subject": "Tunguska event"}
    merged = rdc.merge_topics([shared, {"title": "B"}], [shared, {"title": "C"}])
    assert [e["title"] for e in merged] == ["A", "B", "C"]


def _record(measured_at, views, history):
    return {
        "video_id": "vid1",
        "latest_measurement": {"measured_at": measured_at, "actual_views": views},
        "measurement_history": [{"measured_at": m, "actual_views": v} for m, v in history],
        "measurement": {"measured_at": None},
    }


def test_video_record_keeps_the_newer_reading():
    older = _record("2026-08-11T06:00:00Z", 100, [("2026-08-11T06:00:00Z", 100)])
    newer = _record("2026-08-11T09:00:00Z", 180, [("2026-08-11T09:00:00Z", 180)])
    for a, b in ((older, newer), (newer, older)):
        merged = rdc.merge_video_record(a, b)
        assert merged["latest_measurement"]["actual_views"] == 180


def test_video_record_loses_no_reading_from_either_side():
    a = _record("2026-08-11T06:00:00Z", 100,
                [("2026-08-11T03:00:00Z", 60), ("2026-08-11T06:00:00Z", 100)])
    b = _record("2026-08-11T09:00:00Z", 180,
                [("2026-08-11T03:00:00Z", 60), ("2026-08-11T09:00:00Z", 180)])
    merged = rdc.merge_video_record(a, b)
    stamps = [e["measured_at"] for e in merged["measurement_history"]]
    assert stamps == ["2026-08-11T03:00:00Z", "2026-08-11T06:00:00Z", "2026-08-11T09:00:00Z"]


def test_the_frozen_first_reading_is_never_dropped():
    """`measurement` is written once and is what predict.py trains on."""
    frozen = {"measured_at": "2026-08-06T12:00:00Z", "actual_views": 42}
    a = _record("2026-08-11T09:00:00Z", 180, [])
    b = _record("2026-08-11T06:00:00Z", 100, [])
    b["measurement"] = frozen
    assert rdc.merge_video_record(a, b)["measurement"] == frozen
    assert rdc.merge_video_record(b, a)["measurement"] == frozen


def test_split_sides_handles_every_hunk_not_just_the_first():
    """A record conflicts in two places at once — latest_measurement near the
    top and measurement_history further down. Rebuilding only the first hunk
    leaves the later markers in place and the result is not valid JSON, which
    is what stopped the resolver on its first real run."""
    text = (
        "{\n"
        + conflicted('  "views": 100,', '  "views": 180,')
        + '  "middle": true,\n'
        + conflicted('  "history": [1]', '  "history": [1, 2]')
        + "}\n"
    )
    ours, theirs = rdc.split_sides(text)
    assert json.loads(ours) == {"views": 100, "middle": True, "history": [1]}
    assert json.loads(theirs) == {"views": 180, "middle": True, "history": [1, 2]}
