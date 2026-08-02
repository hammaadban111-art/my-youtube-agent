Records moved here are NOT deleted, just excluded from `store.all_records()`
(which only scans `data/videos/`). Each entry below explains why.

- **5Z7wifCabEk** — "The Night Something Walked Across England". Recorded by
  the pipeline as uploaded, but `videos.list` against the channel's own
  token returns nothing for this ID (checked 2026-08-02): it never actually
  went live on YouTube. `measurement.actual_views` was already null, so it
  never fed the prediction baseline — this move only stops it rendering as
  a dead link on the dashboard.
