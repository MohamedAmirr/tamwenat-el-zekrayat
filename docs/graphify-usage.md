# Graphify usage

`graphify-out/` is a local, git-ignored knowledge graph of the repo (the `marketplace_integrations` app and its Talabat, Shopify and Noon code, plus `docs/`). Scope comes from `.graphifyignore`. It is a map, not the source: it saves tokens on discovery, but behavior claims must be checked against the source.

## When to query (before reading or grepping many files)

| Need | Command |
|---|---|
| Where does X live / how do A and B relate / architecture question | `graphify query "<question>"` |
| How does A reach B (call/dependency chain) | `graphify path "<A>" "<B>"` |
| What is this concept/module | `graphify explain "<concept>"` |
| What breaks if I change this symbol (impact analysis, review scope) | `graphify affected "<symbol>"` |
| Broad architecture review | read `graphify-out/GRAPH_REPORT.md` |

Orientation order for a new task: skim `graphify-out/GRAPH_REPORT.md` (god nodes, communities, suggested questions) to find the relevant community, then `graphify query` inside it, then Read only files from that cluster.

Query first for discovery, then Read the specific source files it points to. Skip the graph when the exact file/symbol is already known or for a few-line edit.

Rules:
- Never Read `graph.json`, `graph.html` or `cache/` raw; use the CLI.
- INFERRED/AMBIGUOUS edges can be wrong. If graph and source disagree, trust source and run `graphify update .`.
- The graph is a snapshot and can be stale (other agents/tools edit files too). If a result looks wrong or a symbol is missing, update, then re-query.

## When to update

| Trigger | Action | Who |
|---|---|---|
| Commit or branch switch | Automatic: git hooks (`post-commit`, `post-checkout`) rebuild the code graph (AST only) | hooks |
| Code edited (py/js) and you will query or hand off for review/test afterwards | `graphify update .` | agent that edited (`frappe-dev`, `frappe-test`) |
| A query returns stale/missing results | `graphify update .`, re-query | any agent |
| `docs/` changed materially, or new doc added | `graphify update .` (code part only), then tell the user to run `/graphify . --update` (semantic re-extract; LLM cost, user-triggered) | agent that edited docs |
| Merge/pull brought in large changes | `graphify update .` | anyone |

Optional while editing heavily: `graphify watch .` rebuilds on file changes (long-running; user starts it, agents never do). The `graph.json` git merge driver (`graphify merge-driver`) is not registered and not needed: `graphify-out/` is git-ignored here, so each machine rebuilds locally.

`graphify update .` is cheap (AST only, no LLM). Skip silently if `graphify` is not installed or `graphify-out/graph.json` is missing. Never run `/graphify . --update` unprompted.

## Per-agent expectations

- `feature-planner`: query to find existing modules/prior art before planning. Never updates.
- `frappe-dev`: query before exploring; `graphify affected` before changing a shared symbol; update after edits.
- `frappe-test`: query to find the code under test and its callers; update after adding/editing tests.
- `frappe-review`: `graphify affected` on each changed symbol to find untouched callers/duplicates; then Read source to confirm. Read-only, so does not update (report if graph looked stale).
- `frappe-ops`, `git-ops`: no queries needed. Hooks handle refresh after commits/checkouts.
