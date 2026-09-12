"""Deterministic editorial feedback for the weekly Claude Cowork session.

The daily pipeline must never choose or invent a new story. This module is
different: it turns already-recorded channel results into a compact, auditable
brief that Claude reads *before* researching the next weekly packet. It makes
the feedback loop real without granting a model permission to silently alter
the upload schedule or recycle a previous subject.
"""
from __future__ import annotations

import hashlib
import json
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import benchmark, history, predict, store

ROOT = Path(__file__).resolve().parents[1]
BRIEF_SCHEMA_VERSION = 1
BRIEF_JSON_PATH = ROOT / "content" / "weekly_editorial_brief.json"
BRIEF_MARKDOWN_PATH = ROOT / "content" / "weekly_editorial_brief.md"
HOOK_WINDOW = 0.25
MAX_EXAMPLES = 5
MAX_BRIEF_AGE_DAYS = 8
# A file generated moments ago is not fresh evidence if its newest underlying
# channel reading is weeks old. Keep that distinction explicit for Cowork.
MAX_MEASUREMENT_AGE_DAYS = 8
# A single good-looking retention curve is not enough to teach the next weekly
# research session a hook pattern. This is deliberately small, but prevents a
# weak sample from turning one video into editorial policy.
MIN_HOOK_EVIDENCE_RECORDS = 4

EXAMPLE_FIELDS = (
    "strong_mature_examples",
    "strong_early_examples",
    "strong_hook_examples",
    "hook_watch_examples",
    "early_performance_watchlist",
)
SNAPSHOT_COUNT_FIELDS = (
    "measured_records",
    "fresh_measured_records",
    "stale_measurement_records",
    "usable_records",
    "excluded_no_signal_records",
    "comparable_early_records",
    "hook_retention_records",
    "hook_evidence_records",
    "hook_evidence_minimum",
)


class EditorialBriefError(ValueError):
    """A brief is missing, malformed, or too old to guide a new packet."""


def _latest(record: dict) -> dict:
    return record.get("latest_measurement") or record.get("measurement") or {}


def _number(value) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _clean_number(value):
    value = _number(value)
    if value is None:
        return None
    return int(value) if value.is_integer() else round(value, 4)


def _latest_views(record: dict):
    return _clean_number(_latest(record).get("actual_views"))


def _frozen_views(record: dict):
    return _clean_number((record.get("measurement") or {}).get("actual_views"))


def _hook_retention(record: dict) -> float | None:
    retention = _latest(record).get("retention") or {}
    if not retention.get("available"):
        return None
    curve = retention.get("curve") or []
    values = [
        _number(point.get("watch_ratio"))
        for point in curve
        if isinstance(point, dict)
        and _number(point.get("position")) is not None
        and _number(point.get("position")) <= HOOK_WINDOW
        and _number(point.get("watch_ratio")) is not None
    ]
    return round(min(values), 4) if values else None


def _opening_line(record: dict) -> str | None:
    script = record.get("script") or {}
    segments = script.get("segments") if isinstance(script, dict) else None
    if not isinstance(segments, list):
        segments = record.get("segments") or []
    for segment in segments:
        if isinstance(segment, dict):
            line = str(segment.get("narration") or "").strip()
            if line:
                return line
    return None


def _subject(record: dict) -> str:
    return str(record.get("topic_subject") or "").strip()


def _subject_key(record: dict) -> str:
    return history.normalize_subject(_subject(record))


def _example(record: dict) -> dict:
    frozen = record.get("measurement") or {}
    return {
        "subject": _subject(record),
        "title": str(record.get("title") or "").strip(),
        "latest_views": _latest_views(record),
        "frozen_views": _frozen_views(record),
        "frozen_hours_after_upload": _clean_number(
            frozen.get("hours_after_upload")),
        "hook_retention": _hook_retention(record),
        "opening_line": _opening_line(record),
    }


def _unique_ranked(records: list[dict], key, *, reverse: bool,
                   limit: int = MAX_EXAMPLES) -> list[dict]:
    ranked = sorted(
        (record for record in records if key(record) is not None),
        key=key,
        reverse=reverse,
    )
    seen, result = set(), []
    for record in ranked:
        subject = _subject_key(record)
        if not subject or subject in seen:
            continue
        seen.add(subject)
        result.append(_example(record))
        if len(result) == limit:
            break
    return result


