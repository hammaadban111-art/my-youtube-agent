#!/usr/bin/env python3
"""Recomputes data/benchmark.json's per-category signal from data/niche_scan.json.

WHY THIS EXISTS
---------------
Two problems that turned out to be the same problem.

1. `data/niche_scan.json` was DEAD OUTPUT. niche_scan.yml runs every Monday,
   spends real API quota scanning ~545 Shorts across the niche, commits the
   result — and nothing in production ever read the file. Verified 2026-09-09:
   no module outside the scanner itself referenced it.

2. `data/benchmark.json`'s `categories` block was STALE and had no refresh path
   at all. It was captured 2026-08-01 and every other maintenance script leaves
   it alone: scripts/refresh_benchmark.py rebuilds the channel percentiles and
   `prior_views`, never the category residuals. By 2026-09-09 the numbers
   steering topic selection were 39 days old, and the only reason nobody had
   noticed was that nothing consumed them either (see agent/editorial.py's
   topic_category_tiebreaker, added the same day).

The scan already contains everything the category signal needs — title,
subscriber count, views — so recomputing costs ZERO additional API calls. The
weekly scan now feeds the weekly signal.

THE METHOD, AND WHY IT IS RESIDUALS RATHER THAN MEDIANS
-------------------------------------------------------
Raw per-category view medians are worthless here and agent/benchmark.py already
says so: they track the size mix of whichever channels the query surfaced, and
median subscriber count per category ranged from 1,860 to 988,500 in the
original scan. A category full of big channels looks "better" purely because
big channels get more views.

So performance is measured as a residual against channel size:

    log10(views) ~ a + b * log10(subscribers)          (least squares, all videos)
    residual     = log10(views) - predicted

The residual is how much better or worse a video did than a same-sized channel
typically does. Averaged per category and raised back through 10^x, it becomes a
multiplier where 1.0 is "exactly par for a channel this size".

WHAT THIS IS NOT
----------------
Not a forecast for any individual video, and the output is deliberately shaped
so it cannot be used as one. Within-category spread is about as large as
between-category spread, so these rank content TYPES and nothing more. A
category whose effect sits inside its own error bars is marked usable=false and
agent/benchmark.py refuses to report it.

Usage:
    python scripts/refresh_category_signal.py --dry-run
    python scripts/refresh_category_signal.py
"""
import argparse
import json
import math
import os
import statistics
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import benchmark  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCAN_PATH = os.path.join(ROOT, "data", "niche_scan.json")
BENCHMARK_PATH = os.path.join(ROOT, "data", "benchmark.json")

# A category needs this many scanned videos before it may say anything.
# Mirrors agent/benchmark.py's MIN_CATEGORY_SAMPLES so the writer and the
# reader agree about what "enough" means.
MIN_SAMPLES = benchmark.MIN_CATEGORY_SAMPLES
# How far outside its own error bars an effect must sit to count. Two standard
# errors is the same bar the 2026-08-01 snapshot used: it is what marked
# `unexplained` (residual 0.029, se 0.092) unusable while keeping
# `disappearance` (residual -0.237, se 0.116).
USABLE_SE_MULTIPLE = 2.0


def load_scan(path: str = SCAN_PATH) -> dict:
    with open(path) as f:
        scan = json.load(f)
    if not isinstance(scan, dict) or not isinstance(scan.get("videos"), list):
        raise SystemExit(f"{path} is not a niche scan (no videos list).")
    return scan


def _fit(points: list[tuple[float, float]]) -> tuple[float, float]:
    """Least-squares a, b for y = a + b*x. Falls back to a flat fit when every
    x is identical, which would otherwise divide by zero."""
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    mean_x, mean_y = statistics.mean(xs), statistics.mean(ys)
    sxx = sum((x - mean_x) ** 2 for x in xs)
    if sxx == 0:
        return mean_y, 0.0
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    slope = sxy / sxx
    return mean_y - slope * mean_x, slope


