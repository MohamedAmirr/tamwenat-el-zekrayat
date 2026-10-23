# UI customization guide

How to change the Desk UI of `marketplace_integrations`. Shared rules (permissions, verification, version compatibility) are in `docs/frappe-best-practices.md`. JS gotchas are in `docs/frappe-conventions.md` ("Client scripts") and the project skill `.claude/skills/frappe-desk-ui/SKILL.md`. This guide does not decide which modules to keep or remove; it gives the method.

## Existing entry points

All paths are under `apps/marketplace_integrations/marketplace_integrations/` unless noted.

| Surface | Where | Notes |
| --- | --- | --- |
| Own doctype form | `marketplace_integrations/doctype/<name>/<name>.js` | Ships with the doctype. |
| Core doctype form (Sales Order) | `public/js/sales_order.js`, registered in `hooks.py` `doctype_js` | Wrap in an IIFE; shares scope with core script. |
| Core doctype list | `public/js/sales_order_list.js`, `doctype_list_js` | Extend `frappe.listview_settings`, never replace it. |
| Desk pages | `marketplace_integrations/page/` (product management, products, Shopify and Talabat catalog mapping) | Plain DOM plus `frappe.ui.form.make_control`; no `frappe.DataTable`. |
| Reusable table | `public/js/remote_data_grid.js` (`RemoteDataGrid`) | The one deliberate shared component. |
| Reports | `marketplace_integrations/report/` (channel status, needs attention, Shopify import failures, reconciliation summary) | Script Reports need the `report` permission on `ref_doctype`. |
| Workspaces | `marketplace_integrations/workspace/` (`tamwenat_marketplaces`, `shopify_reconciliation`) | |
| Dock + Sidebar shells (#326) | `dock/marketplace_integrations/marketplace_integrations.json` (Dock mounted on `erpnext`, rows Marketplaces and Bosta), `marketplace_integrations/sidebar/{marketplaces,bosta}/` | Entries on ERPNext's rail, no apps-screen tile of our own. Visibility is read permission on each item; guard: `tests/test_navigation.py`, `tests/test_sidebar.py`. |
| Fixtures | `hooks.py` `fixtures`, `fixtures/` | Custom Field, Property Setter, Client Script, etc. |

## Workspace and sidebar customization

- Workspaces, Sidebar documents and Docks are standard documents synced by `bench migrate`. The importer reimports a file only when its `modified` is newer than the DB row, so bump `modified` on every edit. Details and the Frappe 16.51+ Sidebar format: `docs/frappe-conventions.md` ("App-level folders auto-synced by `bench migrate`").
- Role-filtered navigation (#326): Sidebar and Sidebar Item have no roles field; an item shows when the user can read its target (a Workspace through its `roles`). A Section Break whose items are all filtered out still renders its header, so give every section at least one item for every role that sees the shell, or merge sections. The first item is the landing item: keep it one every platform role can open (Home = Workspace `Tamwenat Marketplaces`, whose roles include the three platform roles). Workspace shortcuts are filtered per user the same way, so a role-guarded shortcut needs no extra handling.
- Dock mount: the Marketplaces and Bosta rail entries come from `dock/marketplace_integrations/marketplace_integrations.json` (`mount_on: erpnext`, one Sidebar row each, icon and title required). Rows are appended after ERPNext's own rows. A site or user with a saved erpnext Dock layer drops entries it does not name, so on such a site add them in Manage Dock. Dock row and shell icons are Lucide ids (not every Lucide-looking name exists: `home` does not, `house` does).
- A shortcut without a `stats_filter` reopens the user's last-used filters. Give filtered or "show all" shortcuts an explicit `stats_filter`.
- Dev and prod can drift in Frappe version. Check both before depending on newer framework UI (Sidebar doctype); see `docs/frappe-best-practices.md` (Version compatibility).
- Sync check: after `bench migrate` on a fresh test site, the workspace and sidebar must appear without manual steps.

## Forms, lists, reports, custom pages

- Order of preference: standard field/setting, then report, then form or list script, then a custom page. A custom page is the last resort because it carries its own permission, translation and layout work.
- Forms and lists: use `doctype_js`/`doctype_list_js`, never edit core. Use the patterns in the `frappe-desk-ui` skill.
- Reports: filter by the user's permissions (`frappe.get_list`, not `get_all`, for user-facing rows).
- Custom pages: set the page's roles, authorize every server call, and render server values escaped.
- Loading, empty, success and error states are required on every async screen. Errors show the server message escaped, with a recovery action where one exists.

## Readable references in lists

- Prefer native settings: `title_field` + `show_title_field_in_link` on the target doctype, and `in_list_view` / `in_standard_filter` on real fields. Formatters are client display only; sorting, filtering and search must stay on real stored fields (`search_fields`).
- Frappe falls back to the docname in list and link display when the title field is empty (`Document.get_title()` itself returns `''`), so keep the title field filled (see `MarketplaceOrder.before_save`).
- Set `hide_name_column: true` in the list settings of a doctype with a `title_field`, or Frappe adds a second `ID` (docname) column next to the readable subject. Keep the docname reachable (form, search, plus the row tooltip from the shared `public/js/list_row_name.js` helper: `frappe.require` it in `onload` and guard against it missing; do not copy it per list). Link report columns get titles in one batched query; Dynamic Link columns get none, so send a label from the report and render it escaped in `<bdi dir="ltr">`.
- Do not repeat the title value as a list column: the Marketplace Order list shows the platform order number as the subject; `external_order_id` and `order_code` stay on the form and in `search_fields` but are not `in_list_view`. Bosta Shipment's subject is the tracking number; its status shows as the translated, coloured indicator pill only (a `get_indicator` makes Frappe drop the status field column, so a `status` formatter never runs).
- Never use a mandatory field with a default as `title_field` (the default is dropped on new docs, see `frappe-conventions.md`).
- The subject link is plain text: it takes no HTML from a formatter.

## Scoped styling

- Add CSS only when no standard control does the job. Scope every rule under a class owned by this app (for example a wrapper class on your page or dialog), never bare element selectors or global overrides.
- Use Frappe's CSS variables for color and spacing so light/dark and RTL keep working. No inline `left/right`; use logical properties (`margin-inline-start`).
- Ship CSS through the app's `public/` assets and hooks; no editing of framework CSS.

## Role-based navigation versus authorization

Navigation only controls what staff see, never what they can do. Authorization and its verification: `docs/frappe-best-practices.md` (Permissions, Verification).

## Arabic, RTL, accessibility, responsive

- Translations: `docs/frappe-conventions.md` (Translations). Check RTL alignment, icon direction, number/date formatting and truncated text.
- Keyboard operable with visible focus, labelled inputs, indicator colors paired with text.
- Check a phone-width viewport and a desktop width for any custom page or dialog.

## UX principles

Source note: these come from the internet (NN/g, Baymard, PatternFly, Material, W3C, Frappe docs), user-approved 2026-10-22, and were read from search summaries, not fully verified. Repo rules and the installed framework win on conflict.

- Reversible actions: offer undo or soft-delete; confirm only serious or irreversible actions to avoid dialog fatigue. https://www.nngroup.com/articles/ten-usability-heuristics/
- Error messages: plain language, say what is wrong and the fix, e.g. "Bosta rejected the city. Pick a city and retry", not "Error 400". https://www.nngroup.com/articles/error-message-guidelines/
- Bulk actions: show the bulk toolbar only when rows are selected; with pagination, say that "select all" covers the current page only; show progress and per-row success/failure results. https://www.patternfly.org/components/table/design-guidelines
- Forms: validate on blur, not while typing; clear the error as soon as it is fixed; prevent errors with defaults and constraints. https://baymard.com/blog/inline-form-validation
- RTL: mirror direction icons (arrows, progress); do not mirror numbers, phone numbers, charts or non-directional icons. https://m2.material.io/go/design-bidirectionality
- Bidi text: wrap SKUs, URLs and phone numbers in `<bdi dir="ltr">` or use `dir="auto"` (source partly dated). https://w3c.github.io/i18n-drafts/articles/inline-bidi-markup/index.en
- WCAG 2.2 AA additions: click targets at least 24x24 CSS px; a focused element must not be hidden by sticky bars or dialogs. https://www.w3.org/WAI/standards-guidelines/wcag/new-in-22/
- Cheap evaluation: heuristic evaluation with NN/g's 10 heuristics and the fillable workbook. https://media.nngroup.com/media/articles/attachments/Heuristic_Evaluation_Workbook_1_Fillable.pdf
- Frappe (official docs): the sidebar never grants permission, entries a user cannot open are hidden; changing the sidebar for everyone needs the Workspace Manager role; workspace access is set per workspace by roles. https://docs.frappe.io/framework/user/en/desk/navigation/sidebar and https://docs.frappe.io/framework/user/en/desk/navigation/workspace.md
- Open question (unverified, ask staff): Arabic-Indic (٠١٢) vs Western (0-9) numerals; Arabic type size and line-height are not sourced.

## Module decision matrix

Used when someone asks to simplify the UI. Decide per module, with the dependency check first.

| Decision | Meaning | When | Dependency check before doing it |
| --- | --- | --- | --- |
| Keep | No change | Staff use it, other code depends on it | None |
| Hide | Remove from workspace, sidebar or desktop only | Not needed for daily work, still reachable by URL | Does any flow link to it? Does the server still allow access (intended)? |
| Restrict | Change roles or permissions so some staff cannot use it | Some roles must not see or act on it | Which roles lose access; which jobs, APIs, reports or links run as those users; permission tests for both roles |
| Uninstall | Remove the module or app | Nothing depends on it and data is archived | Imports, hooks, fixtures, Link fields from other doctypes, scheduled jobs, patches, reports, translations; backup and a tested rollback |

Rules:
- Hiding is not restricting, and neither is uninstalling. Say which one you mean.
- Uninstall is destructive: it needs explicit user approval, a backup, and a rollback plan (restore point plus the revert commit).
- Scope of removal and any permission change is the user's decision. Record the decision and the dependency findings in the issue.

## Browser acceptance checks

The user performs the real-browser pass on the dev site using this checklist, not from unit tests. Agents prepare accounts, data and the checklist, and must report browser checks as "not done by agent, pending user".
1. Log in as an ordinary staff user and as Administrator.
2. Open the screen from navigation, then by direct URL, then from a list view.
3. Exercise the main action, the empty state and an error.
4. Repeat in English and in Arabic (RTL).
5. Keyboard-only pass: tab order, Enter and Escape in dialogs.
6. Narrow viewport pass for custom pages and dialogs.
7. Screenshot when the change is visual and will be reviewed.
8. For a hidden item: confirm the item is gone from navigation and the server still enforces permissions on its URL and API.

Agents report browser checks as "not done by agent, pending user"; never substitute test results for them.

## Deployment and rollback

- UI JS/CSS changes ship through the app; deploys follow `docs/frappe-cloud-publish.md`. Synced standard documents (workspace, sidebar, desktop icon) update on `bench migrate` with a newer `modified`.
- Browser caches: JS assets need a build/cache clear, and if a change doesn't appear, hard refresh or run `bench clear-cache` (form meta cache: `docs/frappe-conventions.md`, doctype_js bullet). Say what the deployer must run.
- Rollback: revert the commit and redeploy; for synced documents, the reverted file needs a newer `modified` than the DB row, otherwise migrate skips it. For permission or workspace changes made by fixtures or patches, write the reverse change before shipping.
- Take a backup before migrating a site with real data.
