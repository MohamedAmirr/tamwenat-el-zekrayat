# Codex project map

Use this as an on-demand orientation index. `AGENTS.md` is the concise,
always-loaded contract; the files below hold the detail.

## Architecture

| Area | Where to look | Key point |
| --- | --- | --- |
| Production stack | `docker-compose.yml`, `.env`, `docs/docker-ops.md` | Prebuilt ERPNext images; no custom-app development here. |
| Development stack | `docker-compose.dev.yml`, `docs/dev-environment.md` | Separate throwaway Frappe bench; use the explicit `tamwenat-el-zekrayat-dev` Compose project name. |
| Target marketplace app | `apps/marketplace_integrations/`, `docs/marketplace-integrations/architecture.md` | Shared target architecture; scaffold/migrate incrementally under #33. |
| Legacy Talabat app | `apps/talabat_integration/`, `docs/talabat-integration/architecture.md` | Existing implementation retained during migration under #8. |
| Talabat contract | `docs/talabat-integration/openapi.json` | Source API specification for Talabat behavior. |

## Task routing

| Need | Primary docs | Important constraint |
| --- | --- | --- |
| Implement shared marketplace doctypes/imports/services | `frappe-conventions.md`, marketplace architecture | New cross-marketplace functionality belongs in `marketplace_integrations`. |
| Migrate/fix legacy Talabat code | Frappe conventions, both architecture docs | Preserve proven behavior while moving callers/data incrementally. |
| Add Talabat API behavior | Talabat README, OpenAPI, marketplace architecture | The checked-in Talabat spec is authoritative; do not infer endpoints/payloads. |
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
