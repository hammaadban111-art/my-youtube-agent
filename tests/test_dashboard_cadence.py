"""The public dashboard is deliberately a weekly artifact.

Per-video workflows still commit source records and measurements, but only
weekly maintenance is allowed to rebuild/publish the public dashboard. These
tests protect that cost and data-integrity boundary from accidental workflow
edits.
"""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


def _workflow(name: str) -> str:
    return (WORKFLOWS / name).read_text(encoding="utf-8")


def test_weekly_is_the_only_dashboard_publisher():
    for name in ("daily.yml", "followup.yml", "reupload.yml"):
        text = _workflow(name)
        assert "run: bash scripts/publish_dashboard.sh" not in text
        assert "- name: Publish dashboard" not in text

    weekly = _workflow("weekly.yml")
    assert weekly.count("run: bash scripts/publish_dashboard.sh --only-if-changed") == 1
    assert "--only-if-changed" in weekly


def test_per_video_writers_do_not_commit_generated_dashboard_files():
    assert "git add history/topics.json data public content" not in _workflow("daily.yml")
    assert "git add data public" not in _workflow("followup.yml")
    assert "git add data history public" not in _workflow("reupload.yml")


def test_all_data_writers_queue_instead_of_cancelling_each_other():
    for name in ("daily.yml", "followup.yml", "reupload.yml", "weekly.yml"):
        text = _workflow(name)
        assert "group: repo-data-writers" in text
        assert "cancel-in-progress: false" in text


def test_non_weekly_entry_points_do_not_build_dashboard():
    for relative in (
        "agent/main.py",
        "agent/followup.py",
        "scripts/reupload_video.py",
        "scripts/publish_parked.py",
    ):
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert "dashboard.build()" not in text

    weekly = (ROOT / "scripts" / "weekly_health_check.py").read_text(encoding="utf-8")
    assert "dashboard.build()" in weekly
