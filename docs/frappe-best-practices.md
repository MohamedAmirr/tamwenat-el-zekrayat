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
- Hooks (`doc_events`, `scheduler_events`) only for an intentional lifecycle trigger. This app registers no catalog `doc_events`: `api/test_talabat_catalog.py::test_no_catalog_document_events_are_registered` fails if this app registers one for Stock Ledger Entry, Item Price, Item, Marketplace Product Mapping or Marketplace Account. Catalog pushes run from cron/interval polling and explicit actions. The only `doc_events` are the Marketplace Order re-check triggers (#364) on Delivery Note, Sales Invoice, Payment Entry (`on_submit`, `on_cancel`, `on_trash`) and Sales Order (`on_update_after_submit`, `on_cancel`), the Noon Sales Order Item row check (#368) and the neutral Delivery Note carrier handlers (#391/#415, `services/carrier_shipments.py`: `validate`, `before_submit`, `before_cancel` and `on_trash` are deliberate refusals, so they do raise, and each only touches the notes its carrier adapter claims; nothing is queued on insert, the shipment job starts from the Submit action; the Sales Order `before_cancel` refuses while a draft Delivery Note has a live shipment). The nightly `carrier_shipments.reconcile_open_notes` cron entry calls each adapter's `reconcile()`. The re-check handler (`services/sales_order_recheck.py`) only bumps the sync debounce, never raises, makes no HTTP call and writes without permission checks because the user submitting a Delivery Note may have no Marketplace Order permission. A handler on a core doctype that runs inside someone else's transaction must follow the same shape (never raises, no HTTP, writes queued after commit) unless its whole purpose is to refuse the save, as the two Delivery Note refusals and the Noon item-edit check do. Read that test and `hooks.py` before proposing a new document hook.
- API endpoints: authorize, parse and validate input, delegate, shape the response. No business logic.

## Data integrity

- Prefer document operations (`doc.insert()/save()/submit()`) whenever validation, permissions, naming or lifecycle hooks must run. `frappe.db.set_value`, `db_set`, `frappe.db.sql` updates and `frappe.db.delete` skip all of that (and `modified` tracking unless told otherwise).
- A direct database write, permission bypass or manual `frappe.db.commit()` needs a justification: what behavior is intentionally skipped and why a document operation does not work.
- Preserve atomicity: one logical change per transaction, row locks (`for_update`) where concurrent writers exist, idempotent writes. A commit inside a function commits the caller's whole transaction (this also defeats test rollback, see the Tests section of `docs/frappe-conventions.md`).
- Never write around stock or accounting invariants (Stock Ledger, GL, outstanding amounts). Go through the ERPNext document that owns them.

## Database indexes

- Add an index only for a named, hot query (file:line in the PR). The index must match the predicate: equality columns first, then the range or ORDER BY column. A low-cardinality column (a Select with a few values) is useful only as the second column of a composite behind an equality column.
- Single column: `search_index: 1` in the doctype JSON, and bump the JSON `modified` so migrate re-syncs it. Composite: a module-level `on_doctype_update()` in the controller calling `frappe.db.add_index(doctype, [cols])` (it checks for the index first, so calling it twice is safe; the auto name is `<col1>_<col2>_index`). Frappe calls `on_doctype_update` only on install or when the JSON `modified` changed, so a composite also needs the `modified` bump, and a hand-dropped index is not rebuilt until the JSON changes again.
- Check redundancy before adding: a composite makes a single index on its first column a duplicate. Check every query that filters on that column alone, then remove the single index's `search_index` in the same PR (migrate drops it before it adds the composite, a short gap with no index on that column). If you cannot check them all, report the duplicate and leave it.
- Weigh the write cost: every update that changes an indexed column pays index maintenance (InnoDB indexes NULLs too, so a mostly-NULL column is not free). Skip an index whose read benefit does not outweigh how often the column is written.
- Prove it with `EXPLAIN` before and after on a realistic table. A tiny table (the test site) often ignores indexes; `FORCE INDEX` only shows usability.

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
- #331 the System Notification body comes from the Notification field `message` on Frappe 16.52 (`notification_title` and `notification_message` were removed from the doctype in frappe commit 49e60fc23a); 16.51 still reads `notification_message`. The fixture `fixtures/notification.json` ("Refund Detected on Marketplace Order") therefore sets `message` and keeps the two legacy keys, so one file works on both. Verified: Frappe 16.52.0 on dev and prod commit 4515014. Evidence: `integrations/shopify/test_refund_notification.py::test_notification_message_identifies_the_order_and_links_to_it`. Recheck: on any Frappe upgrade, and remove the legacy keys once nothing runs 16.51.
- #364 submitted Sales Order row add/remove (`services/sales_order_reconciliation.py::_execute_update`) relies on ERPNext's `erpnext.controllers.accounts_controller.update_child_qty_rate` adding a row from a `trans_items` entry without `docname` and deleting a row that is omitted, on `erpnext.stock.get_item_details.get_conversion_factor`, and on `allow_zero_qty_in_sales_order` being 0 (so a zeroed line is dropped, case 6 stays manual). Mark as Out of Stock (#416, `integrations/noon/fulfillment.py`) goes through the same path and also depends on `update_child_qty_rate` deleting the row and on ERPNext cancelling and recreating the row's Stock Reservation Entries (`accounts_controller.py` around L4652-4663 on 16.51.1). Checked on the ERPNext 16.51.1 dev site only, the production version was not checked. Evidence: the run 416b tests; recheck the row removal and the reservation entries after every ERPNext upgrade, before relying on Mark as Out of Stock.
- #416 invoice attachment (`fulfillment.attach_invoice_file`) relies on `frappe.utils.file_manager.save_file(fname, content, dt, dn, is_private=1)` creating a native private File attached to `dt`/`dn`, and on `File.get_content()` reading a private file from disk. The dialog's Attach field uploads a File attached to nothing, so its content is saved again onto the document the button was used on; the loose upload is deleted only when the copy got its own file path. Checked on the ERPNext 16.51.1 dev site only (frappe `file_manager.save_file` around L147); recheck on a Frappe upgrade that duplicate-content handling still gives each attached File its own record.
- Verified: ERPNext 16.51.1 / Frappe 16.52.0 on dev (`test.localhost` tests in `services/test_sales_order_sync.py`); `allow_zero_qty_in_sales_order` = 0 on test and dev. Prod versions and prod `allow_zero_qty_in_sales_order` NOT checked.
- Evidence: the tests on real submitted Sales Orders (`IntegrationTestSalesOrderRowChanges` in `services/test_sales_order_sync.py`).
- Recheck: on any ERPNext/Frappe upgrade, and before shipping to prod.
- #325 Sales Order hover card (`public/js/order_hover_card.js`) relies on `frappe.ui.hover_card` (`frappe/public/js/frappe/ui/components/hover_card.js`), on Desk list row markup (`list-row-container`, the subject link `.list-subject a[data-name]`, checkbox `data-name`, `level-right`), on the instance handle (`frappe.ui.hover_card` stores it in jQuery data `es-hover-card`, read once at attach time and kept in a `WeakMap` by row, since jQuery `.remove()` wipes that data before a MutationObserver runs), and on ERPNext selling `sales_order_list.js` assigning `frappe.listview_settings["Sales Order"]`.
- Verified: Frappe 16.51.0 on dev, 2026-10-22. Prod version NOT known; look it up (Frappe Cloud Apps tab).
- Evidence: the always-on test `marketplace_integrations/test_order_hover_card_js_wiring.py`, plus the framework source paths above.
- Recheck: on any Frappe/ERPNext upgrade, and before shipping to prod.
- #326 Navigation Dock mount (`dock/marketplace_integrations/marketplace_integrations.json`) relies on the Dock `mount_on` mechanism (`frappe/desk/doctype/dock/dock.py`: `mounted_apps`, `validate_added_rows` requiring icon and title), on `frappe/model/sync.py` `APP_ROOTED_DOCTYPES` importing `<app>/dock/<app>/<app>.json` and `<module>/sidebar/<name>/<name>.json`, and on `resolve_sidebar` dropping a shell with no permitted item. Mounted rows are appended after the host's rows; a saved site or user Dock layer drops entries it does not name, so on such a site the entries must be added in Manage Dock.
- Verified: Frappe 16.52.0 / ERPNext 16.51.1 (prod commits 4515014 / 5af6561; prod has no saved Dock layers, checked 2026-10-22), on `test.localhost`.
- Evidence: the always-on test `marketplace_integrations/tests/test_navigation.py` (`UnitTestDockFrameworkWiring`, plus per-role resolution through `resolve_sidebar`).
- Recheck: on any Frappe/ERPNext upgrade, and when prod gains a saved erpnext Dock layer.
- Epic #413 (a) Delivery Note Submit wait (`public/js/delivery_note_shipping.js`, `before_submit`) relies on the form's `before_submit` promise flow and `frappe.validated` (`frappe/public/js/frappe/form/form.js`: `frappe.validated = true; me.script_manager.trigger("before_submit").then(function () { if (!frappe.validated) { return me.handle_save_fail ...`), on `frm.reload_doc()` refreshing the open document after the worker bumped `modified`, and on `frappe.db.get_value` polling.
- Verified: Frappe 16.52.0 / ERPNext 16.51.1 on `test.localhost` (server side); the browser flow (reload before the native submit saves) is NOT yet verified and needs a browser pass.
- Evidence: `test_the_form_still_waits_for_a_before_submit_promise` in `integrations/bosta/test_delivery_note_shipment.py` matches that exact pattern in the installed `form.js` (not only the words), plus `delivery_note_shipping.test.js`.
- Recheck: on any Frappe/ERPNext upgrade, and after the first browser pass.
- Epic #413 (b) Integration Request schema: the Bosta log is the native Integration Request with Custom Fields (`custom_dedupe_key` unique, `custom_operation_type`, `custom_result`, ...). It relies on the unique index raising `UniqueValidationError` / `DuplicateEntryError` on a duplicate insert (the webhook catches it as a replay), on Integration Request granting only System Manager (rows are inserted with `ignore_permissions` after the caller's own check), and on Log Settings deleting old Integration Request rows (90 days), so no rule may depend on counting them.
- Verified: Frappe 16.52.0 on `test.localhost`.
- Evidence: `integrations/bosta/test_statuses.py` (dedupe), `integrations/bosta/test_delivery_note_shipment.py::test_the_lookup_first_rule_survives_the_deletion_of_the_log_rows`.
- Recheck: on any Frappe upgrade (schema of Integration Request, Log Settings defaults).
- #374 Item Marketplace Listings tab: `override_doctype_dashboards` (`services/item_dashboard.py::get_data`) relies on `Meta.get_dashboard_data` calling the hook with `data=` and using the returned dict, and on `non_standard_fieldnames` being honored for connection counts; Frappe's own count (`frappe.desk.notifications.get_doc_count`) uses `frappe.get_all` and is not row-filtered, so the dashboard data sets `method` (read by `form/dashboard.js` in place of `frappe.desk.notifications.get_open_count`) to `services/item_dashboard.py::get_open_count`, which calls Frappe's function and recounts only the Marketplace entry with `frappe.get_list`. `public/js/item.js` also reuses the native grid markup (`.form-grid-container > .form-grid > .grid-heading-row > .grid-row`), an unversioned CSS structure.
- Verified: Frappe 16.52.0 / ERPNext 16.51.1 on dev (source read only; browser pass NOT done). Prod version NOT checked.
- Evidence: `services/test_item_dashboard.py` (hook and `method` registered, ERPNext's own connections survive), `api/test_item_listings.py` (count equals each platform user's rows), `public/js/item.test.js`.
- Recheck: on any Frappe/ERPNext upgrade (including that the dashboard `method` key and `get_open_count` signature still exist), and before shipping to prod.
- Epic #413 (c) Orphan DocType removal order: in `frappe/migrate.py` (16.52.0) `run_schema_updates` runs pre_model_sync patches, `sync_all`, then post_model_sync patches; `post_schema_updates` then runs sync_jobs, sync_fixtures, dashboards, customizations, languages, flushes deferred inserts and only then calls `remove_orphan_doctypes()` (`frappe/model/sync.py`, docstring "Deleting the entry doesn't delete any data"). So a `post_model_sync` patch still sees the old DocType row of a removed doctype, fixtures are not yet synced while it runs (a patch that writes a new Custom Field must create it itself), and the table of a removed doctype is never dropped by migrate (`test.localhost` still has `tabBosta Shipment Item` with no DocType row). `migrate_bosta_shipments_to_delivery_notes` fails loudly when the DocType row exists but its table does not.
- Verified: Frappe 16.52.0 source read in the dev bench container, and the table check on `test.localhost`.
- Evidence: `patches/v0_0/test_migrate_bosta_shipments_to_delivery_notes.py::test_the_patch_fails_loudly_when_the_doctype_row_exists_but_the_table_does_not`.
- Recheck: on any Frappe upgrade.
- A Data Import provider's `validate()` warnings block the import by default; a warning dict with `"type": "info"` does not (`get_blocking_warnings` in `value_mapping.py` skips it). That is the only non-blocking channel of the provider API. The Data Import wizard (Frappe 16.52) hides the standard form layout, so `frm.set_intro` / dashboard headlines are invisible there and there is no hook for static help text.
- #373 Item Data Import and Export providers (`services/item_listing_import.py`) rely on `data_import_providers` in `hooks.py` (`frappe/core/doctype/data_import/import_provider.py`: `get_import_provider` uses only the LAST path registered for a doctype), on the provider hooks `get_import_fields`, `validate`, `import_row`, `get_export_rows` and on `Importer.insert_record` / `update_record` / `upsert_record` (`importer.py`), on `Importer` committing once per record and rolling back a failed one, on `Exporter` fetching extra-table rows only from `get_export_rows` without a permission check (so the provider reads with `frappe.get_list`), and on a provider table's docfields carrying `parent` (the importer takes the child DocType from `fields[0]["parent"]`). ERPNext registers providers for Customer and Supplier only (`erpnext/hooks.py`), none for Item. With a provider active, Frappe maps column headers from the provider schema, which has no alias slot for parent fields, so the native `ID (<autoname label>)` headers stop mapping; the provider adds them as copies of the autoname docfield (`_with_id_header_aliases`), served to every caller except the Export Data field picker request (`PICKER_CMD`), which would list the field three times. That suppression reads `form_dict.cmd`, so it works for the v1 API (`/api/method/...`) only. Item Manager has Marketplace Product Mapping read, write, create and report but no delete (DocPerm and the `has_permission` hook both). A no-change Update of an Item is a Success, not "No changes to update": `Importer.update_record` copies the document with `copy_doc` (clears `name` and the child row names), so `get_diff` is never empty for an Item with a child row (Frappe 16.52, dev; recheck on a Frappe upgrade).
- Registering a provider changes how the ID header is mapped: `_build_fields_dict_from_schema` maps `name`/`ID` to the `name` field, while the meta path maps it to the autoname field (`item_code` for Item), which `update_record` / `upsert_record` read. The provider therefore translates `name` to `item_code` in `import_row` and reads either column in `_listing_entries`.
- Verified: Frappe 16.52.0 / ERPNext 16.51.1 on dev (frappe 4515014, erpnext 5af6561), source read in the dev bench container. Prod NOT checked before release.
- Tests were written in `services/test_item_listing_import.py` but have NOT been run yet, so none of this is verified. Unverified until they run: how Data Import treats a blank Check cell (the code assumes blank cells are dropped from the row and tests it).
- Recheck: on any Frappe/ERPNext upgrade, and before shipping to prod.

## Skills (optional help)

Repository rules, this file and the installed framework version take precedence over any skill. Project-local: `.claude/skills/frappe-desk-ui/SKILL.md` for Desk client scripts. Third-party Frappe skill packs exist (community, not official, unreviewed here); none are installed. General frontend-design guidance may help with custom screens but stays subordinate to established Frappe patterns.
