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
| `docs/` changed materially, or new doc added | `graphify update .` (code part), then the semantic `/graphify . --update` before the PR is opened (see "Working order" below) | main session, before opening the PR |
| Merge/pull brought in large changes | `graphify update .` | anyone |

## Working order for every change (docs and graphify before the PR)

Before opening a PR, in this order:

1. Build the change, then review it (`frappe-review`) and fix findings, then test and lint it (`frappe-test`). Review comes before testing so tests run once on the reviewed code.
2. Update every doc the change affects in the same branch: the relevant `docs/` pages (for example `docs/marketplace-integrations/architecture.md`, `docs/shopify/README.md`), `docs/user-actions.md` rows (migrate, backfill, manual steps) and any issue or convention text that is now wrong.
3. Refresh the graph: `graphify update .` (code), then the semantic `/graphify . --update` when docs changed. For a long doc such as `architecture.md`, extract it in several line-range chunks so the whole file is read, not just the top.
4. Open the PR with `gh pr create --base dev`.

The graph is local and git-ignored, so the refresh is not part of the diff; it is a checklist step so the next session's queries match the merged code and docs. Do not leave the docs or graph refresh as a follow-up the user has to ask for.

Optional while editing heavily: `graphify watch .` rebuilds on file changes (long-running; user starts it, agents never do). The `graph.json` git merge driver (`graphify merge-driver`) is not registered and not needed: `graphify-out/` is git-ignored here, so each machine rebuilds locally.

`graphify update .` is cheap (AST only, no LLM). Skip silently if `graphify` is not installed or `graphify-out/graph.json` is missing. The semantic `/graphify . --update` costs LLM tokens, so run it once per change, at the step above, not after every edit.

## Per-agent expectations

- `feature-planner`: query to find existing modules/prior art before planning. Never updates.
- `frappe-dev`: query before exploring; `graphify affected` before changing a shared symbol; update after edits.
- `frappe-test`: query to find the code under test and its callers; update after adding/editing tests.
- `frappe-review`: `graphify affected` on each changed symbol to find untouched callers/duplicates; then Read source to confirm. Read-only, so does not update (report if graph looked stale).
- `frappe-ops`, `git-ops`: no queries needed. Hooks handle refresh after commits/checkouts.