def _median(values: list[float | int]) -> float | int | None:
    if not values:
        return None
    return _clean_number(statistics.median(values))


def _comparable_early(record: dict) -> bool:
    frozen = record.get("measurement") or {}
    hours = _number(frozen.get("hours_after_upload"))
    return (_frozen_views(record) is not None
            and hours is not None
            and 3 <= hours <= 12)


def _top_early_cohort(records: list[dict]) -> list[dict]:
    """Top quartile by fixed-time views, used to judge hook structures.

    A looping/high retention curve on a video scarcely shown to anyone is not
    proof its hook should be copied. Restricting hook examples to the strongest
    early cohort makes the brief show patterns paired with real distribution.
    """
    ranked = sorted(records, key=_frozen_views, reverse=True)
    return ranked[:max(1, (len(ranked) + 3) // 4)] if ranked else []


def _distributed_early(records: list[dict]) -> list[dict]:
    """Early measurements that cleared the channel's throttle floor.

    ``has_signal`` deliberately judges the latest reading: a healthy video can
    be slow in its first few hours. Hook examples make a different claim,
    though: that the opening paired with enough *early* distribution to be
    worth studying. Keep that evidence bar explicit instead of letting a weak
    cohort make a 5-view clip look like the channel's best hook.
    """
    return [
        record for record in records
        if (_frozen_views(record) or 0) > predict.NO_SIGNAL_VIEW_THRESHOLD
    ]


def _as_utc(now: datetime | None) -> datetime:
    now = now or datetime.now(timezone.utc)
    return now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc)


def _fresh_measurements(records: list[dict], now: datetime) -> tuple[list[dict], int]:
    """Fresh records and the number excluded for stale/unknown readings."""
    cutoff = now - timedelta(days=MAX_MEASUREMENT_AGE_DAYS)
    fresh = []
    stale = 0
    for record in records:
        measured_at = store.last_reading_at(record)
        if measured_at is None or measured_at < cutoff or measured_at > now + timedelta(minutes=5):
            stale += 1
            continue
        fresh.append(record)
    return fresh, stale


def _avoid_subjects(published_subjects: list[str] | None) -> list[str]:
    """Every durable no-repeat source, including ledger-only publications."""
    if published_subjects is not None:
        source = published_subjects
    else:
        source = list(history.published_subjects())
        try:
            # packet imports editorial, so keep this lazy and only invoke it
            # after this module is fully initialized.
            from . import packet
            source.extend(packet.published_story_subjects())
        except Exception as exc:  # noqa: BLE001 - preserve known history
            print(f"[editorial] could not include ledger-only published subjects: "
                  f"{type(exc).__name__}: {exc}")
    seen, result = set(), []
    for subject in source:
        value = str(subject or "").strip()
        key = history.normalize_subject(value)
        if value and key and key not in seen:
            seen.add(key)
            result.append(value)
    return result


def build_brief(records: list[dict] | None = None, *, now: datetime | None = None,
                published_subjects: list[str] | None = None) -> dict:
    """Build plain JSON evidence; no LLM scoring or topic invention occurs."""
    records = list(store.measured_records() if records is None else records)
    now_utc = _as_utc(now)
    fresh_records, stale_count = _fresh_measurements(records, now_utc)
    usable = [
        record for record in fresh_records
        if _subject(record) and _latest_views(record) is not None
        and predict.has_signal(record)
    ]
    excluded_no_signal = sum(
        1 for record in fresh_records
        if _subject(record) and _latest_views(record) is not None
        and not predict.has_signal(record)
    )
    early = [record for record in usable if _comparable_early(record)]
    hooks = [record for record in usable if _hook_retention(record) is not None]
    hook_evidence = [record for record in _top_early_cohort(_distributed_early(early))
                     if _hook_retention(record) is not None]
    hook_evidence_sufficient = len(hook_evidence) >= MIN_HOOK_EVIDENCE_RECORDS
    hook_examples = hook_evidence if hook_evidence_sufficient else []
    avoid_subjects = _avoid_subjects(published_subjects)

    latest_values = [_latest_views(record) for record in usable]
    frozen_values = [_frozen_views(record) for record in early]
    hook_values = [_hook_retention(record) for record in hooks]
    generated_at = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    evidence_status = "fresh" if fresh_records else ("stale" if records else "none")

    return {
        "schema_version": BRIEF_SCHEMA_VERSION,
        "generated_at": generated_at,
        "purpose": (
            "Required evidence input for the next weekly Claude Cowork story "
            "draft. It informs research and hook choices; it does not select "
            "or publish a story by itself."
        ),
        "methodology": {
            "latest_views": (
                "Directional mature-topic signal from each record's newest "
                "measurement; video ages differ, so do not compare it as a "
                "fixed-time forecast."
            ),
            "frozen_views": (
                "First post-upload reading, included only when captured 3-12 "
                "hours after upload; use this for comparable early performance."
            ),
            "hook_retention": (
                "Lowest audience-retention value in first 25% of the video. "
                "Use it to study hook structure, never to copy a prior subject."
            ),
            "no_signal": (
                "Records at or below the channel's distribution-throttle "
                "threshold are excluded from rankings rather than called bad topics."
            ),
        },
        "channel_snapshot": {
            "measured_records": len(records),
            "fresh_measured_records": len(fresh_records),
            "stale_measurement_records": stale_count,
            "evidence_status": evidence_status,
            "usable_records": len(usable),
            "excluded_no_signal_records": excluded_no_signal,
            "comparable_early_records": len(early),
            "hook_retention_records": len(hooks),
            "hook_evidence_records": len(hook_evidence),
            "hook_evidence_minimum": MIN_HOOK_EVIDENCE_RECORDS,
            "hook_evidence_sufficient": hook_evidence_sufficient,
            "median_latest_views": _median(latest_values),
            "median_frozen_views": _median(frozen_values),
            "median_hook_retention": _median(hook_values),
        },
        "editorial_rules": [
            "Read this brief before researching or drafting any story.",
            "Never repeat a subject in avoid_subjects. The packet validator is the final duplicate gate.",
            "Use strong and weak hook examples for structure only when the brief says hook evidence is sufficient. They come from the top early-performance cohort above the throttle floor, not high-retention low-distribution clips.",
            "Use frozen_views for early-performance comparisons and latest_views only as directional mature evidence.",
            "Treat low-signal examples as questions to avoid or test, not proof that an entire topic category cannot work.",
            "If evidence_status is stale or none, do not call a pattern current; research broadly and wait for fresh measurements before making a performance claim.",
        ],
        "strong_mature_examples": _unique_ranked(
            usable, _latest_views, reverse=True),
        "strong_early_examples": _unique_ranked(
            early, _frozen_views, reverse=True),
        "strong_hook_examples": _unique_ranked(
            hook_examples, _hook_retention, reverse=True),
        "hook_watch_examples": _unique_ranked(
            hook_examples, _hook_retention, reverse=False),
        "early_performance_watchlist": _unique_ranked(
            early, _frozen_views, reverse=False),
        "topic_category_tiebreaker": _category_tiebreaker(usable),
        "avoid_subjects": avoid_subjects,
    }


# How much of our own catalogue the classifier must be able to see before its
# ranking is allowed into the brief at all. Below this the ranking describes
# whichever minority of videos happened to match a keyword, which is worse than
# saying nothing. Measured 2026-09-09: coverage was 41% before the keyword
# supplement landed and 71% after.
MIN_CATEGORY_COVERAGE = 0.6
# Videos of ours in a category before we quote our own median for it. One video
# is an anecdote; the murder_coldcase "category" has exactly one and its 1,059
# views would otherwise read as the channel's best-performing content type.
MIN_OWN_CATEGORY_RECORDS = 5


def _category_tiebreaker(usable: list[dict]) -> dict:
    """The niche's content-type ranking, plus how our OWN videos did in each.

    A TIE-BREAKER, NOT A FORECAST, and the structure says so. The niche scan
    measures an 8.5x spread between the best and worst content type
    (science_nature 3.94x, murder_coldcase 0.46x), which is a large enough
    signal to be worth acting on — but agent/benchmark.py's own analysis records
    that between-category spread (0.93 log10) is about the same as
    within-category spread (0.97), so these numbers rank topic TYPES and cannot
    predict any individual video. Multiplying a predicted view count by one
    would be reading far more into it than it can carry.

    Our own medians are shown beside the niche multiplier precisely so the two
    can disagree in the open. As of 2026-09-09 they partly do: science_nature is
    both the niche's top category and ours (median 735 over 31 videos), while
    historical is the niche's second (2.68x) and our weakest (median 591 over
    14). A tie-breaker survives that disagreement honestly; a multiplier would
    not.

    Until 2026-09-09 none of this reached the brief at all — categorize(),
    category_signal() and category_ranking() were written, tested and then
    called by nothing outside tests/."""
    texts, by_category = [], {}
    for record in usable:
        text = f"{record.get('title') or ''} {_subject(record) or ''}"
        texts.append(text)
        category = benchmark.categorize(text)
        if not category:
            continue
        views = _latest_views(record)
        if views is not None:
            by_category.setdefault(category, []).append(views)

    coverage = benchmark.categorization_coverage(texts)
    ranking = []
    for entry in benchmark.category_ranking():
        name = entry.get("category")
        ours = sorted(by_category.get(name) or [])
        enough = len(ours) >= MIN_OWN_CATEGORY_RECORDS
        ranking.append({
            "category": name,
            "niche_multiplier": entry.get("multiplier"),
            "niche_sample": entry.get("n"),
            "our_videos": len(ours),
            # None, not 0, when we have too few of our own to say anything.
            "our_median_views": (_median(ours) if enough else None),
            "our_evidence_sufficient": enough,
        })

    return {
        "what_this_is": (
            "Content-type ranking from the niche scan, shown beside how this "
            "channel's own videos performed in the same categories."
        ),
        "how_to_use_it": (
            "A TIE-BREAKER between two stories that are otherwise equally "
            "good, not a reason to pick a weak story in a strong category, and "
            "never a multiplier on a predicted view count. Where the niche "
            "ranking and our own median disagree, our own median is the more "
            "relevant number and neither is decisive."
        ),
        "classifier_coverage": coverage["coverage"],
        "classifier_coverage_sufficient": coverage["coverage"] >= MIN_CATEGORY_COVERAGE,
        "classified_videos": coverage["classified"],
        "total_videos": coverage["total"],
        "benchmark_captured_at": (benchmark.load() or {}).get("captured_at"),
        "ranking": ranking if coverage["coverage"] >= MIN_CATEGORY_COVERAGE else [],
    }


def _fmt(value, suffix: str = "") -> str:
    if value is None:
        return "not captured"
    if suffix == "%":
        return f"{value * 100:.1f}%"
    return f"{value:,}" if isinstance(value, int) else str(value)


def _example_lines(examples: list[dict], *, include_opening: bool = False) -> list[str]:
    if not examples:
        return ["- No usable examples yet."]
    lines = []
    for item in examples:
        line = (f"- **{item['title'] or item['subject']}** — `{item['subject']}`; "
                f"latest {_fmt(item['latest_views'])}, "
                f"frozen {_fmt(item['frozen_views'])}, "
                f"hook {_fmt(item['hook_retention'], '%')}")
        lines.append(line)
        if include_opening and item.get("opening_line"):
            lines.append(f"  - Opening: {item['opening_line']}")
    return lines


def markdown(brief: dict) -> str:
    snapshot = brief["channel_snapshot"]
    lines = [
        "# Weekly editorial brief",
        "",
        "> Required input for weekly Claude Cowork research. Read this before drafting; "
        "use evidence as patterns, never as a license to repeat a subject.",
        "",
        f"- Generated: `{brief['generated_at']}`",
        f"- Usable records: {snapshot['usable_records']} of {snapshot['measured_records']} "
        f"({snapshot['excluded_no_signal_records']} throttle/no-signal excluded)",
        f"- Evidence freshness: {snapshot['evidence_status']} "
        f"({snapshot['fresh_measured_records']} fresh; "
        f"{snapshot['stale_measurement_records']} stale or undated; "
        f"freshness window {MAX_MEASUREMENT_AGE_DAYS} days)",
        f"- Comparable early records: {snapshot['comparable_early_records']}; "
        f"median frozen views: {_fmt(snapshot['median_frozen_views'])}",
        f"- Hook-retention records: {snapshot['hook_retention_records']} "
        f"({snapshot['hook_evidence_records']} in hook-evidence cohort; "
        f"minimum {snapshot['hook_evidence_minimum']}; "
        f"{'sufficient' if snapshot['hook_evidence_sufficient'] else 'not yet sufficient'}); "
        f"median hook hold: {_fmt(snapshot['median_hook_retention'], '%')}",
        "",
        "## Required use",
        "",
    ]
    lines.extend(f"{index}. {rule}" for index, rule in enumerate(brief["editorial_rules"], 1))
    lines += ["", "## Strong mature examples", ""]
    lines.extend(_example_lines(brief["strong_mature_examples"]))
    lines += ["", "## Strong early examples", ""]
    lines.extend(_example_lines(brief["strong_early_examples"]))
    lines += ["", "## Strong hook examples — top early-performance cohort", ""]
    if snapshot["hook_evidence_sufficient"]:
        lines.extend(_example_lines(brief["strong_hook_examples"], include_opening=True))
    else:
        lines.append(
            "- Not enough distributed early-performance records yet; do not "
            "derive a hook pattern from this sample.")
    lines += ["", "## Hook watch examples — same cohort", ""]
    if snapshot["hook_evidence_sufficient"]:
        lines.extend(_example_lines(brief["hook_watch_examples"], include_opening=True))
    else:
        lines.append("- Waiting for the same minimum evidence threshold.")
    lines += ["", "## Early-performance watchlist", ""]
    lines.extend(_example_lines(brief["early_performance_watchlist"]))
    lines += [
        "",
        "## No-repeat source",
        "",
        f"`weekly_editorial_brief.json` contains all {len(brief['avoid_subjects'])} "
        "published subjects in `avoid_subjects`. Read that full list before choosing "
        "new subjects; `agent/packet.py` remains final enforcement.",
        "",
    ]
    return "\n".join(lines)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def write_brief(*, json_path: Path | str = BRIEF_JSON_PATH,
                markdown_path: Path | str = BRIEF_MARKDOWN_PATH,
                now: datetime | None = None) -> dict:
    brief = build_brief(now=now)
    json_path, markdown_path = Path(json_path), Path(markdown_path)
    _write(json_path, json.dumps(brief, indent=2, ensure_ascii=False) + "\n")
    _write(markdown_path, markdown(brief))
    return brief


def _parse_generated_at(value: object) -> datetime:
    raw = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EditorialBriefError("editorial brief generated_at is invalid") from exc
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _is_nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _validate_brief_shape(brief: dict) -> None:
    """Reject a fresh but incomplete file before it can become provenance.

    A current timestamp alone must not let a hand-written placeholder prove
    that Claude had real feedback. This deliberately validates the brief's
    public contract rather than each optional example's editorial quality.
    """
    problems = []
    if not str(brief.get("purpose") or "").strip():
        problems.append("purpose is missing")

    methodology = brief.get("methodology")
    if (not isinstance(methodology, dict) or not methodology
            or not all(str(value or "").strip() for value in methodology.values())):
        problems.append("methodology is missing or empty")

    rules = brief.get("editorial_rules")
    if (not isinstance(rules, list) or not rules
            or not all(isinstance(rule, str) and rule.strip() for rule in rules)):
        problems.append("editorial_rules is missing or empty")

    for field in EXAMPLE_FIELDS:
        if not isinstance(brief.get(field), list):
            problems.append(f"{field} is not a list")

    avoid_subjects = brief.get("avoid_subjects")
    if (not isinstance(avoid_subjects, list)
            or not all(isinstance(subject, str) and subject.strip()
                       for subject in avoid_subjects)):
        problems.append("avoid_subjects is not a clean list")

    snapshot = brief.get("channel_snapshot")
    if not isinstance(snapshot, dict):
        problems.append("channel_snapshot is missing")
    else:
        for field in SNAPSHOT_COUNT_FIELDS:
            if not _is_nonnegative_int(snapshot.get(field)):
                problems.append(f"channel_snapshot.{field} is not a non-negative integer")
        sufficient = snapshot.get("hook_evidence_sufficient")
        if not isinstance(sufficient, bool):
            problems.append("channel_snapshot.hook_evidence_sufficient is not a boolean")
        if snapshot.get("evidence_status") not in ("fresh", "stale", "none"):
            problems.append("channel_snapshot.evidence_status is invalid")
        if snapshot.get("hook_evidence_minimum") != MIN_HOOK_EVIDENCE_RECORDS:
            problems.append("channel_snapshot.hook_evidence_minimum is not the required positive minimum")
        if not problems:
            if (snapshot["fresh_measured_records"] + snapshot["stale_measurement_records"]
                    != snapshot["measured_records"]):
                problems.append("channel_snapshot fresh and stale counts do not equal measured records")
            if snapshot["usable_records"] + snapshot["excluded_no_signal_records"] > snapshot["measured_records"]:
                problems.append("channel_snapshot usable and excluded counts exceed measured records")
            if snapshot["comparable_early_records"] > snapshot["usable_records"]:
                problems.append("channel_snapshot comparable early count exceeds usable records")
            if snapshot["hook_retention_records"] > snapshot["usable_records"]:
                problems.append("channel_snapshot hook retention count exceeds usable records")
            if snapshot["hook_evidence_records"] > snapshot["hook_retention_records"]:
                problems.append("channel_snapshot hook evidence count exceeds retention records")
            expected_sufficient = (
                snapshot["hook_evidence_records"] >= snapshot["hook_evidence_minimum"])
            if sufficient != expected_sufficient:
                problems.append("channel_snapshot hook evidence sufficiency is inconsistent")
            if not sufficient and (brief.get("strong_hook_examples")
                                   or brief.get("hook_watch_examples")):
                problems.append("hook examples exist without sufficient evidence")

    if problems:
        raise EditorialBriefError("editorial brief is malformed: " + "; ".join(problems))


def brief_provenance(path: Path | str = BRIEF_JSON_PATH, *, now: datetime | None = None,
                     max_age_days: int = MAX_BRIEF_AGE_DAYS) -> dict:
    """Validate fresh brief and return compact packet-safe provenance only."""
    path = Path(path)
    if not path.is_file():
        raise EditorialBriefError(
            f"editorial brief is missing: {path}. Run scripts/build_editorial_brief.py first.")
    try:
        raw = path.read_bytes()
        brief = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise EditorialBriefError(f"editorial brief is unreadable: {path}") from exc
    if not isinstance(brief, dict) or brief.get("schema_version") != BRIEF_SCHEMA_VERSION:
        raise EditorialBriefError("editorial brief has an unsupported schema_version")
    _validate_brief_shape(brief)

    generated_at = _parse_generated_at(brief.get("generated_at"))
    age = _as_utc(now) - generated_at
    if age > timedelta(days=max_age_days):
        raise EditorialBriefError(
            f"editorial brief is {age.days} day(s) old; regenerate it before assembling a packet")
    if age < timedelta(minutes=-5):
        raise EditorialBriefError("editorial brief generated_at is too far in the future")

    try:
        label = str(path.resolve().relative_to(ROOT))
    except ValueError:
        label = str(path)
    snapshot = brief.get("channel_snapshot") or {}
    return {
        "path": label,
        "generated_at": brief["generated_at"],
        "sha256": hashlib.sha256(raw).hexdigest(),
        "usable_records": snapshot.get("usable_records"),
        "fresh_measured_records": snapshot.get("fresh_measured_records"),
        "evidence_status": snapshot.get("evidence_status"),
        "hook_retention_records": snapshot.get("hook_retention_records"),
        "hook_evidence_records": snapshot.get("hook_evidence_records"),
        "hook_evidence_sufficient": snapshot.get("hook_evidence_sufficient"),
    }