def compute_categories(scan: dict) -> tuple[dict, dict]:
    """(categories block, provenance) recomputed from a niche scan."""
    usable_rows = []
    for video in scan.get("videos") or []:
        if not isinstance(video, dict):
            continue
        subs, views = video.get("subs") or 0, video.get("views") or 0
        title = str(video.get("title") or "")
        # log10 needs both strictly positive. A channel that hides its
        # subscriber count reads as 0 here and is dropped rather than assumed.
        if subs < 1 or views < 1:
            continue
        usable_rows.append((math.log10(subs), math.log10(views),
                            benchmark.categorize(title)))

    if len(usable_rows) < MIN_SAMPLES:
        raise SystemExit(
            f"only {len(usable_rows)} scanned videos have both a subscriber "
            "count and a view count; refusing to recompute the signal from that.")

    intercept, slope = _fit([(x, y) for x, y, _ in usable_rows])

    residuals: dict[str, list[float]] = {}
    for x, y, category in usable_rows:
        if category:
            residuals.setdefault(category, []).append(y - (intercept + slope * x))

    categories = {}
    for name, values in residuals.items():
        n = len(values)
        mean_residual = statistics.mean(values)
        sd = statistics.stdev(values) if n > 1 else 0.0
        standard_error = (sd / math.sqrt(n)) if n > 1 else 0.0
        enough = n >= MIN_SAMPLES
        outside_error_bars = abs(mean_residual) > USABLE_SE_MULTIPLE * standard_error
        categories[name] = {
            "n": n,
            "residual_log10": round(mean_residual, 3),
            "multiplier": round(10 ** mean_residual, 2),
            "standard_error": round(standard_error, 3),
            "usable": bool(enough and outside_error_bars),
            "within_category_sd_log10": round(sd, 3),
        }

    provenance = {
        "recomputed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "from_scan_at": scan.get("scanned_at"),
        "scanned_videos": len(scan.get("videos") or []),
        "videos_used": len(usable_rows),
        "size_fit": {"intercept": round(intercept, 4), "slope": round(slope, 4)},
        "method": ("per-category mean of log10(views) residuals against a "
                   "least-squares fit on log10(subscribers); multiplier = "
                   "10^mean_residual, where 1.0 is par for a channel that size"),
        "usable_rule": (f"n >= {MIN_SAMPLES} and |residual| > "
                        f"{USABLE_SE_MULTIPLE} standard errors"),
    }
    return categories, provenance


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Recompute benchmark category signal from the niche scan.")
    parser.add_argument("--dry-run", action="store_true",
                        help="print what would change without writing.")
    args = parser.parse_args()

    scan = load_scan()
    categories, provenance = compute_categories(scan)

    with open(BENCHMARK_PATH) as f:
        bench = json.load(f)
    previous = bench.get("categories") or {}

    print(f"Scan from {provenance['from_scan_at']}: "
          f"{provenance['videos_used']} of {provenance['scanned_videos']} "
          "videos usable.")
    print(f"Size fit: log10(views) = {provenance['size_fit']['intercept']} + "
          f"{provenance['size_fit']['slope']}*log10(subs)")
    print()
    print(f"{'category':<20}{'n':>5}{'multiplier':>12}{'usable':>9}   was")
    for name, entry in sorted(categories.items(),
                              key=lambda kv: -kv[1]["multiplier"]):
        was = previous.get(name, {}).get("multiplier", "—")
        print(f"{name:<20}{entry['n']:>5}{entry['multiplier']:>12}"
              f"{str(entry['usable']):>9}   {was}")

    if args.dry_run:
        print("\n[DRY RUN] data/benchmark.json not written.")
        return 0

    # Only the category block and its provenance are replaced. prior_views,
    # prior_basis, selection, sample and category_keywords belong to
    # scripts/refresh_benchmark.py and the original scan design; overwriting
    # them here would silently take ownership of numbers this script does not
    # compute.
    bench["categories"] = categories
    bench["category_provenance"] = provenance
    with open(BENCHMARK_PATH, "w") as f:
        json.dump(bench, f, indent=2)
        f.write("\n")
    print(f"\nWrote {BENCHMARK_PATH} (categories + category_provenance only).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
