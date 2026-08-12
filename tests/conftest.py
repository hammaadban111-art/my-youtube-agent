import sys
from pathlib import Path

import pytest

# Add the repo root directory to sys.path so 'from agent import ...' works cleanly
repo_root = Path(__file__).resolve().parent.parent
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))


@pytest.fixture(autouse=True)
def _isolate_quota_ledger(tmp_path, monkeypatch):
    """Never let a test write the REAL data/quota_ledger.json.

    tests/test_quota.py always pointed LEDGER_PATH at a temp file, but any
    OTHER test that reaches a code path calling quota.record_units() or
    quota.record_upload() - the upload-failure tests do - wrote straight into
    the committed ledger. On 2026-08-12 a test run appended nine synthetic
    upload ids to the real file and had to be reverted by hand. Autouse, so a
    test cannot forget: isolation is the default, not something each new test
    has to remember to opt into."""
    from agent import quota
    monkeypatch.setattr(quota, "LEDGER_PATH", str(tmp_path / "quota_ledger.json"))
