# Tamwenat El Zekrayat — ERPNext

Docker Compose deployment of ERPNext/Frappe for Tamwenat El Zekrayat. GitHub: `MohamedAmirr/tamwenat-el-zekrayat` (private).

## Stack

Services (see `docker-compose.yml`): `mariadb`, `redis-cache`, `redis-queue`, `configurator` (one-shot config), `create-site` (one-shot site bootstrap), `backend`, `websocket`, `queue-short`, `queue-long`, `scheduler`, `frontend` (nginx, published on `HTTP_PUBLISH_PORT`).

Versions and site config live in `.env`: `ERPNEXT_VERSION`, `MARIADB_VERSION`, `REDIS_VERSION`, `SITE_NAME`, `COMPOSE_PROJECT_NAME`. Read `.env` before assuming a value — don't hardcode versions or the site name in commands.

`docker-compose.yml` uses **prebuilt `frappe/erpnext` images** and can't run custom apps — it's the deploy-shaped stack. Custom app development happens in a separate `docker-compose.dev.yml` bench container (see "Custom app strategy" below).

## Details are in docs/, not here

Keep this file short. Load these on demand rather than re-deriving them:

- `docs/docker-ops.md` — every-day commands for the prod-shaped stack: start/stop, running `bench` inside the container, logs, backup/restore, migrations, resetting.
- `docs/dev-environment.md` — the dev bench stack: bringing it up, `bench new-app`/`new-site`/`install-app`/`bench start`, how host↔container app code syncing works. Reusable for every custom app.
- `docs/frappe-conventions.md` — Frappe/ERPNext framework conventions to follow for all custom app work: doctype structure, hooks.py, permissions, migrations/patches, whitelisted methods, client scripts, naming, plus hard-won gotchas (XSS-sanitizer field corruption, autoname/`_sync_autoname_field` traps, composite-uniqueness pattern).
- `docs/marketplace-integrations/architecture.md` — target shared marketplace architecture: connection/account/mapping/inventory boundaries, import shapes, and migration rules. Read this for all new marketplace work.
- `docs/talabat-integration/architecture.md` — historical map of the now-removed legacy `talabat_integration` app. Closed Talabat issues and this doc remain evidence of proven behavior/security decisions; consult them when touching Talabat code in `marketplace_integrations` so proven behavior isn't rediscovered or lost.
- `docs/issue-conventions.md` — how features become GitHub issues: epic + ordered sub-issues, the token-efficient body template, label rules.

## Custom app strategy

Single custom app: **`marketplace_integrations`** — owns cross-marketplace account, product-mapping, inventory-partition, import/export, Talabat integration, and future platform integration work. Its agreed architecture is `docs/marketplace-integrations/architecture.md` and its foundation is tracked by #33.

The legacy `talabat_integration` app has been fully migrated and removed (tracked by #8); all Talabat functionality now lives in `marketplace_integrations`. Talabat API behavior still comes from `docs/talabat-integration/README.md` and the checked-in `openapi.json`, not from the shared architecture document.

## Ground rules

- **Never assume.** Don't guess at missing information — endpoints, schemas, config values, business rules, anything. If a doc has a gap or you don't have what you need, stop and ask the user rather than inferring or picking a plausible default. This applies to every agent, not just the main session (e.g. the #6/#26 catalog-sync design was initially drafted around an assumed polling mechanism instead of checking `docs/talabat-integration/openapi.json`, which documents a webhook callback instead — exactly the kind of assumption this rule exists to prevent). If you look something up on the web to fill a gap, tell the user what you found and that it came from the internet (not this repo's docs), and get their explicit go-ahead before relying on it — don't silently fold external information into a decision, into code, or into an issue body.
- **Don't over-engineer.** Build what the issue/task actually asks for — no speculative abstractions, extra config knobs, generic frameworks, or handling for cases that can't happen yet. Even inside `marketplace_integrations`, don't invent generic platform adapters/fields before a second real platform creates the need. `RemoteDataGrid` is the existing deliberate reusable exception because its issue explicitly scoped it that way. When in doubt about how much to build, ask rather than defaulting to the more elaborate version.
- **Ask before deciding.** When a task has more than one reasonable way to do it — a design choice, a scope call, which endpoint/mechanism/UI approach to use — don't pick one and proceed. Surface the real options (with a recommendation if you have one) and let the user choose, the way the #6/#26 endpoint-and-trigger decisions and #5's deferred-UI note were handled. This is stricter than "never assume": even when you have enough information to make a defensible call yourself, the call is still the user's to make, not yours.
- **Show GitHub issue content before it's created or edited.** Never have `git-ops` (or anyone) create or edit a GitHub issue's actual content — title, scope, acceptance criteria, which options got picked — without showing the user that content first and getting a go-ahead. This applies even when the content itself follows an already-agreed decision; the issue text is the thing to confirm, not just the decision behind it. If an issue does get created without this step, surface its full content immediately afterward and offer to revise it.

## Which agent to use

Typical flow per sub-issue: `feature-planner` (plan it) → `frappe-dev` (build it) → `frappe-test` (test + lint it) → `frappe-review` (review it) → `git-ops` (commit/PR it).

- Planning a new feature before building it → `feature-planner` agent (opus — this is a judgment call worth spending on). Breaks it into an Epic + a few dependency-ordered sub-issues per `docs/issue-conventions.md`, then hands off to `git-ops` to actually create them — no need to re-explain the convention each time.
- Container/bench operations (start/stop, logs, migrate, backup, bench console, site management) → `frappe-ops` agent. Keeps verbose docker/bench output out of the main conversation.
- Writing or reviewing custom app code (doctypes, controllers, hooks, client scripts) → `frappe-dev` agent. Applies `docs/frappe-conventions.md`.
- Writing/running tests and linting → `frappe-test` agent (haiku — mechanical: write test files, run `bench run-tests`, run whatever linter is configured, fix trivial lint issues, report real failures rather than papering over them).
- Reviewing a diff/PR before it lands → `frappe-review` agent (opus). Checks convention adherence, duplication/schema drift, inconsistent implementation, security, and framework best practices. Read-only — reports findings, doesn't fix them.
- Git/GitHub work (commits, branches, pushing, PRs, repo settings, issues) → `git-ops` agent (haiku — mechanical once the rules in its own file are followed). Never commits `.env` or force-pushes without explicit instruction.
