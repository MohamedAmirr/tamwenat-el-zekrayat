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
- Base autoname on whichever field(s) form the record's **stable identity — never a field meant to change after creation.** The failure mode differs by autoname type, both confirmed directly against Frappe's source (`naming.py`/`base_document.py`), not assumed: for `autoname: field:X` (a single field mirrors `name`), `_sync_autoname_field` force-reverts any edit to `X` back to what `name` implies, on every save — the edit silently fails. For `autoname: format:...` (including a **custom `def autoname(self): self.name = ...`** controller method, needed when the identity fields need encoding beyond simple template substitution — e.g. to avoid a separator-collision risk, see the next bullet), `name` is only computed once, at insert (`set_new_name`) — Frappe never recomputes it on update. So changing an identity field on an existing record *does* persist the field's new value, but `name` silently goes stale/misleading relative to it. Either way, don't build the identity out of a field meant to change.
- Frappe has **no native composite/multi-field unique constraint.** For "field A + field B must be unique together" where neither alone is the record's identity, add a hidden (`hidden: 1`, `read_only: 1`) field with `unique: 1`, computed in a `before_save()` hook. **Don't join the values with a plain separator** (`f"{a}-{b}"`) unless you can prove neither field's real values can ever contain that separator — e.g. a bare `-` is a real collision risk for anything that might hold a UUID or a hyphenated code (`item="A-B", vendor="C"` and `item="A", vendor="B-C"` both produce `"A-B-C"`). Use an unambiguous encoding instead, e.g. `frappe.as_json([a, b])`, and give the field an explicit `length` generous enough for the combined encoded size. This gets a real DB-level unique index — a `validate()`-time `frappe.db.exists()` check alone is a check-then-insert race, not atomic.
- Any field storing an **opaque external identifier or secret** (API keys, external SKUs/IDs, tokens — anything whose exact bytes matter and is never rendered as HTML) must set `"ignore_xss_filter": 1` in its JSON. `Document.save()` runs HTML/XSS sanitization on any field value containing `<`/`>`, Password fields included, and will silently strip characters that merely *look* like an HTML tag — a real Talabat client secret was corrupted this way in production testing (a `<...>` substring silently deleted on save, no error). This is a real, confirmed footgun, not a hypothetical.
- Prefer **expression-based autoname** (`"autoname": "PREFIX-.#####"` — a literal prefix plus Frappe's built-in running-counter placeholder, no `format:`/field-reference needed once it contains `#`, confirmed against `naming.py`'s `set_name_from_naming_options`) when the record's identity fields shouldn't become `name` at all — e.g. because an external ID may change, or because encoding two fields into `name` risks the separator-collision class of bug described above. This computes `name` once at insert, from nothing but a shared counter — completely decoupled from any document field, so there's no naming-vs-identity coupling to reason about at all. Combine with a hidden `unique: 1` key field (same pattern as composite uniqueness above) for the actual business-identity constraint. Also set `"naming_rule": "Expression"` explicitly in the JSON alongside `autoname` — Frappe's `patch_old_naming_expressions` migration doesn't backfill this for hand-authored doctype JSON, so without it Desk shows an empty naming rule on the doctype's Settings, and a careless save from Desk could clear `autoname` outright.
- Any field feeding a hidden `unique: 1` composite/secondary key computed in `before_save()` (see above) must have **its own normalization done inside the key-computation function itself**, not left to Frappe's field-level normalization (e.g. `_validate_selects()` for `Select` fields, which strips/validates against `options`). That normalization runs in `_validate()`, which fires *after* `run_before_save_methods()` — i.e. after `before_save()` has already computed and stored the key from the raw, pre-normalization value. A value like `" Talabat "` would then be hashed untrimmed but persisted trimmed, silently decoupling the key from the row it identifies (confirmed directly: `document.py`'s `insert()`/`save()` call order is `_validate_links()` → `run_before_save_methods()` [`validate()`, `before_save()`] → `_validate()` [`_validate_selects()`, `_validate_mandatory()`, ...]). `_validate_selects()` is also skipped entirely under `frappe.flags.in_import`, so Data Import is a second path that bypasses it. Normalize (`cstr(...).strip()`, and reject/placeholder rather than silently stringifying `None`) inside the shared key function so it can't diverge from what ends up stored, regardless of validation order or whether some validation step gets skipped.
- Because core's `_validate_links()` runs *before* your controller's `validate()`/`before_save()` (previous bullet), a hand-written Dynamic Link existence check in your own `validate()` is normally dead code on the ordinary insert/save path — core's own `LinkValidationError` always wins the race first. A test asserting only `assertRaises(frappe.ValidationError)` around such a scenario will stay green even if you delete your own check, since core's exception is a `ValidationError` subclass too. If you want a test that actually proves your own check does something, either assert on the specific message/exception type, or bypass core's check with `.insert(ignore_links=True)` / `flags.ignore_links = True` and confirm your own check still fires — the same applies to a hand-written mandatory-field check racing against core's `_validate_mandatory()` (use `ignore_mandatory=True` to bypass core there).
- A **`Dynamic Link` field** (`fieldtype: "Dynamic Link"`, `options: <fieldname of the Link-to-DocType field>`) gets its target-document-exists check run automatically by Frappe core, in `_validate_links()` — confirmed in `frappe/model/base_document.py`'s `get_invalid_links()`. This runs *before* your controller's own `validate()`/`before_save()` (both on insert and on update), using whatever's in the `options` field as the target doctype: `frappe.db.get_value(self.get(df.options), docname, ...)`. So it already guarantees "this document literally exists in that doctype's table" without any hand-written check — but if the `options` field (e.g. a `connection_doctype`-style field) is unset while the Dynamic Link field has a value, the error is a generic `"{label} must be set first"`, not your own wording. Writing an explicit check in `validate()` anyway (`frappe.db.exists(self.connection_doctype, self.connection)`) is still worthwhile for a friendlier, doctype-specific error message and as defense-in-depth against any code path that sets `flags.ignore_links`.

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
- A `frappe.ui.form.make_control()` Link control with no backing Frappe document has no model to update asynchronously — call `set_input()` to render a server-returned value directly, not `set_value()` (which updates the model async and leaves the input blank since there's no document for it to attach to).
- Frappe's Link control only fires the DOM `change` event on blur, not when a suggestion is picked from the awesomplete dropdown — bind `awesomplete-selectcomplete` too if you need to react to the pick immediately, or the UI will appear to do nothing until the user clicks elsewhere.
- A page-field (`page.add_field()`) Link control's `change` handler fires **twice** per pick: once on the programmatic selection, once again on the following native blur. Guard against double-firing (e.g. compare against a "current value" variable) if the handler does something expensive like refetching data.
- `frappe.DataTable` is bundled only with the report/web-form JS bundles — it is not available on a plain Desk page (`frappe.pages`). Build custom tabular UI there with plain DOM + `frappe.ui.form.make_control()` instead, or use `frappe.require()` to explicitly pull in whatever bundle actually contains it.
- Frappe renders several things as raw HTML client-side: Awesomplete/Autocomplete option labels (`.html()` in `controls/autocomplete.js`), `frappe.toast`/`frappe.show_alert` messages, and `set_disp_area()` (defined in `controls/base_input.js`, inherited by every text-ish control) for any fieldtype outside its escape whitelist (`Data`, `Long Text`, `Small Text`, `Text`, `Password`, `MultiSelect` — notably NOT `Autocomplete` or `Select`). Any value sourced from an `ignore_xss_filter` field or an external API must be run through `frappe.utils.escape_html()` at the point of interpolation into any of these — the field-level `ignore_xss_filter`/server-side sanitizer does not protect client-side rendering. For an Autocomplete field specifically, pass a df-level `formatter: (value) => frappe.utils.escape_html(value)` to escape the *display* value without touching the raw `value` a `set_value()` call needs.

## App-level folders auto-synced by `bench migrate`

- An `<app_name>/<app_name>/workspace_sidebar/` directory (like `doctype/`, `page/`, `report/`) is a **Frappe app-level-folder convention** — every `*.json` fixture in it is synced automatically by `bench migrate` (via `import_file_by_path`). No `hooks.py` wiring is needed to pick it up.
- That importer decides whether to reimport a file by comparing its `modified` timestamp against the DB row's (falling back to a content hash for DocType records). A workspace_sidebar (or any other app-level-folder) JSON that's missing `creation`/`modified`/`modified_by`/`owner`/`docstatus`/`idx` gets treated as always-newer-than-DB, so every `bench migrate` deletes and reimports it — harmless functionally, but noisy and worth avoiding. Give it real timestamps and the standard Frappe document metadata fields, matching the format Frappe/ERPNext use for their own shipped fixtures (e.g. `frappe/frappe/workspace_sidebar/*.json` in the framework source). This cuts both ways once a real `modified` is in place: `is_db_timestamp_latest` treats an unchanged-or-older `modified` as not-newer-than-DB and **silently skips the import**, so any future hand-edit to the JSON must also bump `modified` (to a value newer than what's already in the DB on every site that has previously imported it) or the edit will appear to do nothing on `bench migrate`.

## Tests

- `class Test<DocType>(IntegrationTestCase)` (from `frappe.tests`) in `test_<doctype_name>.py`, using `frappe.get_doc(...).insert()` fixtures per test, not shared global state. `FrappeTestCase` is deprecated as of Frappe v16 — use `IntegrationTestCase` for tests that touch the DB/site, `UnitTestCase` for pure-logic tests that don't.
- Run with `bench --site $SITE_NAME run-tests --app <app_name>` (see `docs/docker-ops.md` for running this inside the container).
- To prove a hidden `unique: 1` composite-key field is enforced by MariaDB itself and not merely by your `validate()`-time precheck (relevant whenever `name` is independent/expression-based, so a duplicate-identity insert can't be relied on to collide on `name` the way a composite-string autoname would): set `doc.flags.ignore_validate = True` before calling `.save()` on an already-inserted document whose key field you've manually overwritten to collide with another row's. That flag makes `run_before_save_methods()` skip **both** `validate()` and `before_save()` together (confirmed in `document.py`) — so your own precheck and your own key-recomputation are both bypassed, and only the raw `UPDATE` in `db_update()` remains, which surfaces the real DB constraint as `frappe.UniqueValidationError` (or nothing, if the constraint isn't actually there).
- `IntegrationTestCase` auto-generates a generic test record for every doctype linked from the one under test's meta (including doctypes referenced only via a `Link` field like `warehouse`, not just fields you explicitly construct in a test). If that auto-generation transitively hits ERPNext's pre-existing Company/Fiscal Year overlap bug on this dev site (unrelated to whatever you're testing), add the offending doctype(s) to a module-level `IGNORE_TEST_RECORD_DEPENDENCIES` list and construct that dependency explicitly in the test instead — same pattern already used for `Item` in `talabat_product_mapping`'s tests and for `Warehouse` in `marketplace_account`'s.
- The failure above isn't necessarily "auto-generating a conflicting Company/Fiscal Year record" — for `marketplace_product_mapping`'s `sales_uom` (Link → UOM), the actual mechanism is that Frappe's test-dependency walker, while resolving `UOM` as a transitive Link target, imports `UOM Category`'s test module as a side effect (to check whether *it* has its own test-record requirements) — and that import itself (`erpnext/tests/utils.py`'s `ERPNextTestSuite`/`BootStrapTestData()`) runs at **module import time**, not record-creation time, and is what tries to create a colliding Fiscal Year. This means: (1) it's not just a doctype's *direct* Link fields that can trigger this — **any transitive Link-field hop** to a doctype whose test module imports `erpnext.tests.utils` can, so `IGNORE_TEST_RECORD_DEPENDENCIES` may need a new entry every time a new Link field introduces a fresh transitive path, even if that field's own direct target looks unrelated to Company/Fiscal Year; (2) `IGNORE_TEST_RECORD_DEPENDENCIES` only suppresses the *auto-generation* step for a scoped test run that starts its dependency walk from doctypes in your own app (e.g. `bench run-tests --app <app_name>` or `--module ...`) — it does **not** protect a whole-bench `bench run-tests` run, where the same transitive import can still be triggered by walking from some other app/doctype entirely.
- `IntegrationTestCase.doctype` is derived automatically by `UnitTestCase.setUpClass()` from the test file's own path/location (`frappe/tests/classes/unit_test_case.py`) — setting it as a class attribute on your test class has zero effect (it gets immediately overwritten), so don't add it as a convention.

## Naming/style quick reference

| Thing | Convention |
|---|---|
| Doctype name | Title Case (`Loan Application`) |
| Python module/file | snake_case |
| Python function/variable | snake_case |
| Fieldname | snake_case |
| App name | snake_case, matches folder |
| Custom fields on core doctypes | Prefix isn't required, but keep a consistent app-specific prefix if collisions are likely |
