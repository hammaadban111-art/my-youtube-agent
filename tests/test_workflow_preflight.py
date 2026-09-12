"""The daily workflow's shape, asserted rather than trusted.

Two things here are easy to break by accident and expensive to discover at
06:07 UTC:

1. The packet pre-flight runs BEFORE apt-get and pip. That only works while the
   pre-flight's import graph is standard-library only. One `import requests` in
   agent/packet.py or anything it pulls in would move the failure from this test
   to a live scheduled run.

2. The recovery half of the pipeline is not gated on a story being due. A video
   uploaded but never recorded belongs to a story that is no longer selectable,
   so `due` is false precisely when recovery matters.
"""
import ast
import os
import re
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DAILY = os.path.join(REPO, ".github", "workflows", "daily.yml")


@pytest.fixture(scope="module")
def workflow():
    with open(DAILY) as f:
        return f.read()


def _step_index(text, name):
    match = re.search(rf"^\s*- name: {re.escape(name)}\s*$", text, re.M)
    assert match, f"step {name!r} not found in daily.yml"
    return match.start()


# ------------------------------------------------------------- step ordering

def test_the_packet_preflight_runs_before_apt_and_pip(workflow):
    """A run with nothing to publish should cost seconds, not the ~1-2 minutes
    of ffmpeg/ImageMagick and PyPI installs it used to pay first."""
    preflight = _step_index(workflow, "Validate the weekly story packet")
    assert preflight < _step_index(workflow, "Install ffmpeg")
    assert preflight < _step_index(workflow, "Install Python dependencies")


def test_the_expensive_installs_are_conditional(workflow):
    """Unconditional installs would make the reordering pointless."""
    for step in ("Install ffmpeg", "Install Python dependencies"):
        chunk = workflow[_step_index(workflow, step):][:400]
        assert "steps.packet.outputs.due == 'true'" in chunk
        assert "steps.recovery.outputs.pending == 'true'" in chunk


def test_pip_is_cached(workflow):
    assert "cache: pip" in workflow


def test_the_recovery_probe_runs_after_the_caches_it_inspects(workflow):
    """It reads workdir/checkpoint and workdir/pending_upload, both of which
    arrive from the Actions cache. Asking before the restore always says no."""
    probe = _step_index(workflow, "Check for unfinished work from a previous run")
    assert probe > _step_index(workflow, "Restore pipeline checkpoint")
    assert probe > _step_index(workflow, "Restore parked upload recovery bundle")
    assert probe < _step_index(workflow, "Install ffmpeg")


def test_the_record_is_committed_on_a_recovery_run_too(workflow):
    """Writing the missing record IS the recovery. Gating the commit on a due
    story would finish the work and then throw the result away."""
    chunk = workflow[_step_index(workflow, "Persist topic history and video record"):][:600]
    assert "steps.recovery.outputs.pending == 'true'" in chunk


def test_the_cron_fallback_is_still_present(workflow):
    """docs/scheduling.md promises the crons keep running the channel if the
    external dispatch trigger is never configured or silently stops."""
    for cron in ('cron: "7 1 * * *"', 'cron: "7 6 * * *"',
                 'cron: "7 11 * * *"', 'cron: "7 16 * * *"'):
        assert cron in workflow


def test_the_dispatch_trigger_is_wired(workflow):
    assert "repository_dispatch:" in workflow
    assert "publish-slot" in workflow


def test_writers_share_one_concurrency_group(workflow):
    """Two writers rewriting every video record at once is what broke the
    2026-08-09/10/11 runs."""
    assert "group: repo-data-writers" in workflow
    assert "cancel-in-progress: false" in workflow


# --------------------------------------------------- the stdlib-only promise

# Everything the pre-flight imports, transitively, inside this package.
PREFLIGHT_MODULES = ("packet", "cadence", "config", "history",
                     "script_writer", "store", "notify", "checkpoint")

THIRD_PARTY = {"requests", "moviepy", "PIL", "pydub", "google", "googleapiclient",
               "google_auth_oauthlib", "edge_tts", "numpy", "yaml"}


