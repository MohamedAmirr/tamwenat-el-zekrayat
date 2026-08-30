# Codex project map

Use this as an on-demand orientation index. `AGENTS.md` is the concise,
always-loaded contract; the files below hold the detail.

## Architecture

| Area | Where to look | Key point |
| --- | --- | --- |
| Production stack | `docker-compose.yml`, `.env`, `docs/docker-ops.md` | Prebuilt ERPNext images; no custom-app development here. |
| Development stack | `docker-compose.dev.yml`, `docs/dev-environment.md` | Separate throwaway Frappe bench; use the explicit `tamwenat-el-zekrayat-dev` Compose project name. |
| Custom app | `apps/talabat_integration/` | Host bind-mounted into the dev bench. |
| Talabat contract | `docs/talabat-integration/openapi.json` | Source API specification. |
| Current app state | `docs/talabat-integration/architecture.md` | Read before changing the integration; update with structural changes. |

## Task routing

| Need | Primary docs | Important constraint |
| --- | --- | --- |
| Implement a doctype, hook, patch, API, or client script | `frappe-conventions.md`, Talabat architecture | Framework conventions and app map are authoritative. |
| Add Talabat functionality | Talabat README, OpenAPI, architecture | Route outbound calls through `client.talabat_request()`; do not persist live catalog data unless explicitly decided. |
| Run containers or bench | `docker-ops.md` / `dev-environment.md` | Check `.env`; bound log output; confirm destructive actions. |
| Add/run tests | `dev-environment.md`, Frappe conventions | Prefer `IntegrationTestCase` or `UnitTestCase`, not deprecated `FrappeTestCase`. |
| Plan/file issues or create a branch/PR | `issue-conventions.md` | Epic + ordered sub-issues; create GitHub-linked issue branches when available. |

## High-risk project details

- The dev site sees the host's currently checked-out branch. Running `bench
  migrate` after switching branches can drop tables that no longer have a
  matching doctype definition. Treat the schema as branch-specific.
- Frappe patches run once per site. During local patch iteration, explicitly
  call the patch `execute()` function instead of expecting `migrate` to rerun
  it.
- Opaque external IDs and secrets need `ignore_xss_filter: 1` where their exact
  bytes must survive saving.
- Composite uniqueness requires a persisted, unique derived key; a validate-time
  lookup is race-prone.
- Never rely on a plain separator when constructing a multi-field identity
  unless input values cannot contain it.

## Existing Claude roles (reference only)

The repository's `.claude/agents/` directory documents six specialized roles:
planner, Frappe developer, tester, reviewer, operations, and Git/GitHub
operator. Use their division of responsibility as workflow guidance; Codex
does not require their Claude-specific definitions to perform the work.
