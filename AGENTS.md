# Codex project guidance

## Project map

Tamwenat El Zekrayat is an ERPNext/Frappe deployment. The target shared
marketplace app is `marketplace_integrations`; the existing
`talabat_integration` app is legacy and remains in place during the Talabat
migration. The production-shaped Compose stack uses prebuilt images; build
custom apps only in the separate dev bench stack.

Start with [`docs/codex-project-map.md`](docs/codex-project-map.md) when you
need orientation. Load the referenced document for the active task; do not
load every document by default.

## Read by task

| Task | Required reference |
| --- | --- |
| Shared marketplace app code or review | `docs/frappe-conventions.md`, `docs/marketplace-integrations/architecture.md` |
| Legacy Talabat app code or migration | `docs/frappe-conventions.md`, `docs/talabat-integration/architecture.md`, `docs/marketplace-integrations/architecture.md` |
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
- Ask before deciding. When a task has more than one reasonable way to do
  it — a design choice, a scope call, which endpoint/mechanism/UI approach
  to use — do not pick one and proceed. Surface the real options (with a
  recommendation if you have one) and let the user choose. This is
  stricter than "never assume": even when you have enough information to
  make a defensible call yourself, the call is still the user's to make.
- Show GitHub issue content before it is created or edited. Never create or
  edit a GitHub issue's actual content (title, scope, acceptance criteria,
  which options got picked) without showing the user that content first
  and getting a go-ahead. This applies even when the content follows an
  already-agreed decision — the issue text itself is the thing to confirm.
  If an issue does get created without this step, surface its full content
  immediately afterward and offer to revise it.
- Never edit vendored `frappe` or `erpnext`; change only the custom app or
  exported Frappe fixtures.
- Prefer the Frappe ORM. Parameterize any unavoidable SQL.
- Keep business logic in controllers or `doc_events`, not in API endpoints.
- Do not expose secrets in logs or responses. Every whitelisted API method
  authorizes its caller; public webhooks validate their signature.
- Update `docs/marketplace-integrations/architecture.md` when the shared
  marketplace app's core model/module boundaries change. Update
  `docs/talabat-integration/architecture.md` when the legacy Talabat app's
  actual code map changes.
- Do not add new cross-marketplace functionality to `talabat_integration`.
  New shared functionality belongs in `marketplace_integrations`; touch the
  legacy app only for an explicit legacy fix or migration step.
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
