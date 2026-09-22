#!/usr/bin/env python3
"""Report story-ledger slots with more than one published video.

Read-only by default.  A duplicate slot means two videos are already live, and
no script can know which one should remain public.  Review the listed IDs in
YouTube Studio first.  To keep every video in a slot, record that decision:

  python scripts/reconcile_story_slots.py --acknowledge 2026-09-08T0607Z \
      --note "both videos kept live"

That only marks the ledger (so the slot stops being reported on every run);
it never changes anything on YouTube.
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
    parser.add_argument("--acknowledge", metavar="SLOT", action="append", default=[],
                        help="mark every video in SLOT as reviewed and kept (repeatable)")
    parser.add_argument("--note", default="reviewed; every video in the slot kept live",
                        help="why the slot was acknowledged")
    args = parser.parse_args()

    for slot in args.acknowledge:
        changed = packet.acknowledge_slot(slot, args.note)
        if not changed:
            print(f"{slot}: not a duplicate slot — nothing to acknowledge.")
            return 1
        print(f"{slot}: acknowledged " + ", ".join(
            str(e.get("video_id")) for e in changed) + f" ({args.note})")
    if args.acknowledge:
        return 0

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
