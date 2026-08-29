# Dev bench environment

Separate from `docker-compose.yml` (the prod-shaped stack with prebuilt images and no custom-app support). This is a real `bench` you can `bench new-app`/`new-site`/`install-app` in, used to develop custom apps — `talabat_integration` today, any future app the same way.

## Why a second compose file

`docker-compose.yml`'s `frappe/erpnext` images can't create new apps. This one (`docker-compose.dev.yml`) runs `frappe/bench:latest`, which has bench itself plus Python/Node tooling, against its own throwaway MariaDB/Redis — it never touches the prod stack's data or `.env`.

## Always pass an explicit project name

`.env` in this directory sets `COMPOSE_PROJECT_NAME=tamwenat-el-zekrayat` for the prod stack, and Compose auto-loads `.env` regardless of which `-f` file you point at. Without an override that name would collide with the prod stack's containers/volumes. Every command below uses `-p tamwenat-el-zekrayat-dev` for this reason — don't drop it.

## Bringing the stack up

```bash
docker compose -p tamwenat-el-zekrayat-dev -f docker-compose.dev.yml up -d
```

First time only — a fresh named volume is created root-owned, and the `frappe` user inside the container can't write to it until it's chowned:

```bash
docker exec -u root tamwenat-el-zekrayat-dev-frappe-1 chown -R frappe:frappe /home/frappe/bench-workspace
```

(Skip this on subsequent `up`s — the volume persists and keeps its ownership.)

## What lives where

- **`dev-bench-data` volume**, mounted at `/home/frappe/bench-workspace`: the actual bench — `frappe-bench/apps/frappe`, `apps/erpnext`, `env/` (Python venv), `node_modules`, `sites/`. Never bind-mounted to host, never committed. It's mounted at the *parent* of `frappe-bench`, not `frappe-bench` itself — `bench init` refuses to target a path that already exists (even empty), which a volume mounted directly at `frappe-bench` would trigger.
- **`apps/talabat_integration/` bind mount** → `/home/frappe/bench-workspace/frappe-bench/apps/talabat_integration`: this is the only thing shared between host and container. Edit it from your editor on the host; the container sees changes immediately, and vice versa.
- **`dev-mariadb-data` volume**: throwaway dev database, root password `123` (matches the upstream `frappe_docker` dev convention — this is a disposable local dev DB, not reused anywhere else).

## One-time setup (already done for `frappe-bench` + `talabat_integration` — reference for the *next* app)

Run inside the container (`docker exec -w /home/frappe/bench-workspace/frappe-bench tamwenat-el-zekrayat-dev-frappe-1 bash -lc "<cmd>"`, or `docker exec -it ... bash` for an interactive shell):

```bash
# 1. Init bench (only once per volume)
cd /home/frappe/bench-workspace
bench init --skip-redis-config-generation --frappe-branch version-16 frappe-bench
cd frappe-bench

# 2. Point it at the compose services, not localhost
bench set-config -g db_host mariadb
bench set-config -g redis_cache redis://redis-cache:6379
bench set-config -g redis_queue redis://redis-queue:6379
bench set-config -g redis_socketio redis://redis-queue:6379
sed -i '/redis/d' ./Procfile   # redis runs in its own containers, not via honcho

# 3. Create the dev site
bench new-site --db-root-password 123 --admin-password admin --mariadb-user-host-login-scope=% dev.localhost
bench --site dev.localhost set-config developer_mode 1
bench --site dev.localhost clear-cache

# 4. Get ERPNext (talabat_integration's doctypes link to Item, which lives in ERPNext)
bench get-app --branch version-16 --resolve-deps erpnext
bench --site dev.localhost install-app erpnext
```

## Creating a new app (the part that's reusable for future apps)

`bench new-app <app_name>` is interactive with no non-interactive flags — pipe the answers in order (Title, Description, Publisher, Email, License, GitHub Actions y/N, Branch Name):

```bash
docker exec -w /home/frappe/bench-workspace/frappe-bench tamwenat-el-zekrayat-dev-frappe-1 bash -lc "
printf '\n<description>\n<publisher>\n<email>\n\nn\n\n' | bench new-app <app_name>
"
```
Blank lines accept the bracketed default (Title, License `mit`, Branch `version-16`); `n` declines the GitHub Actions prompt.

This generates the app inside the volume, not on the host yet. To make it host-editable:

```bash
# Copy the generated app out to the host (note the trailing /. — copies contents, not a nested dir)
docker cp tamwenat-el-zekrayat-dev-frappe-1:/home/frappe/bench-workspace/frappe-bench/apps/<app_name>/. ./apps/<app_name>

# bench new-app initializes its own git repo inside the app — remove it, this repo tracks the files directly
rm -rf ./apps/<app_name>/.git
```

Then add a bind-mount line to `docker-compose.dev.yml`'s `frappe` service volumes (mirror the `talabat_integration` line) and recreate just that container to pick it up:

```bash
docker compose -p tamwenat-el-zekrayat-dev -f docker-compose.dev.yml up -d frappe
```

Finally, install it on the dev site:

```bash
docker exec -w /home/frappe/bench-workspace/frappe-bench tamwenat-el-zekrayat-dev-frappe-1 bash -lc "bench --site dev.localhost install-app <app_name>"
```

## Day-to-day

```bash
# Run any bench command
docker exec -w /home/frappe/bench-workspace/frappe-bench tamwenat-el-zekrayat-dev-frappe-1 bash -lc "bench --site dev.localhost <command>"

# Interactive shell
docker exec -it -w /home/frappe/bench-workspace/frappe-bench tamwenat-el-zekrayat-dev-frappe-1 bash

# Start the dev webserver (foreground; add & or run in a separate exec for background)
docker exec -it -w /home/frappe/bench-workspace/frappe-bench tamwenat-el-zekrayat-dev-frappe-1 bash -lc "bench start"
```

`bench start` serves on `127.0.0.1:8000` (published by the compose file). Add `dev.localhost` to `/etc/hosts` pointing at `127.0.0.1` before browsing to `http://dev.localhost:8000`. Login `Administrator` / `admin`.

## Tearing down

```bash
docker compose -p tamwenat-el-zekrayat-dev -f docker-compose.dev.yml down       # keep the bench/db volumes
docker compose -p tamwenat-el-zekrayat-dev -f docker-compose.dev.yml down -v    # wipe them — confirm with the user first
```
