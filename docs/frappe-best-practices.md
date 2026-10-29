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
- Hooks (`doc_events`, `scheduler_events`) only for an intentional lifecycle trigger. This app registers no catalog `doc_events`: `api/test_talabat_catalog.py::test_no_catalog_document_events_are_registered` fails if this app registers one for Stock Ledger Entry, Item Price, Item, Marketplace Product Mapping or Marketplace Account. Catalog pushes run from cron/interval polling and explicit actions. The only `doc_events` are the Marketplace Order re-check triggers (#364) on Delivery Note, Sales Invoice, Payment Entry (`on_submit`, `on_cancel`, `on_trash`) and Sales Order (`on_update_after_submit`, `on_cancel`): the handler (`services/sales_order_recheck.py`) only bumps the sync debounce, never raises, makes no HTTP call and writes without permission checks because the user submitting a Delivery Note may have no Marketplace Order permission. A handler on a core doctype that runs inside someone else's transaction must follow the same shape. Read that test and `hooks.py` before proposing a new document hook.
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
- #364 submitted Sales Order row add/remove (`services/sales_order_reconciliation.py::_execute_update`) relies on ERPNext's `erpnext.controllers.accounts_controller.update_child_qty_rate` adding a row from a `trans_items` entry without `docname` and deleting a row that is omitted, on `erpnext.stock.get_item_details.get_conversion_factor`, and on `allow_zero_qty_in_sales_order` being 0 (so a zeroed line is dropped, case 6 stays manual).
- Verified: ERPNext 16.51.1 / Frappe 16.52.0 on dev (`test.localhost` tests in `services/test_sales_order_sync.py`); `allow_zero_qty_in_sales_order` = 0 on test and dev. Prod versions and prod `allow_zero_qty_in_sales_order` NOT checked.
- Evidence: the tests on real submitted Sales Orders (`IntegrationTestSalesOrderRowChanges` in `services/test_sales_order_sync.py`).
- Recheck: on any ERPNext/Frappe upgrade, and before shipping to prod.
- #390 partial line update on a submitted Sales Order with delivered or billed rows (`_execute_update`, `partial_line_update` evidence) relies on `update_child_qty_rate` behaving, for a row of a submitted order, as spiked: an undelivered, unbilled row can be reduced; a row with 1 of 2 delivered can go 2 to 1; qty 0 is always refused ("cannot be zero"), so a row is removed only by leaving it out of `trans_items`, and every kept row (delivered or billed too) must be listed, because leaving out a delivered or billed row is refused; a cut below the delivered qty raises `ValidationError`; a billed-only row cut below its billed qty raises `ValidationError` that mentions Rate, not quantity. Messages are translated, so code matches the exception class and re-reads the row quantities, never the text. The planner therefore checks the delivered and billed quantity itself first (the billed quantity is derived as billed amount / rate).
- Verified: ERPNext 16.51.1 / Frappe 16.52.0 on `test.localhost` (spike, 2026-10-29). Prod versions NOT checked.
- Evidence: the real-Sales-Order tests `test_partial_update_*` in `services/test_sales_order_sync.py`.
- Recheck: on any ERPNext/Frappe upgrade, and before shipping to prod.
- #325 Sales Order hover card (`public/js/order_hover_card.js`) relies on `frappe.ui.hover_card` (`frappe/public/js/frappe/ui/components/hover_card.js`), on Desk list row markup (`list-row-container`, the subject link `.list-subject a[data-name]`, checkbox `data-name`, `level-right`), on the instance handle (`frappe.ui.hover_card` stores it in jQuery data `es-hover-card`, read once at attach time and kept in a `WeakMap` by row, since jQuery `.remove()` wipes that data before a MutationObserver runs), and on ERPNext selling `sales_order_list.js` assigning `frappe.listview_settings["Sales Order"]`.
- Verified: Frappe 16.51.0 on dev, 2026-10-22. Prod version NOT known; look it up (Frappe Cloud Apps tab).
- Evidence: the always-on test `marketplace_integrations/test_order_hover_card_js_wiring.py`, plus the framework source paths above.
- Recheck: on any Frappe/ERPNext upgrade, and before shipping to prod.
- #326 Navigation Dock mount (`dock/marketplace_integrations/marketplace_integrations.json`) relies on the Dock `mount_on` mechanism (`frappe/desk/doctype/dock/dock.py`: `mounted_apps`, `validate_added_rows` requiring icon and title), on `frappe/model/sync.py` `APP_ROOTED_DOCTYPES` importing `<app>/dock/<app>/<app>.json` and `<module>/sidebar/<name>/<name>.json`, and on `resolve_sidebar` dropping a shell with no permitted item. Mounted rows are appended after the host's rows; a saved site or user Dock layer drops entries it does not name, so on such a site the entries must be added in Manage Dock.
- Verified: Frappe 16.52.0 / ERPNext 16.51.1 (prod commits 4515014 / 5af6561; prod has no saved Dock layers, checked 2026-10-22), on `test.localhost`.
- Evidence: the always-on test `marketplace_integrations/tests/test_navigation.py` (`UnitTestDockFrameworkWiring`, plus per-role resolution through `resolve_sidebar`).
- Recheck: on any Frappe/ERPNext upgrade, and when prod gains a saved erpnext Dock layer.

## Skills (optional help)

Repository rules, this file and the installed framework version take precedence over any skill. Project-local: `.claude/skills/frappe-desk-ui/SKILL.md` for Desk client scripts. Third-party Frappe skill packs exist (community, not official, unreviewed here); none are installed. General frontend-design guidance may help with custom screens but stays subordinate to established Frappe patterns.
