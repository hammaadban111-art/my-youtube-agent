"""
One-off backfill: stamps a system_version onto video records written before
that field existed.

Deliberately conservative — it only ever ADDS the one missing key. It never
edits, reorders or removes anything else, because these records are the
prediction model's training data and the dashboard's only source of truth;
a backfill that quietly rewrote a view count would be far worse than no
backfill at all.

Usage:
    python -m scripts.backfill_system_version --dry-run   # report only
    python -m scripts.backfill_system_version             # write
"""
import argparse
import glob
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from agent import store  # noqa: E402


def backfill(data_dir: str, dry_run: bool = True) -> dict:
    paths = sorted(glob.glob(os.path.join(data_dir, "*", "*.json")))
    stamped, already, unchanged_check = [], [], []

    for path in paths:
        with open(path) as f:
            original_text = f.read()
        record = json.loads(original_text)

        if record.get("system_version"):
            already.append(os.path.basename(path))
            continue

        before = json.loads(original_text)
        record["system_version"] = store.LEGACY_SYSTEM_VERSION

        # Every key other than the one we added must be byte-identical.
        # This is the actual safety property, so it is asserted rather than
        # assumed - a backfill is exactly the kind of script nobody re-reads
        # after it has run once.
        for key in before:
            if json.dumps(before[key], sort_keys=True) != json.dumps(record[key], sort_keys=True):
                raise AssertionError(f"{path}: backfill would alter existing key {key!r}")
        if set(record) - set(before) != {"system_version"}:
            raise AssertionError(f"{path}: backfill would add unexpected keys")
        unchanged_check.append(os.path.basename(path))

        if not dry_run:
            with open(path, "w") as f:
                json.dump(record, f, indent=2)
        stamped.append(os.path.basename(path))

    return {"total": len(paths), "stamped": stamped,
            "already_tagged": already, "verified_unchanged": unchanged_check}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--data-dir", default=store.DATA_DIR)
    args = parser.parse_args()

    result = backfill(args.data_dir, dry_run=args.dry_run)
    mode = "DRY RUN - nothing written" if args.dry_run else "WROTE"
    print(f"[{mode}] {len(result['stamped'])} record(s) stamped "
          f"'{store.LEGACY_SYSTEM_VERSION}', "
          f"{len(result['already_tagged'])} already tagged, "
          f"{result['total']} total.")
    for name in result["stamped"]:
        print(f"  + {name}")
