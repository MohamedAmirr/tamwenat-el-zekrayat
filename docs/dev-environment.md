# Dev bench environment

Separate from `docker-compose.yml` (the prod-shaped stack with prebuilt images and no custom-app support). This is a real `bench` you can `bench new-app`/`new-site`/`install-app` in, used to develop custom apps — `marketplace_integrations` today, any future app the same way.

## Why a second compose file

`docker-compose.yml`'s `frappe/erpnext` images can't create new apps. This one (`docker-compose.dev.yml`) runs `frappe/bench:latest`, which has bench itself plus Python/Node tooling, against its own throwaway MariaDB/Redis — it never touches the prod stack's data or `.env`.

Note: the sections below describe the bench setup as it exists today, with only `marketplace_integrations` installed. The legacy `talabat_integration` app has been fully migrated and removed.

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
- **`apps/marketplace_integrations/` bind mount** → its matching path under `frappe-bench/apps/`: this is the only thing shared between host and container (one mount line per custom app). Edit it from your editor on the host; the container sees changes immediately, and vice versa.
- **`dev-mariadb-data` volume**: throwaway dev database, root password `123` (matches the upstream `frappe_docker` dev convention — this is a disposable local dev DB, not reused anywhere else).

## One-time setup (already done for `frappe-bench` + `marketplace_integrations` — reference for the *next* app)

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

# 4. Get ERPNext (marketplace_integrations' doctypes link to Item, which lives in ERPNext)
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

Then add a bind-mount line to `docker-compose.dev.yml`'s `frappe` service volumes (mirror the existing `marketplace_integrations` line) and recreate just that container to pick it up:

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

### Restarting `bench start`

The worker and scheduler processes started by `bench start` do not reload Python on their own. If Python changes don't seem to take effect (web or worker), restart `bench start`. After pulling or editing worker-side code (background jobs, scheduler events, anything enqueued), restart `bench start`. After JS changes, hard-refresh the browser. To restart, kill the running honcho/`bench start` process in the container, then relaunch it detached:

```bash
docker exec -d -w /home/frappe/bench-workspace/frappe-bench tamwenat-el-zekrayat-dev-frappe-1 bash -lc "nohup bench start > logs/bench-start.out 2>&1 &"
```

Always pass `-p tamwenat-el-zekrayat-dev` when using `docker compose` for this stack.

## Running tests: use `test.localhost`, never `dev.localhost`

Tests must run on the dedicated `test.localhost` site. Code under test calls `frappe.db.commit()` (per-chunk and per-group commits, `db_set(..., commit=True)`), which defeats `IntegrationTestCase`'s class-level rollback, so **a test run permanently writes to whichever site it runs on**. Running them on `dev.localhost` polluted it with hundreds of leaked rows (by 2026-09-24: 227 test Shopify Connections, 122 accounts, 1359 mappings, 123 Items, 132 Price Lists). An attempt to auto-clean them after each test (a `CleanupTestCase` that deleted "whatever is new", PR #120, closed) made things worse on a live site: its unconditional commit made rows in doctypes outside its list permanent (a leaked test tax template even became the site's default), and deleting "whatever is new" could remove a real row created by another process mid-test. Tests therefore run on a separate site, and `dev.localhost` has `allow_tests` set to `false`, so `run-tests` is refused there ("Testing is disabled for the site!"). That setting is per site and can drift: it was found `true` on `dev.localhost` on 2026-10-08 (it had been left on, and a test run happened on dev). Check `sites/dev.localhost/site_config.json` for `allow_tests` before trusting the guard. Also `common_site_config.json` has `default_site` set to `dev.localhost`, so `bench run-tests` without `--site` targets dev: always pass `--site test.localhost`.

`migrate` on `dev.localhost` (schema update after pulling) is separate from `migrate` plus tests on `test.localhost`; tests never run on dev. See "Test site vs dev site" in [`frappe-best-practices.md`](frappe-best-practices.md).

