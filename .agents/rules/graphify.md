---
trigger: always_on
description: Consult the graphify knowledge graph at graphify-out/ for codebase and architecture questions.
---

## graphify

This project has a graphify knowledge graph at graphify-out/.

Rules:
- For codebase or architecture questions, when `graphify-out/graph.json` exists, first run `graphify query "<question>"` (CLI) or `query_graph` (MCP). Use `graphify path "<A>" "<B>"` / `shortest_path` for relationships and `graphify explain "<concept>"` / `get_node` for focused concepts. These return a scoped subgraph, usually much smaller than `GRAPH_REPORT.md` or raw grep output.
- If graphify-out/wiki/index.md exists, navigate it instead of reading raw files
- Read graphify-out/GRAPH_REPORT.md only for broad architecture review or when query/path/explain do not surface enough context
- After completing any task that CHANGED CODE in this session, run `/graphify --update` (code AND docs) before ending the turn. Automatic — not only when asked. "Changed code" covers source, behaviour, workflow/config files, and docs describing the system.
- Skip it on turns that changed nothing (questions, explanations, reading, planning) — the update costs tokens and has nothing to record.
- Do NOT substitute the bare `graphify update .` CLI: it is AST-only and leaves docs and semantic edges stale, which is how the doc pass on this repo went stale for a whole phase. `graphify update` also misreports freshness — it can print "No code-graph topology changes detected" while `graph.json` does contain the new nodes, so don't trust the "Built from commit" line.
