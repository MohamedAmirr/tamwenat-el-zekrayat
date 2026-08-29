# Docker / bench operations

All commands run from the repo root. `$SITE_NAME` below means the value currently set in `.env` — check it, don't assume `erpnext.localhost`.

## Lifecycle

```bash
docker compose up -d              # start everything
docker compose down                # stop, keep volumes (data survives)
docker compose down -v             # stop and WIPE all data — confirm with the user first
docker compose ps                  # container status
docker compose logs -f create-site # watch first-run site bootstrap
docker compose logs -f backend     # app server logs
```

## Running bench commands

The `backend` container has the bench environment. Run one-off commands with `exec`:

```bash
docker compose exec backend bench --site $SITE_NAME migrate
docker compose exec backend bench --site $SITE_NAME console
docker compose exec backend bench --site $SITE_NAME clear-cache
docker compose exec backend bench --site $SITE_NAME set-config <key> <value>
docker compose exec backend bench --site $SITE_NAME list-apps
```

If `backend` isn't running (e.g. debugging `create-site`), use `docker compose run --rm backend bench ...` instead of `exec`.

## Backup / restore

```bash
docker compose exec backend bench --site $SITE_NAME backup --with-files
# backups land in the `sites` volume under sites/$SITE_NAME/private/backups

docker compose exec backend bench --site $SITE_NAME restore <path-to-sql-gz> \
  --with-public-files <public-files-tar> --with-private-files <private-files-tar>
```

Always take a backup before `migrate` on data you care about, and before any `down -v`.

## Migrations after upgrading versions

1. Bump `ERPNEXT_VERSION` (and `MARIADB_VERSION`/`REDIS_VERSION` if needed) in `.env`.
2. `docker compose pull`
3. `docker compose up -d`
4. `docker compose exec backend bench --site $SITE_NAME migrate`

## Reading logs efficiently

Prefer `docker compose logs --since 10m <service>` or `--tail 200` over unbounded `-f` when you just need recent output — full logs can be large. Use the `frappe-ops` agent for anything that needs scanning long log output; it keeps the raw text out of the main conversation and reports back a summary.

## Site name / hosts file

`SITE_NAME` is not plain `localhost`, so add it to `/etc/hosts` pointing at `127.0.0.1` before browsing to it, or set `FRAPPE_SITE_NAME_HEADER` (already wired to `SITE_NAME` in `docker-compose.yml`).