```bash
# Full suite (add --module marketplace_integrations.integrations.shopify.test_products for one module)
docker exec -w /home/frappe/bench-workspace/frappe-bench tamwenat-el-zekrayat-dev-frappe-1 bash -lc "bench --site test.localhost run-tests --app marketplace_integrations"
```

`test.localhost` is a throwaway clone of the dev site, not a fresh site: the tests assume the dev fixtures (Company `Tamwenat El Zekrayat` / abbreviation `TEZ`, its warehouses, the `Standard Selling` Price List, ...). Junk accumulating on it is fine. Recreate it whenever it drifts, or after dev gains fixtures the tests need:

```bash
# 1. Fresh backup of dev (read-only on dev)
docker exec -w /home/frappe/bench-workspace/frappe-bench tamwenat-el-zekrayat-dev-frappe-1 bash -lc "bench --site dev.localhost backup"

# 2. (Re)create the test site with the same apps, then restore the backup over it
#    If test.localhost already exists: bench drop-site test.localhost --root-password 123 first.
docker exec -w /home/frappe/bench-workspace/frappe-bench tamwenat-el-zekrayat-dev-frappe-1 bash -lc "
bench new-site test.localhost --db-root-password 123 --admin-password admin --mariadb-user-host-login-scope=% --install-app erpnext --install-app marketplace_integrations &&
bench --site test.localhost set-config pause_scheduler 1 &&
bench --site test.localhost restore sites/dev.localhost/private/backups/<timestamp>-dev_localhost-database.sql.gz --db-root-password 123 &&
bench --site test.localhost migrate &&
bench --site test.localhost clear-cache"
#    <timestamp>-dev_localhost-database.sql.gz is the file the step-1 backup printed,
#    e.g. 20260924_161501-dev_localhost-database.sql.gz.
#    pause_scheduler goes in right after new-site, before the restore: the restore brings back
#    dev's System Settings (scheduler enabled), and the site must not run jobs meanwhile.

# 3. Make it test-capable and inert (scheduler disable again: the restore reset it)
docker exec -w /home/frappe/bench-workspace/frappe-bench tamwenat-el-zekrayat-dev-frappe-1 bash -lc "
bench --site test.localhost set-config allow_tests true &&
bench --site test.localhost set-config developer_mode 1 &&
bench --site test.localhost scheduler disable"
```

Keep the test site's OWN `encryption_key` (a fresh site generates one; do not copy dev's `site_config.json` secrets or `host_name`): the restored Shopify/Talabat credentials are then undecryptable there, and with the scheduler off nothing on the test site can call a real marketplace. `default_site` in `common_site_config.json` stays `dev.localhost`.

### Fast test loop

The full `marketplace_integrations` suite takes about 50 seconds (990 tests, measured 2026-10-07). Waiting on an agent that migrates, re-reads files and retries is what made runs take 20-80 minutes, not the tests. Run them directly and print only the summary:

```bash
C=tamwenat-el-zekrayat-dev-frappe-1
docker exec -w /home/frappe/bench-workspace/frappe-bench $C bash -lc \
  "bench --site test.localhost run-tests --app marketplace_integrations > /tmp/suite.out 2>&1; \
   grep -E '^(Ran|OK|FAILED)' /tmp/suite.out; grep -E '^ (FAIL|ERROR) ' /tmp/suite.out"
```

Narrow it while iterating (all flags are from `bench run-tests --help`):

| Need | Flag |
|---|---|
| One module (seconds) | `--module marketplace_integrations.services.test_sales_order_sync` |
| One test class | `--case IntegrationTestSalesOrderSync` (with `--module`) |
| One test | `--module ... --test test_name` |
| DB-free tests only (about 0.2s for 210 tests) | `--test-category unit` |
| Stop at the first failure | `--failfast` |
| Failure detail | `sed -n '/^ FAIL /,/^Ran /p' /tmp/suite.out` inside the container |

Run the full suite once before handing work off. Migrate `test.localhost` (`bench --site test.localhost migrate`) only when a doctype JSON changed. `bench run-parallel-tests` exists but splits runs across builds for CI; it is not used here because all runs share the single `test.localhost` database, so parallel runs would clash. Run the JS test and `ruff check` / `ruff format --check` as separate quick commands.

