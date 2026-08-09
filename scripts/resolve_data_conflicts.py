#!/usr/bin/env python3
"""
Resolves rebase conflicts in the two committed-back data files.

Both workflows commit `data/` and `history/` after every run, and two runs
that overlap will conflict on the same two files. Git cannot merge either of
them, so `git pull --rebase` stops mid-rebase and the whole step exits 1 -
AFTER the video is already live on YouTube. That is how three videos ended up
published with no record: the upload succeeded, the commit did not, and the
runner was wiped.

Neither file is source code and neither needs a human:

  data/quota_ledger.json - one Pacific day's spend. Two sides that disagree on
    the date mean one of them is yesterday's, and the newer date wins outright.
    Same date means both counted real calls, so the higher spend and the union
    of booked uploads win: over-counting quota is safe (it only makes the agent
    more conservative), under-counting is not.

  history/topics.json - an append-only list. Both sides appended, so the union
    in order is exactly right; dropping either side would let a published topic
    be repeated.

Usage (from a mid-rebase working tree):
  python scripts/resolve_data_conflicts.py
"""
import json
import re
import subprocess
import sys
from pathlib import Path

LEDGER = "data/quota_ledger.json"
TOPICS = "history/topics.json"
CONFLICT = re.compile(r"<<<<<<<[^\n]*\n(?P<ours>.*?)\n=======\n(?P<theirs>.*?)\n>>>>>>>[^\n]*\n",
                      re.S)


def split_sides(text: str) -> tuple[str, str] | None:
    """The two full-document versions of a conflicted file, or None if clean."""
    m = CONFLICT.search(text)
    if not m:
        return None
    head = text[:m.start()]
    tail = text[m.end():]
    return head + m.group("ours") + "\n" + tail, head + m.group("theirs") + "\n" + tail


def merge_ledger(ours: dict, theirs: dict) -> dict:
    if ours.get("pacific_date") != theirs.get("pacific_date"):
        # A stale day carries no information about today's remaining quota.
        return max(ours, theirs, key=lambda d: d.get("pacific_date", ""))
    booked, seen = [], set()
    for vid in ours.get("uploads_recorded", []) + theirs.get("uploads_recorded", []):
        if vid not in seen:
            seen.add(vid)
            booked.append(vid)
    return {
        "pacific_date": ours.get("pacific_date"),
        # Not a sum: both sides already include the shared history they
        # branched from, so adding them would double-count it.
        "units_used": max(ours.get("units_used", 0), theirs.get("units_used", 0)),
        "uploads_recorded": booked,
    }


def merge_topics(ours: list, theirs: list) -> list:
    merged, seen = [], set()
    for entry in ours + theirs:
        key = (entry.get("title"), entry.get("topic_subject"))
        if key not in seen:
            seen.add(key)
            merged.append(entry)
    return merged


def resolve(path: str, merge) -> bool:
    p = Path(path)
    if not p.exists():
        return False
    sides = split_sides(p.read_text())
    if sides is None:
        return False
    ours, theirs = (json.loads(s) for s in sides)
    p.write_text(json.dumps(merge(ours, theirs), indent=2) + "\n")
    subprocess.run(["git", "add", path], check=True)
    print(f"  resolved {path}")
    return True


def main() -> int:
    resolved = [resolve(LEDGER, merge_ledger), resolve(TOPICS, merge_topics)]
    if not any(resolved):
        print("No conflicts in the data files.")
        return 0
    print("Resolved. Continue with: git rebase --continue")
    return 0


if __name__ == "__main__":
    sys.exit(main())
