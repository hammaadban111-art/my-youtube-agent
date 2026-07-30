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