def _imported_roots(path):
    with open(path) as f:
        tree = ast.parse(f.read(), filename=path)
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            # Relative imports (level > 0) stay inside this package.
            if node.level == 0 and node.module:
                roots.add(node.module.split(".")[0])
    return roots


@pytest.mark.parametrize("module", PREFLIGHT_MODULES)
def test_preflight_modules_import_no_third_party_package(module):
    """If this fails, move the pre-flight back after `pip install` in daily.yml
    in the same change — or the 06:07 run dies on ModuleNotFoundError."""
    path = os.path.join(REPO, "agent", f"{module}.py")
    offenders = _imported_roots(path) & THIRD_PARTY
    assert not offenders, (
        f"agent/{module}.py imports {sorted(offenders)}, which pip installs. "
        "daily.yml runs the packet pre-flight BEFORE pip install.")


def test_the_preflight_actually_runs_without_site_packages():
    """The end-to-end version of the check above: import the pre-flight with
    -S, so nothing from site-packages is importable at all."""
    result = subprocess.run(
        [sys.executable, "-S", "-c",
         "import sys; sys.path.insert(0, '.'); "
         "import agent.packet, agent.checkpoint; print('ok')"],
        cwd=REPO, capture_output=True, text=True,
        env={**os.environ, "NICHE": "unsolved mysteries and bizarre history"},
    )
    assert result.returncode == 0, (
        f"pre-flight import failed without site-packages:\n{result.stderr}")
    assert "ok" in result.stdout


# ------------------------------------------------------- billed-minute guards

# August 2026 billed 2,449 Actions minutes against a 2,000-minute free
# allowance on a private repository, and the account ran out. These assert the
# three changes that bought the headroom back, so a later edit that undoes one
# fails here instead of showing up on next month's bill.

def test_the_pexels_cache_is_not_re_uploaded_every_run(workflow):
    """actions/cache@v4 keyed on run_id can never hit its exact key, so it
    re-uploaded the whole footage pile on EVERY run: 30s measured, ~45 billed
    minutes a month to save what was already there. Restore and save are split
    so the save can be conditional."""
    assert "actions/cache/restore@v4" in workflow
    chunk = workflow[_step_index(workflow, "Restore Pexels footage cache"):][:400]
    assert "actions/cache@v4" not in chunk, (
        "the combined cache action saves on every run; use cache/restore plus a "
        "conditional cache/save")


def test_the_pexels_cache_is_still_saved_somewhere(workflow):
    """Splitting restore from save is only safe if something still saves. A
    cache that is never written rots, and every run re-downloads footage from
    Pexels — slower and more fragile than the thing it replaced."""
    chunk = workflow[_step_index(workflow, "Persist Pexels footage cache (once a day)"):][:600]
    assert "actions/cache/save@v4" in chunk
    # The 01:07Z cron, or any non-schedule run, so a day of dispatch-only runs
    # still banks what it downloaded.
    assert "github.event_name != 'schedule'" in chunk
    assert "7 1 " in chunk


def test_apt_skips_recommended_packages(workflow):
    """ffmpeg and imagemagick recommend docs, fonts and codecs this pipeline
    never touches. The packages themselves are unchanged, so rendering is
    unaffected."""
    chunk = workflow[_step_index(workflow, "Install ffmpeg"):][:400]
    assert "--no-install-recommends" in chunk
    assert "ffmpeg" in chunk and "imagemagick" in chunk


def test_followup_runs_every_four_hours_not_three():
    """GitHub bills each run rounded UP to a whole minute and a normal
    follow-up pass takes 28 seconds, so the run COUNT is the cost. main.py
    still sweeps after every upload, so measurement passes go 12/day -> 10/day,
    not 8 -> 6."""
    with open(os.path.join(REPO, ".github", "workflows", "followup.yml")) as f:
        followup = f.read()
    assert 'cron: "15 */4 * * *"' in followup
    assert 'cron: "15 */3 * * *"' not in followup
