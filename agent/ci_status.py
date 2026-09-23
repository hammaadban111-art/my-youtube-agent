"""
Pulls recent GitHub Actions failures for the dashboard's Errors section.

Runs from inside the weekly dashboard build, using the job-scoped
GITHUB_TOKEN Actions already provides — no extra secret to manage. This is
not real-time monitoring: it refreshes weekly. True polling would mean a
workflow run every couple of minutes, which blows through the free CI minute
budget fast.

Never raises: a status-fetch failure must not break the dashboard build
it's decorating.
"""
import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

API_BASE = "https://api.github.com"
USER_AGENT = "faceless-youtube-agent/1.0"
WORKFLOWS = ["daily.yml", "followup.yml"]
# Four daily slots over the relevant eight-day packet window is already 32
# runs, so a 15-run lookback can hide an entire outage.
LOOKBACK_PER_WORKFLOW = 100
FAILURE_LOOKBACK_DAYS = 8
MAX_WORKFLOW_RUN_PAGES = 10
MAX_ERROR_CHARS = 300

# Matches the raised-exception line rather than GitHub's own generic
# "##[error]Process completed with exit code 1" — the former is what's actually
# diagnosable.
ERROR_RE = re.compile(r"[A-Za-z_.]{2,60}Error(?::.{0,250}|\b.{0,250})")


def _repo() -> str | None:
    return os.getenv("GITHUB_REPOSITORY")


def _token() -> str | None:
    return os.getenv("GITHUB_TOKEN")


