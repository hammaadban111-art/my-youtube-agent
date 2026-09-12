#!/usr/bin/env python3
"""
Emails the weekly maintenance report.

Kept out of weekly_health_check.py so the check can be run locally, and its
report read on the terminal, without a mail secret in the environment.

The subject line carries the verdict, because that is the only part read on a
phone: "all clear" or the number of things that need a human. notify.alert()
never raises, but a false return is still a delivery failure and must make the
workflow visibly fail: silent maintenance mail is indistinguishable from no
maintenance at all.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent import notify  # noqa: E402


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else "weekly-report.txt"
    try:
        with open(path, encoding="utf-8") as f:
            body = f.read().strip()
    except OSError as e:
        # The check itself produced nothing readable. That is worth an email in
        # its own right: silence from this job is indistinguishable from the
        # job having stopped running.
        body = f"The weekly health check produced no report ({type(e).__name__}: {e})."
        sent = notify.alert("Weekly maintenance: no report produced", body)
        print(f"[weekly] {body}")
        return 0 if sent else 1

    problems = [ln for ln in body.splitlines() if ln.startswith("PROBLEM:")]
    subject = (f"Weekly maintenance: {len(problems)} item(s) need attention"
               if problems else "Weekly maintenance: all clear")

    run_url = os.getenv("RUN_URL", "").strip()
    if run_url:
        body += f"\n\nFull run: {run_url}"

    sent = notify.alert(subject, body)
    print(f"[weekly] reported: {subject}")
    if not sent:
        print("[weekly] ERROR: report email was not sent", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
