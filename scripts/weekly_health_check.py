#!/usr/bin/env python3
"""
The weekly Friday maintenance pass: inspect, self-heal what can be healed
automatically, and report.

WHAT THIS CAN AND CANNOT DO — read this before trusting the report.

It CANNOT write code. "Fix the bugs" is not something a cron job does; what it
does is run the regression suite that catches the bugs this project has already
been bitten by, apply the repairs the repo already knows how to perform without
a human, and say plainly what it found that only a human can fix.

The self-healing repairs, all of which are idempotent and safe to repeat:

  * reconcile the quota ledger against the real video records, so an upload
    the ledger missed (a run that published then died) stops the day's count
    reading several slots short of the truth
  * rebuild the dashboard from current data

The checks, none of which change anything:

  * the regression suite (tests/), the same one tests.yml runs
  * the YouTube OAuth token — probed live, because the 7-day testing-token
    expiry silently halts the whole channel and nothing else notices until a
    scheduled upload fails. Four days of outage passed unnoticed in August.
  * videos published with no record in data/ (the stranded-record failure)
  * videos rendered but never uploaded, still parked in artifacts
  * recent scheduled-run failures, read from the Actions API
  * the weekly story packet: valid, and how many days of stories are left

Every finding lands in the returned report, which the workflow emails. A week
where nothing is wrong sends a short "all clear" and — because the publish step
is gated on real changes — produces no commit and no deployment at all.

Exit status is 0 unless the run itself broke. Findings are reported, not
raised: a red weekly job every week is a job nobody reads.
"""
import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent import (cadence, config, dashboard, history, packet, quota, store,  # noqa: E402
                   upload, youtube_stats)

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")
# Long enough to cover the week this job reports on, plus a margin.
FAILURE_LOOKBACK_DAYS = 8


class Report:
    """Findings, in the order they were made. `blocking` marks the ones a
    human has to act on — they are what the email subject is built from."""

    def __init__(self):
        self.lines: list[str] = []
        self.blocking: list[str] = []

    def note(self, text: str) -> None:
        print(f"[weekly] {text}")
        self.lines.append(text)

    def problem(self, text: str) -> None:
        print(f"[weekly] PROBLEM: {text}")
        self.lines.append(f"PROBLEM: {text}")
        self.blocking.append(text)

    def detail(self, text: str) -> None:
        """An indented supporting line under the finding above it.

        Prints as well as records. The first version only appended, so the
        names of the failing tests reached the emailed report but never the
        run log — leaving the log saying "1 failed" and nothing else."""
        print(f"[weekly]     {text}")
        self.lines.append(f"    {text}")

    def text(self) -> str:
        return "\n".join(self.lines)


# Stripped from the environment before the suite runs. .github/workflows/tests.yml
# states the contract plainly: "No API keys are provided on purpose. Every test
# must run offline; if one starts reaching for Pexels, Wikipedia or YouTube it
# will fail here rather than quietly becoming a flaky network test."
#
# This job, unlike tests.yml, genuinely needs those secrets — it probes the
# YouTube token and emails the report — so it has to take them back out again
# before handing control to pytest. The first run that did not
# (2026-09-07T18:21Z) made tests/test_alerts.py take the real send path and
# actually email the owner from a test.
CREDENTIAL_ENV_VARS = (
    "PEXELS_API_KEY",
    "YT_CLIENT_ID", "YT_CLIENT_SECRET", "YT_REFRESH_TOKEN",
    "RESEND_API_KEY", "NOTIFY_TO", "GITHUB_TOKEN",
)


def check_tests(report: Report) -> None:
    """Runs the regression suite. Deliberately in a subprocess: importing
    pytest into this process would let a test's own monkeypatching leak into
    the repairs below, and it is the only way to hand the suite a different
    environment than this process has."""
    offline_env = {k: v for k, v in os.environ.items() if k not in CREDENTIAL_ENV_VARS}
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "-q", "--no-header"],
        cwd=REPO_ROOT, capture_output=True, text=True, env=offline_env,
    )
    summary = (result.stdout.strip().splitlines() or ["no output"])[-1]
    if result.returncode == 0:
        report.note(f"Regression suite passed — {summary}")
    else:
        report.problem(f"Regression suite FAILED — {summary}")
        # The failing test names, not the whole log: the log is in the run.
        failures = [ln for ln in result.stdout.splitlines()
                    if ln.startswith("FAILED") or ln.startswith("ERROR")]
        for line in failures[:20]:
            report.detail(line)


