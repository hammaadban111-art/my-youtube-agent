"""
Generates the static dashboard published to GitHub Pages.

Self-contained: inline CSS, no external fonts, scripts or images, so it
renders identically on Pages with no network dependencies. Regenerated from
the records on disk on every run, so it always reflects current data.
"""
import html
import os
from . import ci_status, predict, store

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "..", "public")

PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>YouTube Agent — Performance</title>
<style>
  :root {{ color-scheme: light dark; --bg:#fff; --fg:#111; --muted:#666;
           --line:#e5e5e5; --card:#fafafa; --good:#0a7; --bad:#c33; --warn:#c80; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg:#111; --fg:#eee; --muted:#999; --line:#2a2a2a; --card:#1a1a1a; }}
  }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; padding:24px 16px; background:var(--bg); color:var(--fg);
         font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; }}
  .wrap {{ max-width:800px; margin:0 auto; }}
  h1 {{ font-size:22px; margin:0 0 4px; }}
  .sub {{ color:var(--muted); font-size:13px; margin-bottom:24px; }}
  .stats {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
            gap:12px; margin-bottom:20px; }}
  .stat {{ background:var(--card); border:1px solid var(--line);
           border-radius:8px; padding:14px; }}
  .stat .n {{ font-size:24px; font-weight:600; }}
  .stat .l {{ color:var(--muted); font-size:12px; text-transform:uppercase;
              letter-spacing:.04em; margin-top:2px; }}
  a {{ color:inherit; }}
  .good {{ color:var(--good); }} .bad {{ color:var(--bad); }} .warn {{ color:var(--warn); }}
  .pending {{ color:var(--muted); font-style:italic; }}
  .banner {{ background:var(--card); border:1px solid var(--line);
             border-left:3px solid var(--muted); border-radius:6px;
             padding:12px 14px; margin-bottom:20px; font-size:14px; }}
  h2.section {{ font-size:13px; text-transform:uppercase; letter-spacing:.04em;
                color:var(--muted); margin:28px 0 10px; }}
  .no-errors {{ background:var(--card); border:1px solid var(--line);
                border-left:3px solid var(--good); border-radius:6px;
                padding:10px 14px; margin-bottom:8px; font-size:14px; color:var(--good); }}
  .status-unavailable {{ background:var(--card); border:1px solid var(--line);
                border-left:3px solid var(--muted); border-radius:6px;
                padding:10px 14px; margin-bottom:8px; font-size:13px; color:var(--muted); }}
  .error-card {{ background:var(--card); border:1px solid var(--line);
                 border-left:3px solid var(--bad); border-radius:6px;
                 padding:10px 14px; margin-bottom:8px; font-size:13px; }}
  .error-card .w {{ font-weight:600; }}
  .error-card .t {{ color:var(--muted); font-size:12px; }}
  .error-card .m {{ margin-top:4px; font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
                     font-size:12.5px; word-break:break-word; }}
  .video-card {{ background:var(--card); border:1px solid var(--line);
                 border-radius:8px; padding:16px 18px; margin-bottom:14px; }}
  .video-card h3 {{ font-size:16px; margin:0 0 4px; }}
  .video-sub {{ color:var(--muted); font-size:12px; margin-bottom:12px; }}
  .video-grid {{ display:grid; grid-template-columns:1fr 1fr; gap:12px; margin-bottom:10px; }}
  .video-grid .k {{ text-transform:uppercase; font-size:11px; color:var(--muted);
                     letter-spacing:.04em; }}
  .video-grid .v {{ font-size:15px; font-weight:600; margin-top:2px; }}
  .video-meta {{ display:flex; gap:18px; flex-wrap:wrap; font-size:13px; color:var(--muted); }}
  .video-meta strong {{ color:var(--fg); font-weight:600; }}
  footer {{ margin-top:32px; color:var(--muted); font-size:12px; }}
