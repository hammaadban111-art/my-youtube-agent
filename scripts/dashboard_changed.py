#!/usr/bin/env python3
"""
Answers "would publishing this dashboard change anything a reader would see?"

Exit status 0 means yes (publish), 1 means no (skip). Written as a status code
so publish_dashboard.sh can branch on it without parsing output.

WHY THIS IS NOT JUST `git diff`

agent/dashboard.py stamps every build with `generated_at` (now) and `next_runs`
(the next few cron times), because the page shows "data fresh 2h ago" and "next
run at ...". Both change on every single build even when not one figure moved,
so a byte comparison always says "changed" and the publish always commits.

For the four scheduled runs that is correct — refreshing that stamp IS the job.
For the weekly maintenance run it is not: a week where nothing broke and no
video moved should produce no commit, no Pages rebuild and no deployment. So
the comparison here ignores exactly those two fields and nothing else.

Deliberately fails OPEN. Any surprise — a missing file, malformed JSON, an
unreadable directory — exits 0 and lets the publish proceed. A needless commit
costs a Pages rebuild; a wrongly suppressed one leaves the public site stale
and gives no sign that it did.
"""
import json
import os
import sys

# The two fields that change on every build by design. Everything else in
# data.json is a real figure about a real video.
VOLATILE_TOP_LEVEL_KEYS = ("generated_at", "next_runs")


def _read_text(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def _substantive(raw: str | None):
    """data.json with the freshness stamp removed, or None if it cannot be
    parsed (which counts as "changed", not as "equal")."""
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return {k: v for k, v in data.items() if k not in VOLATILE_TOP_LEVEL_KEYS}


def has_real_changes(published_dir: str, built_dir: str) -> bool:
    # index.html is a static shell (see agent/dashboard.py) — it changes only
    # when the template itself does, so it is compared byte for byte.
    published_html = _read_text(os.path.join(published_dir, "index.html"))
    built_html = _read_text(os.path.join(built_dir, "index.html"))
    if built_html is None:
        return True  # nothing to compare against; let the publish decide
    if published_html != built_html:
        return True

    published_data = _substantive(_read_text(os.path.join(published_dir, "data.json")))
    built_data = _substantive(_read_text(os.path.join(built_dir, "data.json")))
    if published_data is None or built_data is None:
        return True
    return published_data != built_data


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("usage: dashboard_changed.py <published-dir> <built-dir>", file=sys.stderr)
        sys.exit(0)  # fail open
    try:
        changed = has_real_changes(sys.argv[1], sys.argv[2])
    except Exception as e:  # noqa: BLE001 - fail open, never block a publish
        print(f"[dashboard-changed] {type(e).__name__}: {e} — assuming changed",
              file=sys.stderr)
        sys.exit(0)
    sys.exit(0 if changed else 1)
