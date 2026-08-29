# Frappe / ERPNext conventions

Reference for any custom app work in this project. Applies once a custom app exists under `apps/<app_name>/`.

## Never touch core

Never edit files inside the `frappe` or `erpnext` apps directly — those changes are lost on upgrade and untracked. All customization goes in:
- A custom app (new doctypes, business logic, overrides via hooks), or
- Framework-native customization tools (Custom Field, Property Setter, Client Script, Server Script, Custom DocPerm) — these are exportable as **fixtures** in the custom app so they're version-controlled.

## App layout

```
apps/<app_name>/
  <app_name>/
    hooks.py
    modules.txt
    <module_name>/
      doctype/
        <doctype_name>/
          <doctype_name>.json     # schema
          <doctype_name>.py       # server controller
          <doctype_name>.js       # client script (optional)
          test_<doctype_name>.py  # unit tests
```

## Doctype conventions

- Doctype **name**: Title Case, human-readable (`Sales Order`, `Member Card`). The folder/file name is the snake_case slug of that (`sales_order`).
- Field `fieldname`: snake_case, matches the Python attribute (`doc.customer_name`).
- Prefer the Frappe ORM over raw SQL: `frappe.get_doc`, `frappe.get_list`/`get_all`, `frappe.db.get_value`, `doc.save()`/`doc.submit()`. Reach for `frappe.db.sql` only when the ORM genuinely can't express the query, and always parameterize (`%s`, never string-formatted SQL).
- Controller hooks (`validate`, `on_submit`, `before_save`, etc.) go in `<doctype_name>.py` on the `Document` subclass — don't scatter business logic into API endpoints when it belongs on the doctype.
- Naming series / autoname: prefer built-in `naming_series` or `autoname` in the JSON over custom Python naming unless there's a real reason.

## hooks.py

- `doc_events` for cross-doctype side effects (e.g. reacting to a Sales Invoice submit) instead of monkey-patching core doctypes.
- `scheduler_events` for cron-style jobs; keep the job function itself in the app, not inline.
- `fixtures` to export/version Custom Field, Property Setter, Client Script, Custom DocPerm, Workflow, etc. so `bench migrate` on a fresh site reproduces them.
- `override_doctype_class` / `override_whitelisted_methods` are last resorts — prefer `doc_events` first.

## Permissions

- Default to the Role Permission Manager (exported as a fixture), not hardcoded permission checks, unless the rule is dynamic (e.g. "only the owning branch can see this").
- For dynamic rules, use a `permission_query_conditions` / `has_permission` hook in `hooks.py`, not ad-hoc checks scattered in controllers.

## Migrations

- Schema changes ship via the doctype JSON itself — `bench migrate` reconciles the DB schema from JSON automatically. You rarely hand-write DDL.
- Data migrations (backfills, renames, one-time fixes) go in `<app_name>/patches/<version>/<patch_name>.py`, registered in `patches.txt`. Patches must be idempotent — `bench migrate` can re-run on a site that partially applied one.
- Use `frappe.reload_doc(module, "doctype", "doctype_name")` inside a patch if it depends on a schema change from the same release.

## Whitelisted / API methods

- Expose Python functions to the client with `@frappe.whitelist()`; add `allow_guest=True` only for genuinely public endpoints.
- Validate/authorize inside the whitelisted function — being whitelisted is not itself a permission check.
- Return plain dicts/lists (JSON-serializable); don't return `Document` objects directly from list endpoints.

## Client scripts

- App-shipped JS lives next to the doctype (`<doctype_name>.js`) and ships with the app.
- Site-specific tweaks that shouldn't live in app code go in the **Client Script** doctype in the UI, then exported as a `fixtures` entry — don't hand-edit those JSON exports.

## Tests

- `class Test<DocType>(FrappeTestCase)` in `test_<doctype_name>.py`, using `frappe.get_doc(...).insert()` fixtures per test, not shared global state.
- Run with `bench --site $SITE_NAME run-tests --app <app_name>` (see `docs/docker-ops.md` for running this inside the container).

## Naming/style quick reference

| Thing | Convention |
|---|---|
| Doctype name | Title Case (`Loan Application`) |
| Python module/file | snake_case |
| Python function/variable | snake_case |
| Fieldname | snake_case |
| App name | snake_case, matches folder |
| Custom fields on core doctypes | Prefix isn't required, but keep a consistent app-specific prefix if collisions are likely |
