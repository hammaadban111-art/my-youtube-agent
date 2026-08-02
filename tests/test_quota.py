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
