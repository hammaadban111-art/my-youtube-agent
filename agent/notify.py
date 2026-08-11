"""
Email notifications via Resend's HTTP API (resend.com), using only the
standard library. Deliberately no `requests` dependency: the follow-up
workflow's requirements file excludes it on purpose to stay a light, fast
job, and this module is imported from there (through predict.py) for the
self-improve approval email.

Never raises - a broken notification must not be able to fail the pipeline
or block an upload. Silently no-ops if the secrets aren't configured.
"""
import json
import os
import urllib.error
import urllib.request

RESEND_API_KEY = os.getenv("RESEND_API_KEY", "").strip()
NOTIFY_TO = os.getenv("NOTIFY_TO", "").strip()
NOTIFY_FROM = os.getenv("NOTIFY_FROM", "YouTube Agent <onboarding@resend.dev>").strip()


def send_email(subject: str, text: str) -> bool:
    """True only on a 2xx response from Resend."""
    if not RESEND_API_KEY or not NOTIFY_TO:
        print(f"[notify] RESEND_API_KEY or NOTIFY_TO not set - skipping email: {subject}")
        return False

    payload = json.dumps({
        "from": NOTIFY_FROM,
        "to": [NOTIFY_TO],
        "subject": subject,
        "text": text,
    }).encode("utf-8")

    request = urllib.request.Request(
        "https://api.resend.com/emails",
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type": "application/json",
            # urllib's default User-Agent ("Python-urllib/3.x") gets a flat
            # 403 from Resend's Cloudflare edge (error code 1010, a bot-
            # signature block) - confirmed 2026-08-02, curl with no UA
            # override was unaffected. Any normal-looking UA clears it.
            "User-Agent": "youtube-agent-dashboard/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as resp:
            ok = 200 <= resp.status < 300
            print(f"[notify] Resend responded {resp.status} for: {subject}")
            return ok
    except urllib.error.HTTPError as e:
        print(f"[notify] Resend HTTP error {e.code} for: {subject}: {e.read()[:200]}")
        return False
    except Exception as e:  # noqa: BLE001 - notification failures must not be fatal
        print(f"[notify] Failed to send email ({type(e).__name__}): {e}")
        return False


def alert(subject: str, text: str) -> bool:
    """Sends one failure notification, tagged so it is filterable in a mailbox.

    Until 2026-08-11 send_email had exactly one caller in the whole repo (the
    self-improve approval in predict.py), so nothing that BROKE ever emailed
    anyone - the 08-07 token expiry and the 08-09/10/11 conflict failures ran
    for four days unnoticed. This is the entry point the failure paths use.
    Never raises, for the same reason send_email doesn't."""
    return send_email(f"[youtube-agent] {subject}", text)
