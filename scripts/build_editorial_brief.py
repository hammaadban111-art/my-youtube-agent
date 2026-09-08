#!/usr/bin/env python3
"""Build weekly no-LLM editorial evidence for Claude Cowork.

Run before the Wednesday story-research session. It reads only local channel
records, never calls Gemini, YouTube, or a text model.
"""
import argparse
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from agent import editorial  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json-out", default=str(editorial.BRIEF_JSON_PATH))
    parser.add_argument("--markdown-out", default=str(editorial.BRIEF_MARKDOWN_PATH))
    args = parser.parse_args()

    brief = editorial.write_brief(
        json_path=args.json_out,
        markdown_path=args.markdown_out,
    )
    snapshot = brief["channel_snapshot"]
    print(
        f"Wrote {os.path.relpath(args.json_out, ROOT)} and "
        f"{os.path.relpath(args.markdown_out, ROOT)}: "
        f"{snapshot['usable_records']} usable records, "
        f"{len(brief['avoid_subjects'])} no-repeat subjects."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