## Parallel work with git worktrees

Worktrees are for editing and committing only: one branch per worktree, e.g. `../tez-worktrees/<branch>`. Only the main checkout's `apps/marketplace_integrations` is bind-mounted into the dev container (`docker-compose.dev.yml`), so the container cannot see a worktree. `test.localhost` has one shared database and code under test commits to it, so tests and migrates for a branch run serialized through the MAIN checkout under a host lock, covering the whole sequence:

1. `mkdir /tmp/tez-test.lock` (atomic; if it fails, another run holds it: wait and retry). Remove it when finished, also on failure.
2. In the main checkout make sure the working tree is clean, then `git switch <branch>`. If the branch is checked out in a worktree, use `git switch --detach <branch>` or remove that worktree first.
3. If a doctype JSON changed, run `bench --site test.localhost migrate` (never `dev.localhost` as part of this).
4. Run the tests with `--site test.localhost` (commands above).
5. `git switch dev`, then `rmdir /tmp/tez-test.lock`.

Never run `migrate` on `dev.localhost` while a branch other than `dev` is checked out in the main checkout: a migrate against a branch lacking a doctype drops that doctype's table (see the branch-tracking gotcha below).

True parallel test runs need a second full bench stack (own compose project, DB volume, port and test site). That is not built and is heavy; only do it if the lock becomes a real bottleneck.

## Running one-off Python against a site