</style></head><body><div class="wrap">
<h1>YouTube Agent — Performance</h1>
<div class="sub">Updated {updated} · auto-generated, do not edit by hand</div>
{banner}
<div class="stats">{stats}</div>
<h2 class="section">Errors</h2>
{errors}
<h2 class="section">Videos</h2>
{videos}
<footer>Predictions are measured {hours}h after upload. Errors section reflects the last
{lookback} runs of each workflow as of this page's build time — not live/real-time.</footer>
</div></body></html>
"""


def _stat(n, label):
    return f'<div class="stat"><div class="n">{n}</div><div class="l">{label}</div></div>'


def _errors_html(status: dict) -> str:
    if not status["available"]:
        return ('<div class="status-unavailable">CI status unavailable on this build '
                '(no GitHub token in this run context).</div>')

    failures = status["failures"]
    if not failures:
        return '<div class="no-errors">No failed runs in recent history.</div>'

    cards = []
    for f in failures:
        cards.append(
            '<div class="error-card">'
            f'<span class="w">{html.escape(f["workflow"])}</span> '
            f'<span class="t">{html.escape(f["created_at"][:16].replace("T", " "))} UTC · '
            f'<a href="{html.escape(f["url"])}">run {f["run_id"]}</a></span>'
            f'<div class="m">{html.escape(f["error"])}</div>'
            '</div>'
        )
    return "".join(cards)


def _fact_check(g: dict) -> str:
    if g.get("status") != "checked":
        return f'<span class="pending">{html.escape(str(g.get("status", "—")))}</span>'
    c = g.get("contradicted", 0)
    cls = "good" if c == 0 else "bad"
    return f'<span class="{cls}">{g.get("claims_checked", 0)} checked, {c} contradicted</span>'


def _resolution(g: dict) -> str:
    fsg = g.get("final_segment_grounded")
    if fsg is None:
        return '<span class="pending">— (predates this check)</span>'
    if g.get("final_segment_contradicted"):
        return '<span class="bad">contradicted</span>'
    if fsg:
        return '<span class="good">grounded</span>'
    return '<span class="warn">not grounded</span>'


def _video_card(n: int, r: dict) -> str:
    pred = r.get("prediction", {}).get("predicted_views")
    model = r.get("prediction", {}).get("model_version", "—")
    m = r.get("measurement", {})
    actual = m.get("actual_views")
    g = r.get("grounding", {})

    projected_v = f"{pred} views" if pred is not None else "—"
    projected_l = f"model: {html.escape(str(model))}"

    if actual is None:
        current_v = '<span class="pending">pending</span>'
        current_l = f"measured {store.MEASURE_AFTER_HOURS}h after upload"
        error_v = "—"
    else:
        current_v = f"{actual} views"
        bits = []
        if m.get("likes") is not None:
            bits.append(f"{m['likes']} likes")
        if m.get("comment_count") is not None:
            bits.append(f"{m['comment_count']} comments")
        current_l = " · ".join(bits) if bits else "&nbsp;"
        if pred:
            diff = actual - pred
            pct = round(abs(diff) / max(pred, 1) * 100)
            cls = "good" if diff >= 0 else "bad"
            error_v = f'<span class="{cls}">{diff:+d} ({pct}%)</span>'
        else:
            error_v = "—"

    return f"""<div class="video-card">
  <h3>Video {n} — {html.escape(r["title"])}</h3>
  <div class="video-sub">{html.escape(r["uploaded_at"][:16].replace("T", " "))} UTC ·
    <a href="{html.escape(r["url"])}">watch ↗</a></div>
  <div class="video-grid">
    <div class="block"><div class="k">Projected</div><div class="v">{projected_v}</div>
      <div class="video-sub">{projected_l}</div></div>
    <div class="block"><div class="k">Current</div><div class="v">{current_v}</div>
      <div class="video-sub">{current_l}</div></div>
  </div>
  <div class="video-meta">
    <span>Error: <strong>{error_v}</strong></span>
    <span>Fact-check: <strong>{_fact_check(g)}</strong></span>
    <span>Resolution: <strong>{_resolution(g)}</strong></span>
  </div>
</div>"""


def build(output_dir: str = None) -> str:
    from datetime import datetime, timezone

    output_dir = output_dir or OUTPUT_DIR
    records = store.all_records()  # oldest first — this IS the numbering order
    measured = [r for r in records if r["measurement"].get("actual_views") is not None]
    acc = predict.accuracy_summary()
    active, days = predict.self_improve_active()

    total_views = sum(r["measurement"]["actual_views"] for r in measured)
    stats = "".join([
        _stat(len(records), "videos"),
        _stat(total_views, "total views (5h)"),
        _stat(f"{acc['mean_abs_pct_error']}%" if acc["mean_abs_pct_error"] is not None else "—",
              "mean prediction error"),
        _stat(days, "days of history"),
    ])

    if active:
        banner = ('<div class="banner"><strong>Self-improving mode active.</strong> '
                  f"Predictions and topic selection are now informed by {len(measured)} "
                  "measured videos.</div>")
    else:
        remaining = max(0, predict.SELF_IMPROVE_MIN_DAYS - days)
        banner = ('<div class="banner"><strong>Collecting baseline data.</strong> '
                  f"Self-improving mode activates automatically after "
                  f"{predict.SELF_IMPROVE_MIN_DAYS} days of history "
                  f"({remaining} day(s) to go).</div>")

    # Numbered oldest-first (Video 1 = first ever upload), then displayed
    # newest-first so the freshest video is what you see without scrolling.
    numbered = list(enumerate(records, start=1))
    videos_html = "".join(_video_card(n, r) for n, r in reversed(numbered)) or (
        '<div class="video-card pending">No videos yet.</div>'
    )

    status = ci_status.recent_failures()

    page = PAGE.format(
        updated=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        banner=banner, stats=stats, hours=store.MEASURE_AFTER_HOURS,
        errors=_errors_html(status), videos=videos_html,
        lookback=ci_status.LOOKBACK_PER_WORKFLOW,
    )

    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, "index.html")
    with open(path, "w") as f:
        f.write(page)
    return path


if __name__ == "__main__":
    print(f"Dashboard written to {build()}")
