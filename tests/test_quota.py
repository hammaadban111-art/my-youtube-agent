"""Tests for the self-tracked YouTube Data API quota ledger.

Google's API exposes no real-time quota-remaining anywhere, so agent/quota.py
tracks our own spend against a persisted, Pacific-day-boundary ledger. These
tests point LEDGER_PATH at a temp file so they never touch the real one.
"""
from datetime import datetime

from agent import quota


def _use_tmp_ledger(monkeypatch, tmp_path):
    monkeypatch.setattr(quota, "LEDGER_PATH", str(tmp_path / "quota_ledger.json"))


def test_starts_at_zero(monkeypatch, tmp_path):
    _use_tmp_ledger(monkeypatch, tmp_path)
    assert quota.units_used_today() == 0


def test_record_units_accumulates(monkeypatch, tmp_path):
    _use_tmp_ledger(monkeypatch, tmp_path)
    quota.record_units(2)
    quota.record_units(1)
    assert quota.units_used_today() == 3


def test_resets_on_new_pacific_day(monkeypatch, tmp_path):
    _use_tmp_ledger(monkeypatch, tmp_path)
    quota.record_units(500)
    assert quota.units_used_today() == 500

    # Simulate the ledger being read on a later Pacific day without going
    # through record_units - _pacific_today() is what decides the reset.
    monkeypatch.setattr(quota, "_pacific_today", lambda: "2099-01-01")
    assert quota.units_used_today() == 0


def test_has_headroom_true_when_low(monkeypatch, tmp_path):
    _use_tmp_ledger(monkeypatch, tmp_path)
    quota.record_units(100)
    assert quota.has_headroom() is True


def test_has_headroom_false_past_threshold(monkeypatch, tmp_path):
    _use_tmp_ledger(monkeypatch, tmp_path)
    quota.record_units(7001)  # just over the default 70% of 10,000
    assert quota.has_headroom() is False


def test_has_headroom_respects_custom_fraction(monkeypatch, tmp_path):
    _use_tmp_ledger(monkeypatch, tmp_path)
    quota.record_units(5001)
    assert quota.has_headroom(reserve_fraction=0.5) is False
    assert quota.has_headroom(reserve_fraction=0.9) is True


def test_upload_takes_a_slot_but_zero_units(monkeypatch, tmp_path):
    _use_tmp_ledger(monkeypatch, tmp_path)
    assert quota.record_upload("vid1") is True
    assert quota.uploads_today() == 1
    assert quota.units_used_today() == 0


def test_the_same_upload_is_never_counted_twice(monkeypatch, tmp_path):
    """Three callers book the same upload - the pipeline, the reconciliation
    sweep in the dashboard, and the next dashboard rebuild. A ledger that grew
    by 1,600 each time would refuse work that is actually affordable."""
    _use_tmp_ledger(monkeypatch, tmp_path)
    quota.record_upload("vid1")
    assert quota.record_upload("vid1") is False
    quota.record_upload("vid1")
    assert quota.uploads_today() == 1


def test_reconcile_books_only_todays_uploads(monkeypatch, tmp_path):
    """The ledger resets at midnight PACIFIC, so a video uploaded at 04:26Z
    was charged to the previous Pacific day and must not be re-booked today.
    This is the real off-by-one: both timestamps below are the same UTC date."""
    _use_tmp_ledger(monkeypatch, tmp_path)
    monkeypatch.setattr(quota, "_pacific_today", lambda: "2026-08-05")
    added = quota.reconcile_uploads([
        {"video_id": "yesterday", "uploaded_at": "2026-08-05T04:26:21Z"},  # 21:26 PT on 08-04
        {"video_id": "today", "uploaded_at": "2026-08-05T08:56:36Z"},      # 01:56 PT on 08-05
    ])
    assert added == 1
    assert quota.uploads_today() == 1


def test_reconcile_is_idempotent_across_runs(monkeypatch, tmp_path):
    _use_tmp_ledger(monkeypatch, tmp_path)
    monkeypatch.setattr(quota, "_pacific_today", lambda: "2026-08-05")
    records = [{"video_id": "today", "uploaded_at": "2026-08-05T08:56:36Z"}]
    quota.reconcile_uploads(records)
    assert quota.reconcile_uploads(records) == 0
    assert quota.uploads_today() == 1


def test_reconcile_survives_records_missing_fields(monkeypatch, tmp_path):
    _use_tmp_ledger(monkeypatch, tmp_path)
    monkeypatch.setattr(quota, "_pacific_today", lambda: "2026-08-05")
    assert quota.reconcile_uploads([
        {}, {"video_id": "x"}, {"uploaded_at": "2026-08-05T08:56:36Z"},
        {"video_id": "y", "uploaded_at": "not a timestamp"},
    ]) == 0


def test_headroom_for_protects_the_reserve(monkeypatch, tmp_path):
    """A re-upload is discretionary spend, so it has to leave the 30% buffer
    the rest of the day's scheduled reads live on."""
    _use_tmp_ledger(monkeypatch, tmp_path)
    cost = quota.UNITS_PER_DELETE
    quota.record_units(6950)  # 6950 + 50 == exactly the 7,000 reserve line
    assert quota.has_headroom_for(cost) is True
    quota.record_units(1)
    assert quota.has_headroom_for(cost) is False


def test_fits_in_cap_ignores_the_reserve(monkeypatch, tmp_path):
    """The scheduled reads answer to the hard cap instead: refusing it at 70%
    would halt the channel over a comfort threshold."""
    _use_tmp_ledger(monkeypatch, tmp_path)
    quota.record_units(9000)
    assert quota.has_headroom_for(quota.UNITS_PER_DELETE) is False
    assert quota.fits_in_cap(quota.UNITS_PER_DELETE) is True
    quota.record_units(951)  # 9951 + 50 > 10000
    assert quota.fits_in_cap(quota.UNITS_PER_DELETE) is False

def test_99_uploads_permits_one_more(monkeypatch, tmp_path):
    _use_tmp_ledger(monkeypatch, tmp_path)
    for i in range(99):
        quota.record_upload(f"vid{i}")
    assert quota.can_upload() is True
    quota.record_upload("vid99")
    assert quota.can_upload() is False


def test_remaining_never_goes_negative(monkeypatch, tmp_path):
    _use_tmp_ledger(monkeypatch, tmp_path)
    quota.record_units(12_000)
    assert quota.remaining_units() == 0
