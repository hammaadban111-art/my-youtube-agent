"""
Pulls recent GitHub Actions failures for the dashboard's Errors section.

Runs from inside the pipeline itself (called by dashboard.build()), using
the job-scoped GITHUB_TOKEN Actions already provides — no extra secret to
manage. This is not real-time monitoring: it only refreshes when the
dashboard rebuilds, which is on every upload and every 5-hour follow-up
check (roughly every 1-3h at current volume). True polling would mean a
workflow run every couple of minutes, which blows through the free CI
minute budget fast — this piggybacks on runs that were happening anyway.

Never raises: a status-fetch failure must not break the dashboard build
it's decorating.
"""
import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone

API_BASE = "https://api.github.com"
USER_AGENT = "faceless-youtube-agent/1.0"
WORKFLOWS = ["daily.yml", "followup.yml"]
LOOKBACK_PER_WORKFLOW = 15
MAX_ERROR_CHARS = 300

# Matches the raised-exception line ("google.genai.errors.ServerError: 503
# UNAVAILABLE...") rather than GitHub's own generic "##[error]Process
# completed with exit code 1" — the former is what's actually diagnosable.
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


def _fetch_job_log(job_id: int) -> str:
    req = urllib.request.Request(
        f"{API_BASE}/repos/{_repo()}/actions/jobs/{job_id}/logs",
        headers={"Authorization": f"Bearer {_token()}", "User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode(errors="replace")


def _extract_error(log_text: str) -> str:
    for line in reversed(log_text.splitlines()):
        m = ERROR_RE.search(line)
        if m:
            return m.group(0).strip()[:MAX_ERROR_CHARS]
    for line in reversed(log_text.splitlines()):
        if "##[error]" in line:
            return line.split("##[error]", 1)[1].strip()[:MAX_ERROR_CHARS]
    return "Unknown error — see run logs."


def _failed_job_id(run_id: int) -> int | None:
    data = _api_get(f"/repos/{_repo()}/actions/runs/{run_id}/jobs")
    for job in data.get("jobs", []):
        if job.get("conclusion") == "failure":
            return job["id"]
    return None


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
        total_ms, counted, per_workflow, sources = 0, 0, {}, set()
        for workflow in WORKFLOWS:
            data = _api_get(
                f"/repos/{_repo()}/actions/workflows/{workflow}/runs"
                f"?per_page=100&created=>={month_start.date().isoformat()}"
            )
            wf_ms = 0
            for run in data.get("workflow_runs", []):
                if run.get("status") != "completed":
                    continue
                try:
                    ms, source = _run_billable_ms(run["id"])
                except Exception:  # noqa: BLE001 - one run's timing is not critical
                    continue
                wf_ms += ms
                sources.add(source)
                counted += 1
            per_workflow[workflow] = round(wf_ms / 60000, 1)
            total_ms += wf_ms

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
            data = _api_get(
                f"/repos/{_repo()}/actions/workflows/{workflow}/runs"
                f"?per_page={LOOKBACK_PER_WORKFLOW}"
            )
            for run in data.get("workflow_runs", []):
                if run.get("conclusion") != "failure":
                    continue
                error = "Unknown error — see run logs."
                job_id = _failed_job_id(run["id"])
                if job_id is not None:
                    try:
                        error = _extract_error(_fetch_job_log(job_id))
                    except Exception:  # noqa: BLE001 - log fetch is best-effort
                        pass
                failures.append({
                    "workflow": workflow,
                    "run_id": run["id"],
                    "url": run["html_url"],
                    "created_at": run["created_at"],
                    "error": error,
                })
        failures.sort(key=lambda f: f["created_at"], reverse=True)
        return {"available": True, "failures": failures}
    except Exception as e:  # noqa: BLE001 - reported, never fatal
        return {"available": False, "failures": [], "error": f"{type(e).__name__}: {e}"}
