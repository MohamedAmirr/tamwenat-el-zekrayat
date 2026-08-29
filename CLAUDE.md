# Tamwenat El Zekrayat — ERPNext

Docker Compose deployment of ERPNext/Frappe for Tamwenat El Zekrayat. GitHub: `MohamedAmirr/tamwenat-el-zekrayat` (private).

## Stack

Services (see `docker-compose.yml`): `mariadb`, `redis-cache`, `redis-queue`, `configurator` (one-shot config), `create-site` (one-shot site bootstrap), `backend`, `websocket`, `queue-short`, `queue-long`, `scheduler`, `frontend` (nginx, published on `HTTP_PUBLISH_PORT`).

Versions and site config live in `.env`: `ERPNEXT_VERSION`, `MARIADB_VERSION`, `REDIS_VERSION`, `SITE_NAME`, `COMPOSE_PROJECT_NAME`. Read `.env` before assuming a value — don't hardcode versions or the site name in commands.

Currently using **prebuilt `frappe/erpnext` images** — no custom app can run until a custom image is built (see "Custom app" below).

## Details are in docs/, not here

Keep this file short. Load these on demand rather than re-deriving them:

- `docs/docker-ops.md` — every-day commands: start/stop, running `bench` inside the container, logs, backup/restore, migrations, resetting.
- `docs/frappe-conventions.md` — Frappe/ERPNext framework conventions to follow once custom app work starts: doctype structure, hooks.py, permissions, migrations/patches, whitelisted methods, client scripts, naming.

## Custom app plan

First custom app: **`talabat_integration`** — integrates ERPNext with the Talabat Partner API (catalog, orders, promotions). Reference docs/spec: `docs/talabat-integration/` (OpenAPI spec + notes — read the README there before writing any code against it).

Not scaffolded yet. When starting it:
1. Scaffold it with `bench new-app` inside a **dev bench container** (the prebuilt `frappe/erpnext` image can't create new apps) — not yet set up in this repo; ask before adding one, it changes the compose topology.
2. App code should live in its own directory here (`apps/talabat_integration/`) so it can be bind-mounted into a dev container and later baked into a custom production image via `apps.json` + `docker build`, per Frappe's official `frappe_docker` custom-image docs.
3. Follow `docs/frappe-conventions.md` for doctype/hook/permission conventions.

## Which agent to use

- Container/bench operations (start/stop, logs, migrate, backup, bench console, site management) → `frappe-ops` agent. Keeps verbose docker/bench output out of the main conversation.
- Writing or reviewing custom app code (doctypes, controllers, hooks, client scripts) → `frappe-dev` agent. Applies `docs/frappe-conventions.md`.
- Git/GitHub work (commits, branches, pushing, PRs, repo settings) → `git-ops` agent. Never commits `.env` or force-pushes without explicit instruction.