`bench console` is interactive (IPython-style), so feed it a script via `echo | ...`, not a heredoc directly on `docker exec` (that leaves the console waiting on a TTY it doesn't have):

```bash
docker exec -w /home/frappe/bench-workspace/frappe-bench tamwenat-el-zekrayat-dev-frappe-1 bash -lc "echo '
import frappe
print(frappe.db.count(\"Item\"))
' | bench --site dev.localhost console"
```
(End the session by typing `y` at the "Do you really want to exit" prompt it leaves open, or just ignore it — the script's output already printed.)

**Passing a secret or special-character value in**: don't interpolate it into the python source string above — it can contain shell metacharacters (`$`, `!`, `` ` ``, `&`) that get mangled across the layers of quoting. Pass it through the environment instead, and have the python read `os.environ`:

```bash
docker exec -e MY_VALUE='literal $ecret! value' -w /home/frappe/bench-workspace/frappe-bench tamwenat-el-zekrayat-dev-frappe-1 bash -lc "echo '
import os
print(os.environ[\"MY_VALUE\"])
' | bench --site dev.localhost console"
```
The `-e KEY='...'` on `docker exec` is a single top-level single-quoted shell argument, so it's safe as long as the value itself contains no unescaped single quote.

## Gotcha: the dev site tracks whichever branch is checked out on the host

`apps/marketplace_integrations` is bind-mounted from the host repo, so the container only ever sees whatever git branch is currently checked out on disk — there is no per-branch isolation. `bench migrate` reconciles the site's schema against *those* files: if you check out a branch that doesn't have a doctype another branch added and run `migrate`, Frappe treats the missing doctype as orphaned and **drops its table**. No code is lost (the doctype's JSON/controller are safe in the other branch/git history), but the live dev DB loses that data until you check that branch back out and `migrate` again. This will bite harder as `marketplace_integrations`' doctypes land incrementally across issues — a branch missing one of them and a `migrate` in between is enough to drop it.

Practical rule: don't treat the dev site's current schema as a stable baseline across branch switches — only trust it immediately after running `migrate` on the branch you actually care about right now. Before merging multiple feature branches, the real integration check is checking out `dev` after they've all landed and running `migrate` there once.

## Gotcha: `bench migrate` won't re-run a patch it already applied on this site

Patches are tracked per-site in `Patch Log` and only ever run once, ever — this is correct behavior for a real site, but bites you while *iterating* on a not-yet-merged patch against the shared dev site: fix a bug in the patch's logic, run `migrate` again expecting it to redo the backfill, and nothing happens, because the site already marked that patch module as applied on the first (buggy) run. To force a re-run while developing, call the patch's `execute()` directly instead of going through `migrate`:

```bash
docker exec -w /home/frappe/bench-workspace/frappe-bench tamwenat-el-zekrayat-dev-frappe-1 bash -lc "echo '
from <app>.patches.<version>.<patch_module> import execute
execute()
frappe.db.commit()
' | bench --site dev.localhost console"
```

This is purely a dev-iteration wrinkle — a real site installing the app for the first time, or upgrading past this patch for the first time, runs it normally.

## Seeding sample product data (dev only)

The dev site starts with zero `Item` records — real products aren't imported into ERPNext until the actual website goes live. To have realistic Items to develop/test Talabat product-mapping work against in the meantime, `scripts/seed_dev_items.py` (repo root, **not** part of `apps/marketplace_integrations` — this is dev tooling, never shipped) creates 5 sample grocery/snack Items (with a barcode row and a price each) plus the "Snacks"/"Beverages" Item Groups if they don't already exist. It's idempotent — safe to re-run.

Never run this against a real site.

```bash
docker cp scripts/seed_dev_items.py tamwenat-el-zekrayat-dev-frappe-1:/tmp/seed_dev_items.py
docker exec -w /home/frappe/bench-workspace/frappe-bench tamwenat-el-zekrayat-dev-frappe-1 bash -lc \
  "printf \"ns = {}\nexec(open('/tmp/seed_dev_items.py').read(), ns)\nns['run']()\n\" | bench --site dev.localhost console"
```

(Piping into `bench console` needs the explicit `ns = {}` / `exec(..., ns)` / `ns['run']()` form — a bare `exec(open(...).read())` puts top-level names in a scope the function body can't see, raising `NameError` on its own globals.)

## Tearing down

```bash
docker compose -p tamwenat-el-zekrayat-dev -f docker-compose.dev.yml down       # keep the bench/db volumes
docker compose -p tamwenat-el-zekrayat-dev -f docker-compose.dev.yml down -v    # wipe them — confirm with the user first
```

## Keeping the dev bench on production's Frappe/ERPNext version

Frappe Cloud runs the latest `version-16` of Frappe and ERPNext; the dev bench does not update itself. Framework features (sidebar, apps screen, desk UI) can behave differently between versions: on 2026-10-10 prod ran Frappe 16.51.0 / ERPNext 16.50.0 while dev was on 16.32.0 / 16.33.0, and a sidebar file in the old `workspace_sidebar/` format passed every dev test but was ignored on prod. Check prod's versions (Frappe Cloud site, Apps tab) before framework-UI work, and move dev to the same commits when they differ.

How the dev bench was moved (2026-10-10), from the host with `-p tamwenat-el-zekrayat-dev`:

1. Back up every site: `bench --site <site> backup --with-files`, and `docker cp` the `sites/<site>/private/backups` folders out of the volume.
2. Git inside the container fails over HTTP/2 against GitHub ("expected flush after ref listing"): run `git config --global http.version HTTP/1.1` once. The apps' remote is named `upstream`, not `origin`. Fast-forward each app to prod's commit: `git -C apps/frappe fetch upstream version-16 && git -C apps/frappe merge --ff-only <sha>`, same for `apps/erpnext` (revert a stray `banking/yarn.lock` edit first).
3. `bench setup requirements` aborts at `marketplace_integrations` (a bind-mounted folder that is not its own git repo: `InvalidGitRepositoryError`). Install per app instead: `uv pip install -e apps/<app> --python env/bin/python` for frappe, erpnext and marketplace_integrations, and `yarn install --check-files` inside `apps/frappe` and `apps/erpnext`.
4. `bench build --apps frappe,erpnext`, then `bench --site test.localhost migrate`, then the full suite on `test.localhost`, then migrate `dev.localhost`.
5. The new Frappe ignores `<app>/workspace_sidebar/*.json`; sidebars ship as `<app>/<module>/sidebar/<name>/<name>.json` (Sidebar doctype).
