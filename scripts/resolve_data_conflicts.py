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
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from agent import quota  # noqa: E402 - needs the path above to import

LEDGER = "data/quota_ledger.json"
TOPICS = "history/topics.json"
CONFLICT = re.compile(r"<<<<<<<[^\n]*\n(?P<ours>.*?)\n=======\n(?P<theirs>.*?)\n>>>>>>>[^\n]*\n",
                      re.S)


def split_sides(text: str) -> tuple[str, str] | None:
    """The two full-document versions of a conflicted file, or None if clean.

    Substitutes EVERY hunk, not just the first. One record routinely conflicts
    in two places at once - `latest_measurement` near the top and
    `measurement_history` further down - and rebuilding only the first hunk
    leaves the later `<<<<<<<` markers in place, so the result is not valid
    JSON and the file cannot be resolved at all."""
    if "<<<<<<<" not in text:
        return None
    ours = CONFLICT.sub(lambda m: m.group("ours") + "\n", text)
    theirs = CONFLICT.sub(lambda m: m.group("theirs") + "\n", text)
    return ours, theirs


def merge_ledger(ours: dict, theirs: dict) -> dict:
    if ours.get("pacific_date") != theirs.get("pacific_date"):
        # A stale day carries no information about today's remaining quota.
        return max(ours, theirs, key=lambda d: d.get("pacific_date", ""))

    # max() alone silently LOSES an upload. Both sides branched from a shared
    # base, so each one's units_used is that base plus its own spend, and the
    # higher of the two keeps only one run's 1,600-unit insert. Two overlapping
    # runs that each published therefore merge to 1,600 units short, and the
    # loss is permanent: reconcile_uploads() re-books only uploads MISSING from
    # uploads_recorded, and the union below has already listed both ids.
    #
    # So: take the higher side, then add the insert cost of every upload only
    # the other side saw. Over-counting is safe here (it makes the agent more
    # conservative); under-counting is what runs the day into the cap.
    high, low = sorted((ours, theirs), key=lambda d: d.get("units_used", 0),
                       reverse=True)
    unseen_by_high = (set(low.get("uploads_recorded", []))
                      - set(high.get("uploads_recorded", [])))
    total_units = (high.get("units_used", 0)
                   + len(unseen_by_high) * quota.UNITS_PER_UPLOAD)

    booked, seen = [], set()
    for vid in ours.get("uploads_recorded", []) + theirs.get("uploads_recorded", []):
        if vid not in seen:
            seen.add(vid)
            booked.append(vid)
    return {
        "pacific_date": ours.get("pacific_date"),
        # Not a plain sum: both sides already include the shared history they
        # branched from, so adding them outright would double-count it. See
        # the note above for why it is not a plain max() either.
        "units_used": total_units,
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


def _measured_at(record: dict) -> str:
    return (record.get("latest_measurement") or {}).get("measured_at") or ""


def merge_video_record(ours: dict, theirs: dict) -> dict:
    """Merges two versions of one video's record.

    These conflict far more often than the ledger does, and that was the gap
    that kept the 2026-08-09/10/11 runs failing: EVERY run re-measures EVERY
    tracked video (followup.sweep), so two overlapping runs rewrite all 40+
    record files and git reports 147 conflicted paths at once. Resolving only
    the ledger left the rest unmerged, the rebase still could not continue,
    and the push was rejected as non-fast-forward.

    The newer reading wins for the current numbers, and the reading history is
    unioned so neither run's measurement is dropped."""
    newer, older = sorted((ours, theirs), key=_measured_at, reverse=True)
    merged = dict(newer)

    history, seen = [], set()
    for entry in (older.get("measurement_history") or []) + (newer.get("measurement_history") or []):
        stamp = entry.get("measured_at")
        if stamp and stamp not in seen:
            seen.add(stamp)
            history.append(entry)
    merged["measurement_history"] = sorted(history, key=lambda e: e.get("measured_at") or "")

    # The frozen first reading is what predict.py trains on. Whichever side
    # actually has it wins - it is written once and must never be lost.
    for side in (ours, theirs):
        if (side.get("measurement") or {}).get("measured_at"):
            merged["measurement"] = side["measurement"]
            break
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
    return True


def conflicted_paths() -> list[str]:
    out = subprocess.run(["git", "diff", "--name-only", "--diff-filter=U"],
                         capture_output=True, text=True).stdout
    return [line for line in out.splitlines() if line.strip()]


def main() -> int:
    paths = conflicted_paths()
    if not paths:
        print("No conflicted files.")
        return 0

    counts, unresolved = {"ledger": 0, "topics": 0, "records": 0}, []
    for path in paths:
        if path == LEDGER and resolve(path, merge_ledger):
            counts["ledger"] += 1
        elif path == TOPICS and resolve(path, merge_topics):
            counts["topics"] += 1
        elif path.startswith("data/videos/") and path.endswith(".json") \
                and resolve(path, merge_video_record):
            counts["records"] += 1
        else:
            unresolved.append(path)

    print(f"Resolved: {counts['records']} video record(s), "
          f"{counts['ledger']} ledger, {counts['topics']} topic history.")
    if unresolved:
        # Never pretend to have fixed something outside these three shapes -
        # a conflict in source code is a real conflict and needs a human.
        print(f"NOT resolved ({len(unresolved)}), needs a human: "
              + ", ".join(unresolved[:10]))
        return 1
    print("Continue with: git rebase --continue")
    return 0


if __name__ == "__main__":
    sys.exit(main())