def check_youtube_token(report: Report) -> None:
    """Probes the stored refresh token. One token request, no quota units.

    This is the check with the best ratio in the whole file: the OAuth consent
    screen's testing mode expires a refresh token after 7 days, and when it
    does, every scheduled upload fails and nothing publishes until a human
    re-mints it. Catching that on a Friday morning beats discovering it from a
    week of red runs."""
    if not config.YT_REFRESH_TOKEN:
        report.problem(
            "YouTube token: not configured in this environment — the channel "
            "could not be verified, so this week is not an all-clear.")
        return
    ok, why = upload.holds_scope(upload.UPLOAD_SCOPE)
    if ok:
        report.note("YouTube token: valid, holds the upload scope")
        return
    if "invalid_grant" in why:
        report.problem(
            "YouTube token is EXPIRED OR REVOKED — no run will publish anything "
            "until it is replaced. Re-run get_refresh_token.py and update the "
            "YT_REFRESH_TOKEN secret. Switching the Google OAuth consent screen "
            f"from Testing to In production stops it recurring. ({why})"
        )
    else:
        report.problem(f"YouTube token could not be verified: {why}")


def reconcile_quota(report: Report) -> None:
    """Books any upload the ledger missed. Idempotent — record_upload is keyed
    by video id."""
    try:
        added = quota.reconcile_uploads(store.all_records())
    except Exception as e:  # noqa: BLE001 - a repair must not kill the report
        report.problem(f"Quota reconciliation failed: {type(e).__name__}: {e}")
        return
    if added:
        report.note(f"Quota ledger: booked {added} upload(s) it had missed (repaired)")
    else:
        report.note("Quota ledger: already agreed with the video records")
    report.note(f"Quota today: {quota.uploads_today()}/{quota.UPLOADS_PER_DAY_CAP} "
                f"upload slots, {quota.units_used_today()}/{quota.DAILY_CAP} Data API units")


def check_orphan_records(report: Report) -> None:
    """Local records plus recent channel uploads with no local record.

    An unmatched channel upload is not automatically called an agent orphan:
    it could be a deliberate manual upload. Surface it for review without
    writing a false record or silently declaring the channel healthy.
    """
    try:
        records = store.all_records()
    except Exception as e:  # noqa: BLE001
        report.problem(f"Could not read video records: {type(e).__name__}: {e}")
        return
    missing_id = [r for r in records if not r.get("video_id")]
    if missing_id:
        report.problem(f"{len(missing_id)} video record(s) have no video_id — "
                       "see scripts/backfill_orphan_records.py")
    else:
        report.note(f"Video records: {len(records)} tracked, all have a video id")

    if not config.YT_REFRESH_TOKEN:
        report.problem("Channel orphan check: unavailable without a YouTube token")
        return
    known = {str(record.get("video_id") or "") for record in records}
    earliest = min((str(record.get("uploaded_at") or "") for record in records
                    if record.get("uploaded_at")), default="")
    try:
        live = youtube_stats.fetch_recent_uploads(limit=100)
    except Exception as exc:  # noqa: BLE001 - unavailable is not all clear
        report.problem(f"Channel orphan check could not read recent uploads: "
                       f"{type(exc).__name__}: {exc}")
        return
    unmatched = [video for video in live
                 if video.get("video_id") not in known
                 and (not earliest or str(video.get("published_at") or "") >= earliest)]
    if not unmatched:
        report.note("Channel uploads: recent videos all have local records")
        return
    report.problem(
        f"{len(unmatched)} recent channel upload(s) have no local record. "
        "They may be manual uploads; inspect before backfilling anything.")
    for video in unmatched[:10]:
        report.detail(f"{video.get('published_at', '?')} {video.get('video_id', '?')} — "
                      f"{video.get('title', '')[:100]}")


