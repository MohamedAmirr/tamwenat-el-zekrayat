# Tamwenat El Zekrayat — ERPNext (Docker)

Local ERPNext stack for **Tamwenat El Zekrayat**, using official Frappe/ERPNext
Docker images, with MariaDB and Redis running as containers alongside it.

## Versions used (checked as of 2026-08-29, latest stable at setup time)

| Component | Version | Notes |
|---|---|---|
| ERPNext | v16.33.0 | Latest ERPNext v16 release |
| Frappe Framework | bundled with erpnext v16.33.0 image | |
| MariaDB | 12.3.3 | Current MariaDB LTS. If you hit compatibility issues, Frappe is most heavily tested against 10.6/11.4 — pin `MARIADB_VERSION` in `.env` to `11.4` as a fallback. |
| Redis | 8.10.1 | Used for both cache and queue/socketio |

## Before you start

1. Edit `.env` and change:
   - `ADMIN_PASSWORD` — ERPNext Administrator login password
   - `DB_ROOT_PASSWORD` — MariaDB root password
   - `SITE_NAME` — defaults to `tamwenat-el-zekrayat.localhost`
   - `HTTP_PUBLISH_PORT` — defaults to `8080`

2. Make sure Docker Desktop is running.

## Start the stack

```bash
cd ~/Projects/tamwenat-el-zekrayat
docker compose up -d
```

The `configurator` container runs once to wire up DB/Redis config, then
`create-site` runs once to create the site and install ERPNext (this can
take a few minutes on first run — watch its logs):

```bash
docker compose logs -f create-site
```

## Access

Once `create-site` finishes successfully, open:

```
http://localhost:8080
```

- Username: `Administrator`
- Password: whatever you set as `ADMIN_PASSWORD` in `.env`

Since `SITE_NAME` is `tamwenat-el-zekrayat.localhost` (not plain `localhost`),
you'll need to add it to your hosts file for the hostname to resolve.

## Stopping / resetting

```bash
docker compose down          # stop, keep data
docker compose down -v       # stop and wipe all data (fresh start)
```

## Notes

- This setup uses official `frappe/erpnext` prebuilt images — no custom apps
  can be installed into this stack (matching Frappe's own guidance for this
  deployment style). If you'll need custom Frappe apps later, that requires
  building a custom image (`apps.json` + `docker build`), which is a
  separate step we can add on request.
- Versions are pinned in `.env` — bump `ERPNEXT_VERSION`, `MARIADB_VERSION`,
  or `REDIS_VERSION` there and re-run `docker compose up -d` to upgrade.
