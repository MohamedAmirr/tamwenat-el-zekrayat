# Frappe best practices (shared rules)

Single source of truth for the cross-cutting rules every agent applies. Agent files point here instead of repeating these. Details, framework gotchas and examples stay in `docs/frappe-conventions.md`; UI specifics are in `docs/ui-customization.md`. If a rule here conflicts with generic advice from a skill or the internet, this file and the installed framework version win.

Each rule says what to do. "Justify" means a short comment or PR note naming the concrete reason, not a general one.

## Permissions

- `frappe.get_list` applies the session user's permissions. `frappe.get_all` bypasses them (it is `get_list` with `ignore_permissions=True`). Use `get_list` for anything whose result reaches a user (API responses, reports, pickers, Desk pages). Use `get_all` only for system-internal work (sweeps, jobs, integration code that already runs as a trusted actor).
- Every `ignore_permissions=True`, `frappe.set_user`, `frappe.flags.ignore_permissions` and `get_all` on user-reachable data needs a justification and a check that the caller was authorized earlier on the same path.
- Hiding navigation, workspace shortcuts, fields or buttons is presentation only. Authorization is always enforced on the server: document-level access (DocPerm, `has_permission`), list filtering (`permission_query_conditions`), reports (the `report` permission on the `ref_doctype`), and every `@frappe.whitelist()` method (whitelisting is not a permission check).
- Verify with a non-Administrator staff user (see "Verification").

## Where business logic goes

- Reusable operations and integration workflows: shared service functions (`services/`, `integrations/<platform>/`). Both API endpoints and background jobs call them.
- Document validation and lifecycle invariants: the doctype controller (`validate`, `on_submit`, ...).
- Hooks (`doc_events`, `scheduler_events`) only for an intentional lifecycle trigger. This app registers no catalog `doc_events`: `hooks.py` keeps `doc_events` empty, and `api/test_talabat_catalog.py::test_no_catalog_document_events_are_registered` fails if this app registers one for Stock Ledger Entry, Item Price, Item, Marketplace Product Mapping or Marketplace Account. Catalog pushes run from cron/interval polling and explicit actions. Read that test and `hooks.py` before proposing a new document hook.
- API endpoints: authorize, parse and validate input, delegate, shape the response. No business logic.

## Data integrity

- Prefer document operations (`doc.insert()/save()/submit()`) whenever validation, permissions, naming or lifecycle hooks must run. `frappe.db.set_value`, `db_set`, `frappe.db.sql` updates and `frappe.db.delete` skip all of that (and `modified` tracking unless told otherwise).
- A direct database write, permission bypass or manual `frappe.db.commit()` needs a justification: what behavior is intentionally skipped and why a document operation does not work.
- Preserve atomicity: one logical change per transaction, row locks (`for_update`) where concurrent writers exist, idempotent writes. A commit inside a function commits the caller's whole transaction (this also defeats test rollback, see the Tests section of `docs/frappe-conventions.md`).
- Never write around stock or accounting invariants (Stock Ledger, GL, outstanding amounts). Go through the ERPNext document that owns them.

## UI customization

- Prefer, in order: standard fields and settings, workspaces and sidebars, reports, existing components in this app, fixtures (Custom Field, Property Setter, Client Script), then supported extension hooks (`doctype_js`, `doctype_list_js`, `override_*` last).
- All changes live in `apps/marketplace_integrations` or fixtures exported from it. Never edit `frappe` or `erpnext`.
- Wrapping an internal framework method (for example `frm.cscript.<method>`) is an isolated, documented exception: keep it in one place, guard it, add a test that asserts the framework method still exists, and record it under "Version compatibility".
- Any dependency on framework internals gets a compatibility check on dev and on the prod version before it ships.

## Background work