def check_parked_videos(report: Report) -> None:
    """Local and retained-Action parked upload recovery bundles."""
    parked_dir = upload.PENDING_DIR
    parked = []
    if os.path.isdir(parked_dir):
        parked = [f for f in os.listdir(parked_dir)
                  if f.endswith(".json") and not f.endswith(".claimed.json")
                  and not f.startswith("replacement-")]
    if parked:
        report.problem(
            f"{len(parked)} rendered video(s) are parked and unpublished — "
            "publish them with scripts/publish_parked.py")
    else:
        report.note("Parked videos: none waiting locally")

    try:
        from agent import ci_status
        artifacts = ci_status.recent_recovery_artifacts()
    except Exception as exc:  # noqa: BLE001
        report.problem(f"Parked-upload artifact check failed: {type(exc).__name__}: {exc}")
        return
    if not artifacts.get("available"):
        report.problem("Parked-upload artifact check could not read GitHub Actions "
                       f"({artifacts.get('error', 'no Actions credentials')})")
        return
    retained = artifacts.get("artifacts") or []
    if retained:
        report.problem(
            f"{len(retained)} retained recovery artifact(s) may contain parked "
            "uploads or incomplete replacements; inspect them before cleanup.")
        for artifact in retained[:10]:
            report.detail(f"{artifact.get('name')} (created {artifact.get('created_at', '?')}, "
                          f"expires {artifact.get('expires_at', '?')})")
    else:
        report.note("Parked-upload artifacts: none retained in Actions")


def check_replacement_journals(report: Report) -> None:
    """A receipt means a replacement did not finish as one transaction."""
    pending = upload.PENDING_DIR
    if not os.path.isdir(pending):
        report.note("Replacement journals: none waiting locally")
        return
    receipts = sorted(name for name in os.listdir(pending)
                      if name.startswith("replacement-") and name.endswith(".json"))
    if not receipts:
        report.note("Replacement journals: none waiting locally")
        return
    report.problem(
        f"{len(receipts)} replacement transaction receipt(s) need reconciliation; "
        "do not rerun those replacements blindly.")
    for name in receipts[:10]:
        report.detail(name)


def check_recent_failures(report: Report) -> None:
    """Recent failed scheduled runs, from the Actions API. Uses the job-scoped
    GITHUB_TOKEN, so there is no extra secret to manage."""
    if not os.getenv("GITHUB_TOKEN") or not os.getenv("GITHUB_REPOSITORY"):
        report.problem("Recent runs: no Actions credentials in this environment — "
                       "cannot verify failures")
        return
    try:
        from agent import ci_status
        result = ci_status.recent_failures()
    except Exception as e:  # noqa: BLE001 - never let the status fetch kill the report
        report.problem(f"Recent runs: could not be read ({type(e).__name__}: {e})")
        return
    if not result.get("available"):
        # Deliberately not reported as "no failures": recent_failures returns
        # available=False when it could not look, which is not the same answer.
        report.problem("Recent runs: could not be read "
                       f"({result.get('error', 'no Actions credentials')})")
        return
    failures = result.get("failures") or []
    if not failures:
        report.note("Recent runs: no failures in the lookback window")
        return
    cutoff = datetime.now(timezone.utc).timestamp() - FAILURE_LOOKBACK_DAYS * 86400
    recent = []
    for failure in failures:
        stamp = failure.get("created_at") or ""
        try:
            when = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc).timestamp()
        except (TypeError, ValueError):
            when = cutoff + 1  # undateable, keep it rather than hide it
        if when >= cutoff:
            recent.append(failure)
    if not recent:
        report.note(f"Recent runs: no failures in the last {FAILURE_LOOKBACK_DAYS} days")
        return
    report.problem(f"{len(recent)} scheduled run(s) failed in the last "
                   f"{FAILURE_LOOKBACK_DAYS} days:")
    for failure in recent[:10]:
        report.detail(
            f"{failure.get('created_at', '?')} {failure.get('workflow', '?')}: "
            f"{(failure.get('error') or 'no error line captured')[:160]}")


