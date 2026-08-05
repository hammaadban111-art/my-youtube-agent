"""
Tracks titles of previously generated videos so script_writer can steer
Gemini away from repeating topics/angles across multiple runs per day.

Persisted as a committed file (not gitignored) so it survives between
GitHub Actions runs, which otherwise start from a clean checkout every
time — the workflow commits this file back to the repo after each
successful run.

Titles alone were not enough. On 2026-07-30 two videos went up 3.5 hours
apart on the same subject — both `topic_subject` "Tamam Shud case" — under
the titles "The Cold War Spy Who Never Existed" and "The 74-Year Mystery of
Australia's Somerton Man". Nothing about those two strings looks like a
repeat, and steering the model with titles could not have caught it. So the
subject is stored alongside the title and compared in NORMALIZED form.
"""
import json
import os
import re
import string
from datetime import date

HISTORY_PATH = os.path.join(os.path.dirname(__file__), "..", "history", "topics.json")
# 60, not 30. At 4 uploads/day the old window was ~7 days — but the real
# Tamam Shud repeat happened INSIDE that window, so the lookback was never
# the whole problem; comparing subjects instead of titles is. Widened anyway
# for headroom, since the cost is a few extra lines in a prompt.
MAX_CONTEXT = 60

# Words that describe the KIND of story rather than which story it is.
# Dropping them is what makes "Tamam Shud case" and "Tamam Shud" the same
# subject, and "Dancing plague of 1518" the same as "1518 dancing plague".
SUBJECT_STOPWORDS = frozenset({
    "a", "an", "and", "at", "in", "of", "on", "the",
    "affair", "case", "disappearance", "disaster", "event", "incident",
    "mystery", "phenomenon", "story",
})
# The smallest overlap that counts as "the same subject" under the subset
# rule. One shared word is a coincidence ("Lake Natron" vs "Lake Baikal");
# two is a subject ("Lake Natron" vs "Lake Natron calcification").
MIN_SUBSET_TOKENS = 2

# B2 lands on this date, not before: Group B changes go out one at a time and
# at least 48h apart (PLAN.md), and B1 (tags on upload) landed 2026-08-05.
# Until then the subjects are RECORDED but nothing about the generated video
# changes. A malformed value holds the change back rather than releasing it
# early — the same fail-closed rule as predict.SELF_IMPROVE_AFTER.
#
# `or` rather than a getenv default: an unset GitHub repo Variable arrives as
# an EMPTY string, not as absent, and an empty string would otherwise read as
# "no hold at all" — releasing the change early by omission, which is exactly
# backwards. To release it, set a date in the past.
DUPLICATE_BLOCK_AFTER = (os.getenv("DUPLICATE_BLOCK_AFTER") or "2026-08-07").strip()


def detection_held_until(today: date = None) -> str | None:
    """The DUPLICATE_BLOCK_AFTER date if it is still in the future, else None.
    None means duplicate detection is live."""
    if not DUPLICATE_BLOCK_AFTER:
        return None
    today = today or date.today()
    try:
        hold = date.fromisoformat(DUPLICATE_BLOCK_AFTER)
    except ValueError:
        return DUPLICATE_BLOCK_AFTER  # unparseable — hold, and report the raw value
    return DUPLICATE_BLOCK_AFTER if today < hold else None


def detection_active(today: date = None) -> bool:
    return detection_held_until(today) is None


def normalize_subject(subject: str) -> str:
    """Reduces a topic_subject to a comparable form: lowercased, without a
    trailing Wikipedia disambiguator, without punctuation, and without the
    generic words above. Returns "" for anything that normalizes away to
    nothing, which never matches."""
    if not isinstance(subject, str):
        return ""
    text = subject.strip().lower()
    # "Devil's Kettle (Minnesota)" and "Devil's Kettle" are one subject.
    text = re.sub(r"\s*\([^)]*\)\s*$", "", text)
    # Apostrophes go rather than becoming spaces, so devil's -> devils.
    text = text.replace("'", "").replace("’", "")
    text = "".join(ch if ch not in string.punctuation else " " for ch in text)
    tokens = [t for t in text.split() if t]
    significant = [t for t in tokens if t not in SUBJECT_STOPWORDS]
    # A subject made entirely of stopwords ("The Incident") keeps its raw
    # tokens rather than normalizing to nothing at all.
    return " ".join(sorted(significant or tokens))


def _tokens(subject: str) -> frozenset:
    return frozenset(normalize_subject(subject).split())


def is_duplicate_subject(subject: str, previous: list[str]) -> str | None:
    """The first entry of `previous` that names the same subject, or None.

    Three ways to be the same subject, in increasing looseness:
      1. identical normalized form ("Tamam Shud case" twice)
      2. same significant words in any order ("Dancing plague of 1518")
      3. one is a strict superset of the other, sharing >= MIN_SUBSET_TOKENS
         ("Lake Natron" vs "Lake Natron calcification")
    Deliberately conservative: a false positive costs a wasted Gemini call
    and, at worst, a rejected script."""
    candidate = _tokens(subject)
    if not candidate:
        return None
    for prior in previous:
        prior_tokens = _tokens(prior)
        if not prior_tokens:
            continue
        if candidate == prior_tokens:
            return prior
        smaller, larger = sorted((candidate, prior_tokens), key=len)
        if len(smaller) >= MIN_SUBSET_TOKENS and smaller < larger:
            return prior
    return None


def _load() -> list[dict]:
    if not os.path.exists(HISTORY_PATH):
        return []
    with open(HISTORY_PATH) as f:
        return json.load(f)


def load_recent_titles(limit: int = MAX_CONTEXT) -> list[str]:
    return [entry["title"] for entry in _load()[-limit:]]


def load_recent_subjects(limit: int = MAX_CONTEXT) -> list[str]:
    """The subjects of recent videos, newest last, deduplicated by normalized
    form so the prompt doesn't list the same subject twice. Entries written
    before subjects were recorded simply have none — they are skipped, not
    treated as an error."""
    seen = set()
    subjects = []
    for entry in _load()[-limit:]:
        subject = (entry.get("topic_subject") or "").strip()
        if not subject:
            continue
        key = normalize_subject(subject)
        if not key or key in seen:
            continue
        seen.add(key)
        subjects.append(subject)
    return subjects


def append_entry(title: str, topic_subject: str = None):
    """Records one published video. `topic_subject` is optional so older
    callers keep working, but the pipeline always passes it — it is the field
    duplicate detection actually reads."""
    os.makedirs(os.path.dirname(HISTORY_PATH), exist_ok=True)
    data = _load()
    entry = {"title": title}
    if topic_subject:
        entry["topic_subject"] = topic_subject
    data.append(entry)
    with open(HISTORY_PATH, "w") as f:
        json.dump(data, f, indent=2)
