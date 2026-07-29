"""
Email notifications via Gmail SMTP.

Both notification types render from the same video record as the dashboard,
so the two delivery channels can't drift out of sync.

Sending never raises: a mail failure must not fail a workflow whose real job
(uploading, or recording a measurement) already succeeded.
"""
import smtplib
import ssl
from email.message import EmailMessage
from . import config, predict

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465


def _send(subject: str, html: str, text: str) -> bool:
    if not (config.GMAIL_ADDRESS and config.GMAIL_APP_PASSWORD):
        print("[notify] Gmail credentials not set — skipping email.")
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = config.GMAIL_ADDRESS
    msg["To"] = config.NOTIFY_TO
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")

    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx, timeout=30) as server:
            server.login(config.GMAIL_ADDRESS, config.GMAIL_APP_PASSWORD)
            server.send_message(msg)
        print(f"[notify] Emailed: {subject}")
        return True
    except Exception as e:  # noqa: BLE001 - reported, never fatal
        print(f"[notify] Email failed ({type(e).__name__}: {e})")
        return False


def _grounding_line(record: dict) -> str:
    g = record.get("grounding", {})
    if g.get("status") != "checked":
        return f"Fact-check: {g.get('status', 'not run')}"
    return (f"Fact-check: {g.get('claims_checked', 0)} claims vs "
            f"{g.get('article', '?')} — {g.get('contradicted', 0)} contradicted")


def notify_upload(record: dict) -> bool:
    pred = record["prediction"]["predicted_views"]
    model = record["prediction"]["model_version"]
    title = record["title"]

    text = (
        f"Uploaded: {title}\n{record['url']}\n\n"
        f"Predicted views at 5h: {pred}  (model: {model})\n"
        f"Topic: {record.get('topic_subject', '-')}\n"
        f"{_grounding_line(record)}\n"
        f"Privacy: {record.get('privacy_status', '?')}\n"
    )
    html = f"""
    <div style="font-family:system-ui,-apple-system,Segoe UI,sans-serif;max-width:600px">
      <h2 style="margin:0 0 4px">Video uploaded</h2>
      <p style="margin:0 0 16px"><a href="{record['url']}">{title}</a></p>
      <table style="border-collapse:collapse;font-size:14px">
        <tr><td style="padding:4px 12px 4px 0;color:#666">Predicted views (5h)</td>
            <td style="padding:4px 0"><strong>{pred}</strong> <span style="color:#888">({model})</span></td></tr>
        <tr><td style="padding:4px 12px 4px 0;color:#666">Topic</td>
            <td style="padding:4px 0">{record.get('topic_subject', '-')}</td></tr>
        <tr><td style="padding:4px 12px 4px 0;color:#666">Fact-check</td>
            <td style="padding:4px 0">{_grounding_line(record)}</td></tr>
        <tr><td style="padding:4px 12px 4px 0;color:#666">Privacy</td>
            <td style="padding:4px 0">{record.get('privacy_status', '?')}</td></tr>
      </table>
    </div>"""
    return _send(f"[YT Agent] Uploaded: {title[:60]}", html, text)


def notify_followup(record: dict) -> bool:
    pred = record["prediction"].get("predicted_views")
    m = record["measurement"]
    actual, hours = m["actual_views"], m["hours_after_upload"]
    delta = actual - pred if pred is not None else None
    pct = round(abs(delta) / max(pred, 1) * 100) if delta is not None else None
    acc = predict.accuracy_summary()

    comments = record.get("comments", [])[:3]
    ctext = "\n".join(f"  - {c['author']}: {c['text'][:140]}" for c in comments) or "  (none yet)"
    chtml = "".join(
        f"<li style='margin-bottom:6px'><strong>{c['author']}</strong> "
        f"<span style='color:#888'>({c['likes']} likes)</span><br>{c['text'][:200]}</li>"
        for c in comments
    ) or "<li style='color:#888'>No comments yet</li>"

    text = (
        f"{record['title']}\n{record['url']}\n\n"
        f"Predicted: {pred}\nActual at {hours}h: {actual}\n"
        f"Difference: {delta:+d} ({pct}% off)\n"
        f"Likes: {m.get('likes')}   Comments: {m.get('comment_count')}\n\n"
        f"Rolling accuracy (last {acc['n']}): {acc['mean_abs_pct_error']}% mean error\n\n"
        f"Comments:\n{ctext}\n"
    )
    html = f"""
    <div style="font-family:system-ui,-apple-system,Segoe UI,sans-serif;max-width:600px">
      <h2 style="margin:0 0 4px">{hours}-hour check-in</h2>
      <p style="margin:0 0 16px"><a href="{record['url']}">{record['title']}</a></p>
      <table style="border-collapse:collapse;font-size:14px;margin-bottom:16px">
        <tr><td style="padding:4px 12px 4px 0;color:#666">Predicted</td>
            <td style="padding:4px 0">{pred}</td></tr>
        <tr><td style="padding:4px 12px 4px 0;color:#666">Actual</td>
            <td style="padding:4px 0"><strong>{actual}</strong></td></tr>
        <tr><td style="padding:4px 12px 4px 0;color:#666">Difference</td>
            <td style="padding:4px 0">{delta:+d} ({pct}% off)</td></tr>
        <tr><td style="padding:4px 12px 4px 0;color:#666">Likes / comments</td>
            <td style="padding:4px 0">{m.get('likes')} / {m.get('comment_count')}</td></tr>
        <tr><td style="padding:4px 12px 4px 0;color:#666">Rolling accuracy</td>
            <td style="padding:4px 0">{acc['mean_abs_pct_error']}% mean error (last {acc['n']})</td></tr>
      </table>
      <h3 style="margin:0 0 8px;font-size:15px">Comments</h3>
      <ul style="padding-left:18px;font-size:14px;margin:0">{chtml}</ul>
    </div>"""
    return _send(f"[YT Agent] {hours}h: {actual} views — {record['title'][:50]}", html, text)