def check_story_packet(report: Report) -> None:
    """The single most important thing this job now checks.

    Stories are written a week ahead, so the channel can be perfectly healthy
    today and out of content on Thursday, and nothing else in this repo would
    say so. Three separate ways that goes wrong, all reported here:

      * the packet is missing or invalid  -> every remaining slot fails
      * the packet is nearly used up      -> the weekly task did not run, and
                                             the channel goes dark when it
                                             empties
      * the packet is running late        -> slots are being missed and the
                                             queue is falling behind
    """
    try:
        current = packet.load_packet()
    except packet.PacketError as e:
        report.problem(f"Story packet: {e}")
        return

    prior = sorted(set(history.published_subjects())
                   | set(packet.published_story_subjects()))
    problems = packet.validate_packet(current, published_subjects=prior)
    if problems:
        report.problem(
            f"Story packet {current.get('packet_id')} has {len(problems)} "
            f"validation problem(s) — every slot until it is fixed will publish "
            f"nothing. First: {problems[0]}")
        for extra in problems[1:6]:
            report.note(f"    - {extra}")
        return

    duplicate_slots = packet.duplicate_published_slots()
    if duplicate_slots:
        report.problem(
            f"{len(duplicate_slots)} publishing slot(s) have more than one live "
            "video in the story ledger; inspect scripts/reconcile_story_slots.py "
            "before deleting anything.")
        for slot, entries in sorted(duplicate_slots.items())[:5]:
            report.detail(slot + ": " + ", ".join(
                f"{entry.get('video_id')} ({entry.get('story_id')})" for entry in entries))

    entries = packet.stories(current)
    remaining = [s for s in entries if packet.status_of(s) in packet.SELECTABLE]
    published = [s for s in entries if packet.status_of(s) == "published"]
    failed = [s for s in entries if packet.status_of(s) == "failed"]

    # "Days of runway" is the number a human actually needs: at four slots a
    # day, ten stories left is two and a half days.
    days_left = len(remaining) / cadence.SLOTS_PER_DAY
    report.note(f"Story packet {current.get('packet_id')}: {len(entries)} stories, "
                f"{len(published)} published, {len(remaining)} left "
                f"({days_left:.1f} days of runway at {cadence.SLOTS_PER_DAY}/day).")

    if not remaining:
        report.problem(
            "The story packet is empty — every story has been used. The next "
            "scheduled slot will publish nothing until the weekly Claude story "
            "task delivers a fresh week.")
    elif days_left < 1:
        report.problem(
            f"Less than a day of stories left ({len(remaining)}). The weekly "
            "Claude story task has not delivered a fresh week.")

    if failed:
        report.note(f"    {len(failed)} story/stories were retired after "
                    f"{packet.MAX_ATTEMPTS} failed attempts: "
                    + ", ".join(s.get("story_id", "?") for s in failed[:5]))

    behind = packet.due_stories(current)
    if len(behind) > 1:
        # More than one story due at once means slots have been missed: the
        # queue drains one per run.
        report.problem(
            f"{len(behind)} stories are past their slot and still unpublished — "
            "the queue is behind. Check the recent run failures above.")


def rebuild_dashboard(report: Report) -> None:
    try:
        dashboard.build()
    except Exception as e:  # noqa: BLE001
        report.problem(f"Dashboard rebuild failed: {type(e).__name__}: {e}")
        return
    report.note("Dashboard: rebuilt from current data")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-tests", action="store_true",
                        help="skip the regression suite (it runs separately in CI)")
    parser.add_argument("--out", help="write the report text to this file too")
    args = parser.parse_args()

    report = Report()
    report.note(f"Weekly health check — {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC")
    report.note(f"Niche: {config.NICHE}")

    if not args.skip_tests:
        check_tests(report)
    check_youtube_token(report)
    reconcile_quota(report)
    check_orphan_records(report)
    check_parked_videos(report)
    check_replacement_journals(report)
    check_recent_failures(report)
    check_story_packet(report)
    rebuild_dashboard(report)

    if report.blocking:
        report.note("")
        report.note(f"{len(report.blocking)} item(s) need a human.")
    else:
        report.note("")
        report.note("All clear — nothing needs a human this week.")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(report.text())

    # Machine-readable for the workflow's own step outputs.
    print("::WEEKLY_RESULT::" + json.dumps({"blocking": len(report.blocking)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
