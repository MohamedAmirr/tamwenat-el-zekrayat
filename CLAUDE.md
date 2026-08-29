# Tamwenat El Zekrayat — ERPNext

Docker Compose deployment of ERPNext/Frappe for Tamwenat El Zekrayat. GitHub: `MohamedAmirr/tamwenat-el-zekrayat` (private).

## Stack

Services (see `docker-compose.yml`): `mariadb`, `redis-cache`, `redis-queue`, `configurator` (one-shot config), `create-site` (one-shot site bootstrap), `backend`, `websocket`, `queue-short`, `queue-long`, `scheduler`, `frontend` (nginx, published on `HTTP_PUBLISH_PORT`).

Versions and site config live in `.env`: `ERPNEXT_VERSION`, `MARIADB_VERSION`, `REDIS_VERSION`, `SITE_NAME`, `COMPOSE_PROJECT_NAME`. Read `.env` before assuming a value — don't hardcode versions or the site name in commands.

`docker-compose.yml` uses **prebuilt `frappe/erpnext` images** and can't run custom apps — it's the deploy-shaped stack. Custom app development happens in a separate `docker-compose.dev.yml` bench container (see "Custom app plan" below).

## Details are in docs/, not here

Keep this file short. Load these on demand rather than re-deriving them:

- `docs/docker-ops.md` — every-day commands for the prod-shaped stack: start/stop, running `bench` inside the container, logs, backup/restore, migrations, resetting.
- `docs/dev-environment.md` — the dev bench stack: bringing it up, `bench new-app`/`new-site`/`install-app`/`bench start`, how host↔container app code syncing works. Reusable for every future custom app, not just `talabat_integration`.
- `docs/frappe-conventions.md` — Frappe/ERPNext framework conventions to follow for all custom app work: doctype structure, hooks.py, permissions, migrations/patches, whitelisted methods, client scripts, naming.
- `docs/issue-conventions.md` — how features become GitHub issues: epic + ordered sub-issues, the token-efficient body template, label rules.

## Custom app plan

First custom app: **`talabat_integration`** — integrates ERPNext with the Talabat Partner API (catalog, orders, promotions). Reference docs/spec: `docs/talabat-integration/` (OpenAPI spec + notes — read the README there before writing any code against it).

Scaffolded and installable — `apps/talabat_integration/` exists in this repo, bind-mounted live into the dev bench container (`docker-compose.dev.yml`), and installs cleanly on the dev site (`dev.localhost`). See `docs/dev-environment.md` for the day-to-day workflow (starting the stack, running bench commands, editing app code). Follow `docs/frappe-conventions.md` for doctype/hook/permission conventions when writing the actual features (starting with issues #1-#6, tracked under epics #7 and #8).

## Which agent to use

- Planning a new feature before building it → `feature-planner` agent. Breaks it into an Epic + a few dependency-ordered sub-issues per `docs/issue-conventions.md`, then hands off to `git-ops` to actually create them — no need to re-explain the convention each time.
- Container/bench operations (start/stop, logs, migrate, backup, bench console, site management) → `frappe-ops` agent. Keeps verbose docker/bench output out of the main conversation.
- Writing or reviewing custom app code (doctypes, controllers, hooks, client scripts) → `frappe-dev` agent. Applies `docs/frappe-conventions.md`.
- Git/GitHub work (commits, branches, pushing, PRs, repo settings, issues) → `git-ops` agent. Never commits `.env` or force-pushes without explicit instruction.
