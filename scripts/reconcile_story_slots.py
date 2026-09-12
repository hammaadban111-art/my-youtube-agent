#!/usr/bin/env python3
"""Report story-ledger slots with more than one published video.

This is intentionally read-only.  A duplicate slot means two videos are
already live, and no script can know which one should remain public.  Review
the listed IDs in YouTube Studio first; only then make the corresponding
manual ledger/video decision.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from agent import packet  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit machine-readable output")
    args = parser.parse_args()

    duplicates = packet.duplicate_published_slots()
    if args.json:
        print(json.dumps(duplicates, indent=2, sort_keys=True))
        return 0
    if not duplicates:
        print("No duplicate published story slots in content/story_history.json.")
        return 0

    print("Duplicate published story slots (read-only report):")
    for slot, entries in sorted(duplicates.items()):
        print(f"\n{slot}")
        for entry in entries:
            print(f"  - {entry.get('video_id', '?')}  {entry.get('story_id', '?')}  "
                  f"{entry.get('topic_subject', '')!r}  published {entry.get('published_at', '?')}")
    print("\nReview the videos in YouTube Studio before deleting or editing anything. "
          "This command never changes YouTube or the ledger.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
