"""
Tracks titles of previously generated videos so script_writer can steer
Gemini away from repeating topics/angles across multiple runs per day.

Persisted as a committed file (not gitignored) so it survives between
GitHub Actions runs, which otherwise start from a clean checkout every
time — the workflow commits this file back to the repo after each
successful run.
"""
import json
import os

HISTORY_PATH = os.path.join(os.path.dirname(__file__), "..", "history", "topics.json")
MAX_CONTEXT = 30


def load_recent_titles(limit: int = MAX_CONTEXT) -> list[str]:
    if not os.path.exists(HISTORY_PATH):
        return []
    with open(HISTORY_PATH) as f:
        data = json.load(f)
    return [entry["title"] for entry in data[-limit:]]


def append_entry(title: str):
    os.makedirs(os.path.dirname(HISTORY_PATH), exist_ok=True)
    data = []
    if os.path.exists(HISTORY_PATH):
        with open(HISTORY_PATH) as f:
            data = json.load(f)
    data.append({"title": title})
    with open(HISTORY_PATH, "w") as f:
        json.dump(data, f, indent=2)
