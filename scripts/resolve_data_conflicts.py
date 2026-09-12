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
    Same date means both counted real calls, and each writer books under its
    own key (agent/quota.py), so the merge is a per-key max and the day's total
    is their sum - neither side's independent spend is discarded. A pre-writer
    ledger carries only a scalar and falls back to the old max(): over-counting
    quota is safe (it only makes the agent more conservative), under-counting
    is not.

  data/niche_scan.json - a whole-file snapshot of one weekly scan. The newer
    scanned_at wins outright; field-by-field merging would invent a sample no
    run actually observed.

  history/topics.json - an append-only list. Both sides appended, so the union
    in order is exactly right; dropping either side would let a published topic
    be repeated.

  content/story_history.json - the weekly packet's status ledger, a dict keyed
    by story id. Two runs touch different stories, so the union of the keys is
    right; where both touched the SAME story the further-along status wins
    (published beats queued beats proposed) and the two per-story history lists
    are unioned by timestamp. Losing a "published" here would let the next
    weekly packet re-propose a story that is already on the channel.

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
STORY_LEDGER = "content/story_history.json"
NICHE_SCAN = "data/niche_scan.json"

# How far along a story is. A merge must never move one backwards: a run that
# published is authoritative over one that only claimed.
STATUS_RANK = {"proposed": 0, "queued": 1, "skipped": 2, "failed": 3, "published": 4}
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


def _merge_counters(ours: dict, theirs: dict) -> dict:
    """Per-writer counters merged by taking each key's higher value.

    Exact, because a writer only ever increments its OWN key: two sides can
    disagree about a key only when one of them branched before the last
    increment, and the higher value is the later one. Order-independent and
    idempotent, so re-running the resolver changes nothing."""
    merged = {}
    for side in (ours or {}, theirs or {}):
        if not isinstance(side, dict):
            continue
        for key, count in side.items():
            if isinstance(count, bool) or not isinstance(count, (int, float)):
                continue
            merged[str(key)] = max(merged.get(str(key), 0), int(count))
    return merged


def merge_ledger(ours: dict, theirs: dict) -> dict:
    if ours.get("pacific_date") != theirs.get("pacific_date"):
        # A stale day carries no information about today's remaining quota.
        return max(ours, theirs, key=lambda d: d.get("pacific_date", ""))

    # THE SPEND. A plain max() loses every unit the smaller side spent
    # independently since the split, and a plain sum double-counts the history
    # both sides inherited. Neither is right without knowing the merge base.
    #
    # agent/quota.py therefore books units under a PER-WRITER key, which makes
    # the merge exact: take each key's higher value and sum. A ledger written
    # before that change carries only the scalar, and for those the old
    # conservative max() still applies — it is the only defensible answer when
    # the two sides cannot be told apart.
    units_by_writer = _merge_counters(ours.get("units_by_writer"),
                                      theirs.get("units_by_writer"))
    scalar_units = max(ours.get("units_used", 0) or 0,
                       theirs.get("units_used", 0) or 0)
    total_units = max(sum(units_by_writer.values()), scalar_units)

    # Failed insert attempts consume an upload SLOT and were dropped entirely
    # by the previous merge, so two overlapping runs that each burned one came
    # out of a conflict having burned none.
    failed_by_writer = _merge_counters(ours.get("failed_by_writer"),
                                       theirs.get("failed_by_writer"))
    scalar_failed = max(ours.get("failed_upload_attempts", 0) or 0,
                        theirs.get("failed_upload_attempts", 0) or 0)
    total_failed = max(sum(failed_by_writer.values()), scalar_failed)

    booked, seen = [], set()
    for vid in ours.get("uploads_recorded", []) + theirs.get("uploads_recorded", []):
        if vid not in seen:
            seen.add(vid)
            booked.append(vid)
    merged = {
        "pacific_date": ours.get("pacific_date"),
        "units_used": total_units,
        "uploads_recorded": booked,
        "failed_upload_attempts": total_failed,
    }
    if units_by_writer:
        merged["units_by_writer"] = units_by_writer
    if failed_by_writer:
        merged["failed_by_writer"] = failed_by_writer
    return merged


def merge_niche_scan(ours: dict, theirs: dict) -> dict:
    """data/niche_scan.json is a whole-file snapshot, so the newer scan wins
    outright. Merging two scans field by field would invent a sample that
    neither run actually observed."""
    return max(ours, theirs, key=lambda d: str(d.get("scanned_at") or ""))


def merge_topics(ours: list, theirs: list) -> list:
    merged, seen = [], set()
    for entry in ours + theirs:
        key = (entry.get("title"), entry.get("topic_subject"))
        if key not in seen:
            seen.add(key)
            merged.append(entry)
    return merged


def merge_story_entry(ours: dict, theirs: dict) -> dict:
    """One story's ledger entry, from two sides that both wrote it."""
    ahead, behind = sorted(
        (ours, theirs),
        key=lambda e: STATUS_RANK.get(e.get("status"), -1),
        reverse=True)
    merged = dict(behind)
    merged.update(ahead)
    merged["attempts"] = max(ours.get("attempts", 0), theirs.get("attempts", 0))
    seen, entries = set(), []
    for entry in (ours.get("history") or []) + (theirs.get("history") or []):
        key = (entry.get("at"), entry.get("status"))
        if key not in seen:
            seen.add(key)
            entries.append(entry)
    merged["history"] = sorted(entries, key=lambda e: e.get("at") or "")
    return merged


def merge_story_ledger(ours: dict, theirs: dict) -> dict:
    merged = {"schema_version": ours.get("schema_version")
                                or theirs.get("schema_version"),
              "stories": {}}
    our_stories = ours.get("stories") or {}
    their_stories = theirs.get("stories") or {}
    for key in sorted(set(our_stories) | set(their_stories)):
        if key in our_stories and key in their_stories:
            merged["stories"][key] = merge_story_entry(
                our_stories[key], their_stories[key])
        else:
            merged["stories"][key] = our_stories.get(key) or their_stories[key]
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

    counts = {"ledger": 0, "topics": 0, "records": 0, "stories": 0, "niche": 0}
    unresolved = []
    for path in paths:
        if path == LEDGER and resolve(path, merge_ledger):
            counts["ledger"] += 1
        elif path == TOPICS and resolve(path, merge_topics):
            counts["topics"] += 1
        elif path == STORY_LEDGER and resolve(path, merge_story_ledger):
            counts["stories"] += 1
        elif path == NICHE_SCAN and resolve(path, merge_niche_scan):
            counts["niche"] += 1
        elif path.startswith("data/videos/") and path.endswith(".json") \
                and resolve(path, merge_video_record):
            counts["records"] += 1
        else:
            unresolved.append(path)

    print(f"Resolved: {counts['records']} video record(s), "
          f"{counts['ledger']} ledger, {counts['topics']} topic history, "
          f"{counts['stories']} story ledger, {counts['niche']} niche scan.")
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
