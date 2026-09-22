#!/usr/bin/env python3
"""
Posts the weekly maintenance report on the run's summary page.

Kept out of weekly_health_check.py so the check can be run locally, and its
report read on the terminal, without any Actions environment.

Each finding that needs a human becomes an ::error:: annotation, and the exit
status is non-zero when there is at least one. That red run is the whole
notification: GitHub emails the owner about it and links straight to this
summary. An all-clear week exits 0 and stays silent.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent import gha  # noqa: E402


def _write_summary(markdown: str) -> None:
    path = os.getenv("GITHUB_STEP_SUMMARY", "").strip()
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        f.write(markdown)


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else "weekly-report.txt"
    try:
        with open(path, encoding="utf-8") as f:
            body = f.read().strip()
    except OSError as e:
        # The check itself produced nothing readable. Silence from this job is
        # indistinguishable from the job having stopped running, so this fails.
        message = f"The weekly health check produced no report ({type(e).__name__}: {e})."
        _write_summary(f"## Weekly maintenance\n\n{message}\n")
        gha.error("Weekly maintenance: no report produced", message)
        return 1

    problems = [ln[len("PROBLEM:"):].strip() for ln in body.splitlines()
                if ln.startswith("PROBLEM:")]
    verdict = (f"{len(problems)} item(s) need attention" if problems
               else "all clear")
    _write_summary(f"## Weekly maintenance: {verdict}\n\n```text\n{body}\n```\n")
    print(body)
    for problem in problems:
        gha.error("Weekly maintenance", problem)
    print(f"[weekly] {verdict}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
