---
name: frappe-desk-ui
description: Use when writing or changing Frappe Desk UI in marketplace_integrations - form/list client scripts, custom buttons, dialogs, Link filters, dashboard banners, child-table events, and their node tests and Arabic strings.
---

# Frappe Desk UI

Project rules live in `docs/frappe-conventions.md` ("Client scripts", "Translations"). This skill is the working checklist; read those sections for the why behind each gotcha. Framework-API facts marked (docs) come from docs.frappe.io (Form Scripts, Dialog pages), checked 2026-10-22; everything else is this repo's own experience.

## Before writing

0. Also read `docs/frappe-best-practices.md` (cross-cutting rules) and `docs/ui-customization.md` (workspace, lists, reports, pages).
1. `graphify query` the doctype or screen first, then Read the source. Existing references: `bosta_shipment.js`, `sales_order.js`, `sales_order_list.js`, `marketplace_order.js`, `remote_data_grid.js`, `shopify_catalog_push_status.js`.
2. Dev and prod can drift. Dev was verified at Frappe 16.51.0 on 2026-10-22 (`apps/frappe/frappe/__init__.py` in the bench container); prod's version was not rechecked, so look it up (Frappe Cloud Apps tab) before framework-UI work.
3. Own doctype: `<doctype>.js` beside it. Non-owned doctype (Sales Order): `public/js/<name>.js` plus `doctype_js` / `doctype_list_js` in `hooks.py`, wrapped in an IIFE.

## Form script rules

- (docs) Event order: `setup` (once per form instance, when the form is first created in the session) -> `before_load` -> `onload` -> `refresh` -> `onload_post_render`. Save: `validate` -> `before_save` -> `after_save` -> `refresh`.
- (docs) `frm.set_query` goes in `setup` or `onload`. Buttons, intro text and display toggles go in `refresh` (it runs on every render).
- (docs) Guard actions that need a saved record with `frm.is_new()`.
- (docs) Reuse logic with `frm.trigger("event")`, not copy-paste. Child-table handlers live in the parent doctype's file, using `cdt`/`cdn`.
- (docs) `frm.dirty()` before `frm.save()` when you changed values in code, or save says "No changes in document".
- (docs) Buttons: `frm.add_custom_button(label, fn, group)`; `frm.remove_custom_button`, `frm.change_custom_button_type` for updates. Keep one group per feature (Bosta, Noon...) so the toolbar stays small.
- (docs) Client-side validation only covers the browser form. Anything that must hold goes in the Python controller or whitelisted method too.
- Gate a whitelisted call with `frappe.model.can_read(...)` / role checks first. A failed permission shows Frappe's "Not permitted" popup that a `.catch` cannot stop.
- A whitelisted method that returns `{ok: false, error}` resolves the promise: check `ok === false` on success, not only rejections.
- `frappe.call()` rejections: server text is in `error.responseJSON._server_messages` (parse twice, strip tags, escape again).

## Dialogs and prompts

- (docs) `new frappe.ui.Dialog({title, fields, size, primary_action_label, primary_action(values)})`, then `d.show()`; `d.hide()` inside `primary_action`. Helpers: `frappe.confirm(msg, yes, no)`, `frappe.prompt`, `frappe.msgprint({title, indicator})`, `frappe.show_alert({message, indicator}, secs)`.
- Disable the primary action or show progress while a call is running so users cannot double-submit.
- Link controls: use the repo notes (blur-only `change`, double fire, `set_input` vs `set_value`, awesomplete select).

## Escaping and rendering

- Every value from an external API or an `ignore_xss_filter` field goes through `frappe.utils.escape_html()` at the point of interpolation: `msgprint`, `show_alert`, `set_headline`, `set_intro`, HTML option labels, Autocomplete. See conventions for JSON fields and Autocomplete formatters.
- `frappe.DataTable` does not exist on plain Desk pages. Use `RemoteDataGrid` or DOM plus `make_control`.
- Globals from `frappe.require()` exist only inside its `.then()`.

## Strings and Arabic

- UX: follow the "UX principles" section of `docs/ui-customization.md` (undo vs confirm, error wording, bulk actions, forms, RTL, WCAG 2.2).
- Wrap every visible string in `__()`. New or reworded text needs its Arabic row in `marketplace_integrations/translations/ar.csv` in the same PR, written with a CSV writer, with placeholders unchanged and product names in Latin script. Reuse terms already in `ar.csv`.
- Check layout in RTL (Arabic user) as well as LTR: no hardcoded left/right, no concatenated sentences (word order differs), one `__()` per full sentence with `{0}` placeholders.

## Tests

- Each `*.js` has a sibling `*.test.js` run by `node --test` (mocks `global.frappe`, `global.__`; see `bosta_shipment.test.js`). Export pure helpers for testing. Run:
  `cd apps/marketplace_integrations && node --test $(find marketplace_integrations -name '*.test.js' -not -path '*/node_modules/*' | sort)`
- Lint: `npx eslint --quiet --ext .js marketplace_integrations` with the CI ignore patterns from `docs/ci.md`.
- Final check in the real UI on `dev.localhost` (the user does this real-browser pass using the checklist in `docs/ui-customization.md`; agents prepare accounts/data and report it as "not done by agent, pending user"): form load, button, dialog, error path, Arabic. Cache: if a change doesn't appear, hard refresh or run `bench clear-cache`.

## Done checklist

JS + sibling test, eslint clean, `ar.csv` rows, docs updated if behavior changed (`docs/frappe-conventions.md` for a new gotcha), `graphify update .`.