def _api_get(path: str) -> dict:
    req = urllib.request.Request(
        f"{API_BASE}{path}",
        headers={
            "Authorization": f"Bearer {_token()}",
            "Accept": "application/vnd.github+json",
            "User-Agent": USER_AGENT,
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def _workflow_runs(workflow: str, *, created_since: str | None = None,
                   limit: int | None = None) -> list[dict]:
    """Fetch the pages needed for an honest window, without duplicates.

    GitHub returns only the first 100 runs by default.  That undercounted CI
    minutes once daily and follow-up together crossed 100 runs in a month.
    """
    runs, seen = [], set()
    for page in range(1, MAX_WORKFLOW_RUN_PAGES + 1):
        query = f"?per_page=100&page={page}"
        if created_since:
            query += f"&created=>={created_since}"
        data = _api_get(f"/repos/{_repo()}/actions/workflows/{workflow}/runs{query}")
        page_runs = data.get("workflow_runs") or []
        new = 0
        for run in page_runs:
            run_id = run.get("id")
            key = run_id if run_id is not None else (run.get("html_url"), run.get("created_at"))
            if key in seen:
                continue
            seen.add(key)
            runs.append(run)
            new += 1
            if limit is not None and len(runs) >= limit:
                return runs
        if len(page_runs) < 100 or not new:
            break
    return runs


def _repo_runs(*, created_since: str | None = None) -> list[dict]:
    """Every workflow run in the repository since `created_since`, all
    workflows together, paged the same way as _workflow_runs()."""
    runs, seen = [], set()
    for page in range(1, MAX_WORKFLOW_RUN_PAGES + 1):
        query = f"?per_page=100&page={page}"
        if created_since:
            query += f"&created=>={created_since}"
        data = _api_get(f"/repos/{_repo()}/actions/runs{query}")
        page_runs = data.get("workflow_runs") or []
        new = 0
        for run in page_runs:
            key = run.get("id")
            if key in seen:
                continue
            seen.add(key)
            runs.append(run)
            new += 1
        if len(page_runs) < 100 or not new:
            break
    return runs


class _StripAuthOnRedirect(urllib.request.HTTPRedirectHandler):
    """Drops the Authorization header when following a redirect.

    The job-logs endpoint does not return the log; it 302s to a pre-signed
    Azure blob URL. urllib re-sends the original Authorization header to that
    URL, and Azure rejects a request carrying both its own signature and a
    bearer token with "HTTP 401: Server failed to authenticate the request."
    That 401 was swallowed by the best-effort `except` in recent_failures(),
    so EVERY incident on the dashboard read "Unknown error - see run logs"
    regardless of what had actually gone wrong."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None:
            new.remove_header("Authorization")
        return new


def _fetch_job_log(job_id: int) -> str:
    req = urllib.request.Request(
        f"{API_BASE}/repos/{_repo()}/actions/jobs/{job_id}/logs",
        headers={"Authorization": f"Bearer {_token()}", "User-Agent": USER_AGENT},
    )
    opener = urllib.request.build_opener(_StripAuthOnRedirect)
    with opener.open(req, timeout=30) as resp:
        return resp.read().decode(errors="replace")


# Failures this project has actually had, newest cause first. Each entry is
# (marker found in the log, plain-English summary). A raw traceback line is
# accurate but tells a non-engineer nothing; these say what broke and what it
# means. Order matters - the first match wins, so specific beats generic.
KNOWN_FAILURES = (
    ("invalid_grant: Token has been expired or revoked",
     "The YouTube login expired. Re-run get_refresh_token.py and update the "
     "YT_REFRESH_TOKEN secret. (Google expires these every 7 days while the "
     "OAuth app is in Testing.)"),
    ("invalid_client",
     "YouTube rejected the app credentials — YT_CLIENT_ID / YT_CLIENT_SECRET "
     "do not match the token."),
    ("invalid_scope",
     "The YouTube token is missing a permission the run needed."),
    ("quotaExceeded",
     "YouTube's daily API quota ran out. It resets at midnight Pacific."),
    ("You must edit all merge conflicts",
     "Two runs overlapped and both saved video data, so the commit could not "
     "be merged. The video itself uploaded fine — only its record was at risk."),
    ("CONFLICT (content)",
     "Two runs overlapped and both saved video data, so the commit could not "
     "be merged. The video itself uploaded fine — only its record was at risk."),
    ("[rejected]",
     "Could not push the run's records — the branch moved underneath it, "
     "usually because another run committed first."),
    # HISTORICAL. Gemini was removed from the pipeline on 2026-09-08, but the
    # dashboard still reads run logs from before that date and twenty-eight
    # runs between 2026-08-24 and 2026-09-04 died on exactly this. Deleting
    # these entries would turn every one of those into "Unknown error".
    #
    # Matches the RAISED error only. The old retry wrapper printed
    # "503 UNAVAILABLE" on every attempt including ones that then succeeded —
    # matching that would tell a reader "no video was published" about a run
    # whose video is live and which actually died later, on the conflict
    # entries above. So these sit below the post-upload causes.
    ("genai.errors.ServerError: 503",
     "Google's AI model was overloaded and still refusing after several minutes "
     "of retries, so the run stopped before rendering. No video was published "
     "for this slot. This is a temporary problem on Google's side, not a fault "
     "in the channel."),
    ("This model is currently experiencing high demand",
     "Google's AI model was overloaded and still refusing after several minutes "
     "of retries, so the run stopped before rendering. No video was published "
     "for this slot. This is a temporary problem on Google's side, not a fault "
     "in the channel."),
    ("Upload blocked to protect distribution",
     "Skipped on purpose: too many uploads in the last 24h, so the upload-pace "
     "guardrail stopped this run before it rendered anything."),
    ("Not enough YouTube quota left today",
     "Skipped on purpose: not enough YouTube quota left today to publish, so "
     "the run stopped before rendering."),
    ("No story packet at",
     "The weekly story packet is missing from the repository, so this slot had "
     "nothing to publish. The weekly Claude task writes it; re-run that task. "
     "Nothing was invented to fill the gap, on purpose."),
    ("The story packet is exhausted",
     "Every story in the current weekly packet has been used, and no fresh "
     "week has been delivered. Re-run the weekly Claude story task. No video "
     "was published for this slot."),
    ("story packet has",
     "The weekly story packet failed its validation checks, so the run "
     "stopped before rendering. The reasons are listed in the run log; the "
     "packet needs regenerating or fixing."),
    ("No story is due yet",
     "This run started outside every planned publishing slot, so there was no "
     "story for it to publish. Normal for a manual run at an odd time."),
    ("Script generation failed validation twice",
     "The script writer could not produce a script that passed the quality "
     "checks, twice in a row, so the run stopped before rendering. No video "
     "was published for this slot."),
    ("Visual query regeneration failed validation twice",
     "Could not rewrite the footage search terms for a re-render, so it "
     "stopped rather than reuse the old ones."),
    ("No Pexels results",
     "Could not find usable stock footage for a segment."),
    ("No space left on device",
     "The runner ran out of disk space while rendering."),
)


def _extract_error(log_text: str, step: str = None) -> str:
    """A human-readable reason this run failed.

    Prefers a known cause in plain English, then the raised exception, then
    GitHub's own error line. The failing step is prefixed when known, since
    "Persist topic history and video record" vs "Run agent" is the difference
    between a video that published and one that never existed."""
    prefix = f"{step}: " if step else ""

    for marker, summary in KNOWN_FAILURES:
        if marker in log_text:
            return (prefix + summary)[:MAX_ERROR_CHARS]

    for line in reversed(log_text.splitlines()):
        # Logs from before 2026-09-22 carry the old email alerter's line,
        # which named the exception class with no message ("[notify] ... Run
        # failed: RuntimeError") AFTER the traceback, so a reverse scan hit it
        # first and reported a bare "RuntimeError" while the real cause sat
        # five lines above. Those runs stay in the lookback window for a while.
        if "[notify]" in line:
            continue
        m = ERROR_RE.search(line)
        if m:
            return (prefix + m.group(0).strip())[:MAX_ERROR_CHARS]

    for line in reversed(log_text.splitlines()):
        if "##[error]" in line:
            detail = line.split("##[error]", 1)[1].strip()
            # GitHub's generic exit-code line on its own is not a cause; at
            # least say which step produced it.
            return (prefix + detail)[:MAX_ERROR_CHARS]

    return (prefix or "") + "Unknown error — see run logs."


def _cancelled_reason(run_id: int, workflow: str = "daily.yml") -> tuple[str, str | None]:
    """Explains a cancelled run without needing a log fetch, returning
    (reason, cancelled_step). Only daily.yml publishes, so the "no video"
    clause is wrong on a follow-up run and is left off it."""
    # A run evicted from the concurrency queue never started and has no log to
    # read, so the whole diagnosis has to come from the jobs payload.
    slot_lost = (" No video was published for this slot."
                 if workflow == "daily.yml" else "")
    try:
        data = _api_get(f"/repos/{_repo()}/actions/runs/{run_id}/jobs")
        jobs = data.get("jobs")
        if jobs is not None and len(jobs) == 0:
            return (
                "This run was cancelled before it even started, because an "
                "earlier run was still holding the shared lock." + slot_lost,
                None
            )
        for job in (jobs or []):
            if job.get("conclusion") == "cancelled":
                step = next((s["name"] for s in job.get("steps", [])
                             if s.get("conclusion") == "cancelled"), "")
                started = job.get("started_at")
                completed = job.get("completed_at")
                if started and completed:
                    try:
                        start_ts = datetime.fromisoformat(started.replace("Z", "+00:00"))
                        comp_ts = datetime.fromisoformat(completed.replace("Z", "+00:00"))
                        if (comp_ts - start_ts).total_seconds() >= 5.5 * 3600:
                            reason = "The run hung and GitHub killed it after 6 hours."
                            if step == "Run agent":
                                reason += " No video was published for this slot."
                            return (reason, step or None)
                    except ValueError:
                        pass
                return ("This run was cancelled.", step or None)
        return ("This run was cancelled.", None)
    except Exception:
        return ("This run was cancelled.", None)


def _failed_job(run_id: int) -> tuple[int, str] | None:
    """(job id, failing step name) for the first failed job, or None. The step
    name is half the diagnosis: a failure in "Run agent" means no video was
    ever published, while one in "Persist topic history and video record"
    means the video IS live and only its bookkeeping broke."""
    data = _api_get(f"/repos/{_repo()}/actions/runs/{run_id}/jobs")
    for job in data.get("jobs", []):
        if job.get("conclusion") == "failure":
            step = next((s["name"] for s in job.get("steps", [])
                         if s.get("conclusion") == "failure"), "")
            return job["id"], step
    return None


def _upload_step_completed(run_id: int) -> bool:
    """Whether daily.yml's actual upload step completed successfully."""
    try:
        data = _api_get(f"/repos/{_repo()}/actions/runs/{run_id}/jobs")
        return any(
            step.get("name") == "Run agent" and step.get("conclusion") == "success"
            for job in data.get("jobs", [])
            for step in job.get("steps", [])
        )
    except Exception:
        return False


def _run_billable_ms(run_id: int) -> tuple[int, str]:
    """Returns (milliseconds, source) for one run.

    Prefers GitHub's own `billable.total_ms`, but that field reports 0 on this
    account (verified against runs whose `run_duration_ms` was 383000), so it
    falls back to the run duration and says which number it used rather than
    silently reporting zero minutes consumed.
    """
    timing = _api_get(f"/repos/{_repo()}/actions/runs/{run_id}/timing")
    billable = sum(b.get("total_ms", 0) for b in (timing.get("billable") or {}).values())
    if billable > 0:
        return billable, "billable"
    return timing.get("run_duration_ms", 0) or 0, "run_duration"


def ci_minutes_this_month(budget: int = 2000) -> dict:
    """Billable Actions minutes used since the 1st of the current month.

    Summed per run rather than read from the billing endpoint, which needs a
    token scope the in-workflow GITHUB_TOKEN doesn't carry. Only matters while
    the repo is private - public repos get unlimited Actions minutes.
    """
    if not _token() or not _repo():
        return {"available": False}

    month_start = datetime.now(timezone.utc).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0)
    try:
        total_ms, counted, per_workflow_ms, sources = 0, 0, {}, set()
        # EVERY workflow bills against the same 2,000 minutes, not just the
        # two that publish. Counting only daily.yml and followup.yml left the
        # regression suite (every code push), the weekly maintenance run, the
        # niche scan, the story-packet check and the editorial brief out of a
        # figure the page presents as the month's total.
        for run in _repo_runs(created_since=month_start.date().isoformat()):
            if run.get("status") != "completed":
                continue
            try:
                ms, source = _run_billable_ms(run["id"])
            except Exception:  # noqa: BLE001 - one run's timing is not critical
                continue
            workflow = os.path.basename(str(run.get("path") or "")) or str(
                run.get("name") or "unknown")
            per_workflow_ms[workflow] = per_workflow_ms.get(workflow, 0) + ms
            total_ms += ms
            sources.add(source)
            counted += 1
        per_workflow = {name: round(ms / 60000, 1)
                        for name, ms in sorted(per_workflow_ms.items())}

        used = round(total_ms / 60000, 1)
        return {
            "available": True,
            "used_minutes": used,
            "budget_minutes": budget,
            "percent_used": round(used / budget * 100, 1) if budget else None,
            "runs_counted": counted,
            "per_workflow_minutes": per_workflow,
            "measurement_source": "+".join(sorted(sources)) or "none",
            "month_start": month_start.date().isoformat(),
        }
    except Exception as e:  # noqa: BLE001 - reported, never fatal
        return {"available": False, "error": f"{type(e).__name__}: {e}"}


def last_run_status() -> dict:
    """The most recent upload run, and which step it actually reached — so a
    failure shows where the pipeline stopped rather than just that it failed."""
    if not _token() or not _repo():
        return {"available": False}
    try:
        data = _api_get(f"/repos/{_repo()}/actions/workflows/daily.yml/runs?per_page=1")
        runs = data.get("workflow_runs") or []
        if not runs:
            return {"available": False}
        run = runs[0]

        reached, failed_step = None, None
        jobs = _api_get(f"/repos/{_repo()}/actions/runs/{run['id']}/jobs").get("jobs", [])
        for job in jobs:
            for step in job.get("steps", []):
                if step.get("conclusion") == "success":
                    reached = step.get("name")
                elif step.get("conclusion") == "failure":
                    failed_step = step.get("name")
                    break
        return {
            "available": True,
            "status": run.get("status"),
            "conclusion": run.get("conclusion"),
            "event": run.get("event"),
            "created_at": run.get("created_at"),
            "url": run.get("html_url"),
            "last_successful_step": reached,
            "failed_step": failed_step,
        }
    except Exception as e:  # noqa: BLE001 - reported, never fatal
        return {"available": False, "error": f"{type(e).__name__}: {e}"}


def recent_failures() -> dict:
    """{"available": bool, "failures": [{workflow, run_id, url, created_at,
    error}, ...]} newest first. "available" is False when there's no token
    (e.g. running locally, outside Actions) rather than pretending there
    are zero failures."""
    if not _token() or not _repo():
        return {"available": False, "failures": []}

    try:
        failures = []
        for workflow in WORKFLOWS:
            since = (datetime.now(timezone.utc)
                     - timedelta(days=FAILURE_LOOKBACK_DAYS)).date().isoformat()
            runs = list(_workflow_runs(workflow, created_since=since,
                                       limit=LOOKBACK_PER_WORKFLOW))
            # A failure followed by a successful run of the same workflow has
            # recovered: still history worth listing, no longer something a
            # human must act on. The weekly report uses this to avoid turning
            # red over incidents that were already fixed.
            last_success = max((str(r.get("created_at") or "") for r in runs
                                if r.get("conclusion") == "success"), default="")
            for run in runs:
                conclusion = run.get("conclusion")
                if conclusion not in ("failure", "cancelled", "timed_out"):
                    continue
                error = "Unknown error — see run logs."
                if conclusion == "cancelled":
                    reason, step = _cancelled_reason(run["id"], workflow)
                    error = (f"{step}: {reason}" if step else reason)[:MAX_ERROR_CHARS]
                elif conclusion == "timed_out":
                    error = "This run exceeded its GitHub Actions timeout and was stopped."
                else:
                    found = _failed_job(run["id"])
                    if found is not None:
                        job_id, step = found
                        try:
                            error = _extract_error(_fetch_job_log(job_id), step)
                        except Exception as e:  # noqa: BLE001 - log fetch is best-effort
                            # Still say something useful. The log fetch silently
                            # 401'd for weeks and every incident read "Unknown
                            # error"; naming the step and the fetch failure makes
                            # that visible instead of invisible.
                            error = (f"{step}: failed (could not read the log: "
                                     f"{type(e).__name__})") if step else error
                failures.append({
                    "workflow": workflow,
                    "run_id": run["id"],
                    "url": run["html_url"],
                    "created_at": run["created_at"],
                    "error": error,
                    "recovered": last_success > str(run["created_at"] or ""),
                })
        failures.sort(key=lambda f: f["created_at"], reverse=True)
        return {"available": True, "failures": failures}
    except Exception as e:  # noqa: BLE001 - reported, never fatal
        return {"available": False, "failures": [], "error": f"{type(e).__name__}: {e}"}


def recent_recovery_artifacts(limit: int = 100) -> dict:
    """Retained upload/replacement recovery artifacts from recent Actions runs.

    An artifact is not proof that a video is missing — it can contain a bundle
    that was later reconciled — but treating its existence as nothing is worse:
    it is the only surviving copy when a GitHub runner is wiped.  Weekly health
    reports it for a human to inspect rather than calling an unverified channel
    state "all clear".
    """
    if not _token() or not _repo():
        return {"available": False, "artifacts": []}
    try:
        artifacts, seen = [], set()
        for page in range(1, 3):
            data = _api_get(f"/repos/{_repo()}/actions/artifacts?per_page=100&page={page}")
            rows = data.get("artifacts") or []
            for artifact in rows:
                artifact_id = artifact.get("id")
                if artifact_id in seen:
                    continue
                seen.add(artifact_id)
                name = str(artifact.get("name") or "")
                if artifact.get("expired") or not name.startswith(("unuploaded-video-", "failed-replace-")):
                    continue
                artifacts.append({
                    "id": artifact_id,
                    "name": name,
                    "created_at": artifact.get("created_at"),
                    "expires_at": artifact.get("expires_at"),
                    "size_in_bytes": artifact.get("size_in_bytes"),
                })
                if len(artifacts) >= limit:
                    return {"available": True, "artifacts": artifacts}
            if len(rows) < 100:
                break
        return {"available": True, "artifacts": artifacts}
    except Exception as e:  # noqa: BLE001 - status is best-effort, never fatal
        return {"available": False, "artifacts": [], "error": f"{type(e).__name__}: {e}"}


def recent_upload_runs(limit: int = 12) -> dict:
    """Recent runs of the daily upload workflow.
    Returns {"available": bool, "runs": [...]}, newest first.
    Each run dict includes an 'error' in plain English if it failed."""
    if not _token() or not _repo():
        return {"available": False, "runs": []}

    try:
        runs = []
        for run in _workflow_runs("daily.yml", limit=limit):
            entry = {
                "run_id": run["id"],
                "url": run["html_url"],
                "started_at": run["created_at"],
                "finished_at": run.get("updated_at") if run.get("status") == "completed" else None,
                "status": run.get("status"),
                "conclusion": run.get("conclusion"),
                "event": run.get("event"),
                "upload_completed": _upload_step_completed(run["id"]),
            }
            if run.get("conclusion") and run.get("conclusion") != "success":
                error = "Unknown error — see run logs."
                conclusion = run.get("conclusion")
                if conclusion == "cancelled":
                    reason, step = _cancelled_reason(run["id"])
                    if step:
                        entry["failed_step"] = step
                    error = (f"{step}: {reason}" if step else reason)[:MAX_ERROR_CHARS]
                elif conclusion == "timed_out":
                    error = "This run exceeded its GitHub Actions timeout and was stopped."
                else:
                    found = _failed_job(run["id"])
                    if found is not None:
                        job_id, step = found
                        # The failing step is what tells a reader whether a video
                        # exists. "Run agent" failing means nothing was published;
                        # anything LATER failing means the upload already happened
                        # and only the bookkeeping broke - which is how three
                        # videos ended up live with no record on 08-03/05/09.
                        entry["failed_step"] = step
                        try:
                            error = _extract_error(_fetch_job_log(job_id), step)
                        except Exception as e:  # noqa: BLE001 - log fetch is best-effort
                            error = (f"{step}: failed (could not read the log: "
                                     f"{type(e).__name__})") if step else error
                entry["error"] = error
            runs.append(entry)
        runs.sort(key=lambda r: r["started_at"], reverse=True)
        return {"available": True, "runs": runs}
    except Exception as e:  # noqa: BLE001 - reported, never fatal
        return {"available": False, "runs": [], "error": f"{type(e).__name__}: {e}"}