For every job, say in the code or PR:
- Retry limit and backoff, and which failures are retryable (timeouts, 429, 5xx) versus terminal (4xx validation, missing mapping). Terminal failures stop and surface; they are not retried forever.
- Duplicate handling: a deduplicated `job_id`, or an idempotency key checked before side effects, so a re-run or double enqueue does no harm.
- Transaction boundaries: what is committed before the external call, what after. Never hold a row lock across an external HTTP call.
- A user-visible failure and recovery state (status field, Needs Attention entry, error text, a Retry action) so nobody has to read logs.
- Reuse the existing patterns (`frappe.enqueue` with `job_id`, `Marketplace Sync Log`, the sweep/poll jobs in `hooks.py`) instead of a new framework.

## Verification

- Test access with an appropriate ordinary staff user as well as Administrator (`tests/utils.py::make_role_user`). Cover direct URL access, list views, reports, APIs, and the case where navigation is hidden but the URL still works.
- UI changes: check English (LTR) and Arabic (RTL).
- Where the change is visible, the user does the real-browser pass on the dev site using the checklist in `docs/ui-customization.md`; agents prepare accounts/data and the checklist and report browser checks as "not done by agent, pending user". Unit or node tests prove logic, never appearance; do not report visual verification from them.
- Python tests run only on `test.localhost` (see "Test site vs dev site"). Node tests run with `node --test`.

## Test site vs dev site

- `bench ... run-tests` always uses `--site test.localhost`, never `dev.localhost` and never a production site. Code under test commits, so a test run permanently writes rows to whatever site it runs on.
- Updating the dev site's schema after pulling code is `bench --site dev.localhost migrate` (back up first if the site has data that matters). Test-site preparation is a separate step: `bench --site test.localhost migrate`, then run the suite there. Commands: `docs/dev-environment.md`.
- Destructive operations (`down -v`, restore over data, `drop-site`, force operations) need explicit user confirmation (`AGENTS.md`, "Do not run destructive ... without explicit user confirmation"; `.claude/agents/frappe-ops.md`). Backup commands: `docs/docker-ops.md`.

## Version compatibility

Three kinds of statement, kept apart:
1. General Frappe behavior that holds across versions.
2. Version-specific observations, tied to a release.
3. Project-specific exceptions, tied to this app's design.

Do not present a workaround seen on one v16 release as valid for all of v16. Dev and prod can drift. Dev was verified at Frappe 16.51.0 on 2026-10-22 (`apps/frappe/frappe/__init__.py` in the bench container); prod's version was not rechecked, so look it up (Frappe Cloud Apps tab) before framework-UI work. Record each version-specific item or workaround with:
- verified version(s) and site (dev or prod),
- evidence: framework source path, or the regression test that fails if it stops holding,
- recheck trigger: e.g. "on any Frappe upgrade", or "when prod passes 16.x".

Existing entries in `docs/frappe-conventions.md` and the architecture doc that name a framework version already follow this; add the three fields when you touch one.

Framework-internals dependencies on record:
- #325 Sales Order hover card (`public/js/order_hover_card.js`) relies on `frappe.ui.hover_card` (`frappe/public/js/frappe/ui/components/hover_card.js`), on Desk list row markup (`list-row-container`, the subject link `.list-subject a[data-name]`, checkbox `data-name`, `level-right`), on the instance handle (`frappe.ui.hover_card` stores it in jQuery data `es-hover-card`, read once at attach time and kept in a `WeakMap` by row, since jQuery `.remove()` wipes that data before a MutationObserver runs), and on ERPNext selling `sales_order_list.js` assigning `frappe.listview_settings["Sales Order"]`.
- Verified: Frappe 16.51.0 on dev, 2026-10-22. Prod version NOT known; look it up (Frappe Cloud Apps tab).
- Evidence: the always-on test `marketplace_integrations/test_order_hover_card_js_wiring.py`, plus the framework source paths above.
- Recheck: on any Frappe/ERPNext upgrade, and before shipping to prod.

## Skills (optional help)

Repository rules, this file and the installed framework version take precedence over any skill. Project-local: `.claude/skills/frappe-desk-ui/SKILL.md` for Desk client scripts. Third-party Frappe skill packs exist (community, not official, unreviewed here); none are installed. General frontend-design guidance may help with custom screens but stays subordinate to established Frappe patterns.
