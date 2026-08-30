# Codex project guidance

## Project map

Tamwenat El Zekrayat is an ERPNext/Frappe deployment with a custom
`talabat_integration` app. The production-shaped Compose stack uses prebuilt
images; build custom apps only in the separate dev bench stack.

Start with [`docs/codex-project-map.md`](docs/codex-project-map.md) when you
need orientation. Load the referenced document for the active task; do not
load every document by default.

## Read by task

| Task | Required reference |
| --- | --- |
| Frappe app code or review | `docs/frappe-conventions.md`, `docs/talabat-integration/architecture.md` |
| Talabat API behavior | `docs/talabat-integration/README.md`, then `openapi.json` as needed |
| Docker, bench, logs, backups, migration | `docs/docker-ops.md`; read `.env` before using site/version values |
| Dev bench or tests | `docs/dev-environment.md`, `docs/frappe-conventions.md` |
| Issue, branch, PR, or commit work | `docs/issue-conventions.md` |

## Non-negotiable rules

- Never assume missing information — endpoints, schemas, config values,
  business rules, anything. If a doc has a gap or you lack what you need,
  stop and ask the user rather than inferring or picking a plausible
  default. If you look something up on the web to fill a gap, tell the user
  what you found and that it came from the internet (not this repo's docs),
  and get their explicit go-ahead before relying on it.
- Do not over-engineer. Build what the issue/task actually asks for — no
  speculative abstractions, extra config knobs, generic frameworks, or
  handling for cases that can't happen yet. Generalize only when a second
  real caller actually shows up, not in anticipation of one.
- Never edit vendored `frappe` or `erpnext`; change only the custom app or
  exported Frappe fixtures.
- Prefer the Frappe ORM. Parameterize any unavoidable SQL.
- Keep business logic in controllers or `doc_events`, not in API endpoints.
- Do not expose secrets in logs or responses. Every whitelisted API method
  authorizes its caller; public webhooks validate their signature.
- Update `docs/talabat-integration/architecture.md` in the same change when a
  Talabat app module, doctype, API, or core client-side pattern changes.
- Do not run destructive Docker/data/Git actions without explicit user
  confirmation. Take a backup before migrating a site with data that matters.
- Preserve existing uncommitted work and avoid broad staging commands.

## Verification and handoff

- Test changed Frappe code using the dev stack and its documented `bench`
  commands. Do not weaken a test merely to make it pass.
- Run configured linting for touched files.
- Before committing, inspect `git status` and both staged and unstaged diffs.
- Use one branch per issue (`<type>/<issue>-<slug>`), and never commit `.env`
  or credentials.
