"""
Generates the static dashboard published to GitHub Pages.

Self-contained: inline CSS, no external fonts, scripts or images, so it
renders identically on Pages with no network dependencies. Regenerated from
the records on disk on every run, so it always reflects current data.
"""
import html
import os
from . import predict, store

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "..", "public")

PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>YouTube Agent — Performance</title>
<style>
  :root {{ color-scheme: light dark; --bg:#fff; --fg:#111; --muted:#666;
           --line:#e5e5e5; --card:#fafafa; --good:#0a7; --bad:#c33; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg:#111; --fg:#eee; --muted:#999; --line:#2a2a2a; --card:#1a1a1a; }}
  }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; padding:24px 16px; background:var(--bg); color:var(--fg);
         font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; }}
  .wrap {{ max-width:1000px; margin:0 auto; }}
  h1 {{ font-size:22px; margin:0 0 4px; }}
  .sub {{ color:var(--muted); font-size:13px; margin-bottom:24px; }}
  .stats {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
            gap:12px; margin-bottom:28px; }}
  .stat {{ background:var(--card); border:1px solid var(--line);
           border-radius:8px; padding:14px; }}
  .stat .n {{ font-size:24px; font-weight:600; }}
  .stat .l {{ color:var(--muted); font-size:12px; text-transform:uppercase;
              letter-spacing:.04em; margin-top:2px; }}
  .scroll {{ overflow-x:auto; -webkit-overflow-scrolling:touch; }}
  table {{ border-collapse:collapse; width:100%; font-size:14px; min-width:720px; }}
  th,td {{ text-align:left; padding:9px 12px; border-bottom:1px solid var(--line);
           vertical-align:top; }}
  th {{ color:var(--muted); font-weight:600; font-size:12px;
        text-transform:uppercase; letter-spacing:.04em; }}
  a {{ color:inherit; }}
  .good {{ color:var(--good); }} .bad {{ color:var(--bad); }}
  .pending {{ color:var(--muted); font-style:italic; }}
  .banner {{ background:var(--card); border:1px solid var(--line);
             border-left:3px solid var(--muted); border-radius:6px;
             padding:12px 14px; margin-bottom:24px; font-size:14px; }}
  footer {{ margin-top:32px; color:var(--muted); font-size:12px; }}
</style></head><body><div class="wrap">
<h1>YouTube Agent — Performance</h1>
<div class="sub">Updated {updated} · auto-generated, do not edit by hand</div>
{banner}
<div class="stats">{stats}</div>
<div class="scroll"><table>
<thead><tr><th>Uploaded</th><th>Title</th><th>Topic</th><th>Predicted</th>
<th>Actual</th><th>Error</th><th>Fact-check</th></tr></thead>
<tbody>{rows}</tbody></table></div>
<footer>Predictions are measured {hours}h after upload.</footer>
</div></body></html>
"""


def _stat(n, label):
    return f'<div class="stat"><div class="n">{n}</div><div class="l">{label}</div></div>'


def _row(r):
    pred = r.get("prediction", {}).get("predicted_views")
    m = r.get("measurement", {})
    actual = m.get("actual_views")
    g = r.get("grounding", {})

    if actual is None:
        actual_cell = '<span class="pending">pending</span>'
        err_cell = "—"
    else:
        actual_cell = f"<strong>{actual}</strong>"
        if pred:
            diff = actual - pred
            pct = round(abs(diff) / max(pred, 1) * 100)
            err_cell = f'<span class="{"good" if diff >= 0 else "bad"}">{diff:+d} ({pct}%)</span>'
        else:
            err_cell = "—"

    if g.get("status") == "checked":
        c = g.get("contradicted", 0)
        fact = (f'<span class="good">{g.get("claims_checked", 0)} ok</span>' if c == 0
                else f'<span class="bad">{c} contradicted</span>')
    else:
        fact = f'<span class="pending">{html.escape(str(g.get("status", "—")))}</span>'

    return (
        f"<tr><td>{html.escape(r['uploaded_at'][:16].replace('T', ' '))}</td>"
        f'<td><a href="{html.escape(r["url"])}">{html.escape(r["title"][:70])}</a></td>'
        f"<td>{html.escape((r.get('topic_subject') or '—')[:40])}</td>"
        f"<td>{pred if pred is not None else '—'}</td>"
        f"<td>{actual_cell}</td><td>{err_cell}</td><td>{fact}</td></tr>"
    )


def build(output_dir: str = None) -> str:
    from datetime import datetime, timezone

    output_dir = output_dir or OUTPUT_DIR
    records = list(reversed(store.all_records()))
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

    page = PAGE.format(
        updated=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        banner=banner, stats=stats, hours=store.MEASURE_AFTER_HOURS,
        rows="".join(_row(r) for r in records)
             or '<tr><td colspan="7" class="pending">No videos yet.</td></tr>',
    )

    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, "index.html")
    with open(path, "w") as f:
        f.write(page)
    return path


if __name__ == "__main__":
    print(f"Dashboard written to {build()}")
