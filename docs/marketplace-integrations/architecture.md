# marketplace_integrations: target architecture

This document is the high-level source of truth for the marketplace integration architecture chosen for Tamwenat El Zekrayat.

It describes the target model and the boundaries already agreed for the new `marketplace_integrations` app. It is intentionally not a platform API specification. Platform-specific endpoint/auth/payload behavior must come from that platform's checked-in documentation before implementation.

## Status and app lifecycle

- `apps/marketplace_integrations/` is the target shared marketplace app (tracked by #33). It is created/migrated incrementally; do not assume a file exists until its issue is implemented.
- `apps/talabat_integration/` is the existing legacy Talabat implementation. It remains installed/usable during migration under #8 and must not be deleted before equivalent behavior/data is verified in the new app.
- New cross-marketplace functionality belongs in `marketplace_integrations`.
- Changes to `talabat_integration` should be limited to legacy fixes or explicit migration work.
- Talabat API truth remains `docs/talabat-integration/openapi.json` plus its README. The shared architecture does not replace the platform contract.

### What actually exists right now (#30 scaffold, #31 Marketplace Account, #32 Marketplace Product Mapping, #37 Talabat Connection + client, #38 Talabat data migration, #34 Item workbook import/export, #5 Mapping workbook import/export, #35 Product Management Desk page, #50 Item workbook scaling, #56 Marketplace Order foundation, #57 Talabat order retrieval + mapper, #58 Talabat order webhook, #59 Talabat order form presentation, #68 operational order UI, #61 ERPNext Sales Order sync, #72 Talabat outlet status service, #73 outlet controls)

- `apps/marketplace_integrations/marketplace_integrations/hooks.py` sets `required_apps = ["erpnext"]` (its core models link to ERPNext Item/Warehouse/UOM, per the domain model below) and nothing else non-default.
- Bind-mounted into the dev bench the same way as `talabat_integration`: `./apps/marketplace_integrations:/home/frappe/bench-workspace/frappe-bench/apps/marketplace_integrations` in `docker-compose.dev.yml`.
- Installed on `dev.localhost` alongside `frappe`, `erpnext`, and `talabat_integration`; `bench --site dev.localhost migrate` and `bench --site dev.localhost run-tests --app marketplace_integrations` both run cleanly with all four apps present.
- **`Marketplace Account`** (`marketplace_integrations/marketplace_integrations/doctype/marketplace_account/`) — one document per external seller/store/vendor account and its ERPNext inventory partition, the generalized replacement for legacy `Talabat Vendor`. Fields: `account_name`, `platform` (Select, currently only `Talabat`), `external_account_id` (`ignore_xss_filter: 1` — opaque, byte-preserving), `connection_doctype` (Link → DocType, required, hidden/read-only — set programmatically by platform-specific code, not via the form) + `connection` (Dynamic Link, `options: connection_doctype`), `warehouse` (Link → Warehouse, optional — no stock fallback), `erpnext_company` (Link → Company, optional until Sales Order sync), `erpnext_selling_price_list` (Link → Price List, optional until Sales Order sync), `erpnext_customer` (Link → Customer, read-only after the sync service creates its one generic Customer for this account), `enabled`. Sales Order synchronization fails clearly until its Company and Selling Price List are configured; it never falls back to user or site defaults. Naming is expression-based and independent of identity (`autoname: "MKTACC-.#####"`), not a `field:`/custom-string autoname — the whole point is avoiding the identity-drift bug class `Talabat Product Mapping` had before its own #19 fix. Secondary (platform, external_account_id) uniqueness is a hidden `account_identity_key` field — a fixed-length SHA-256 hash of the pair (`compute_account_identity_key()`, mirroring `Talabat Product Mapping`'s `compute_item_vendor_key()` pattern), computed in `before_save()`, with a DB-level unique index plus a `validate()`-time friendly precheck. `validate()` also requires a `connection` and rejects one that doesn't exist as a document of `connection_doctype` (defense-in-depth alongside Frappe's own built-in Dynamic Link existence check). No generic platform registry/adapter framework exists yet — `connection_doctype` is a plain field until Talabat-specific code sets it under #37/#38.
- **`Marketplace Product Mapping`** (`marketplace_integrations/marketplace_integrations/doctype/marketplace_product_mapping/`) — one document per external marketplace listing mapped to one ERPNext Item, the generalized replacement for legacy `Talabat Product Mapping`. Fields: `marketplace_account` (Link → Marketplace Account, required), `item` (Link → Item, required), `external_reference` (Data, required, `ignore_xss_filter: 1` — the platform adapter's opaque canonical runtime lookup value; core mapping code never infers whether it's a SKU/barcode/product ID/variant ID), `external_product_id`/`external_sku`/`external_barcode`/`external_variant_id` (Data, optional, same `ignore_xss_filter: 1` opaque treatment), `sales_uom` (Link → UOM, optional), `enabled`, plus generic sync metadata (`sync_status` Select `Not Synced/Queued/Synced/Error`, `last_sync_reference` Data opaque read-only, `last_synced_on` Datetime read-only, `last_error` Small Text read-only — no sync logic itself lives here, just the fields the existing Talabat async catalog flow already needs). Same identity pattern as `Marketplace Account`: expression-based naming (`autoname: "MPM-.#####"`, `naming_rule: Expression`) fully decoupled from any external identifier, with a hidden `mapping_identity_key` field — a SHA-256 hash of `(marketplace_account, external_reference)` (`compute_mapping_identity_key()`), computed in `before_save()`, backed by a DB-level unique index plus a `validate()`-time friendly precheck. `before_save()` also writes the stripped `external_reference` back onto the field itself, not just into the hash, so the stored value and the key stay in sync. `marketplace_account` and `item` both set `search_index: 1` (high-cardinality lookup fields — thousands of rows per account expected); `title_field: "external_reference"` and `search_fields` covering `marketplace_account`/`item`/`external_reference` give Desk breadcrumbs/global search/Link fields something human-readable instead of the opaque `MPM-#####` name. Deliberately **not** unique on `(marketplace_account, item)` — the business model allows multiple listings in the same account to map to the same Item (e.g. a bundle vs. a single-unit listing). `services/mappings.py`'s `resolve_item(marketplace_account, external_reference)` is the one runtime entry point platform adapters call for exact mapping resolution — returns the mapped Item's name only when **both** the mapping and its `Marketplace Account` are enabled (a disabled account fails closed even if the mapping itself is still enabled); returns `None` if no such mapping exists or if a blank/missing `marketplace_account`/`external_reference` is passed (never raises). No fuzzy/fallback matching against `external_sku`/`external_barcode`/`external_variant_id` is ever attempted.
- **`Talabat Connection`** (`marketplace_integrations/marketplace_integrations/doctype/talabat_connection/`) — the first concrete Platform-specific Connection doctype, holding one Talabat Partner API credential/config set. A **non-Single** DocType (multiple real rows — a per-account connection inherently needs several, which a Single can't represent; see the resolved risk note below). Fields: `connection_name` (Data, `reqd`, title field), `client_id`/`client_secret`/`chain_id` (`reqd`, `ignore_xss_filter: 1` — opaque, byte-preserving; `client_secret` is a Password field, so it also never round-trips its plaintext through the ordinary document representation, only via `get_password()`), `environment` (Select `Sandbox`/`Production`, default `Sandbox`), `enabled` (Check, default checked), and the hidden/read-only `legacy_migration_source` marker used only by the one-time legacy backfill. Naming is expression-based (`autoname: "TALCONN-.#####"`, `naming_rule: Expression`), decoupled from every field — same rationale as `Marketplace Account`/`Marketplace Product Mapping`: none of `connection_name`/`client_id`/`chain_id` is guaranteed stable, and none should silently become part of `name`. No composite-uniqueness key field exists here (unlike those two doctypes) — there's no external-identifier pair this doctype needs to guard against duplicating; that's a deliberate scope call, not an oversight, and can be revisited if a real duplicate-connection problem shows up. Controller (`talabat_connection.py`) is intentionally thin: `get_base_url()` (environment → Talabat Partner API base URL) plus token-cache invalidation on update — no HTTP calls live on the DocType controller.
- **`patches/v0_0/migrate_talabat_accounts_and_mappings.py`** — the #38 post-model-sync, idempotent legacy-data migration. It treats each legacy source doctype independently: a missing table is an empty list, vendor-only data migrates its accounts, and mapping-only data fails rather than silently no-oping because a source vendor is required to establish the account. Where either source has rows, it requires exactly one #37 marker-tagged `Talabat Connection` (`legacy_migration_source = "talabat_settings"`), then preflights every source row before creating or updating matching `Marketplace Account` and `Marketplace Product Mapping` documents through normal `.insert()`/`.save()` paths so their identity keys remain valid. Blank vendor IDs, blank SKUs, blank or missing Items, missing source vendors, and duplicate normalized SKUs per vendor fail clearly before target writes. Vendor names, active state, item/SKU, and available sync metadata are copied; source rows/tables are never rewritten, deleted, or renamed, and no Warehouse is created or assigned. It safely no-ops only with no source rows.
- **`integrations/talabat/client.py`**'s `TalabatClient` — the Talabat Partner API client, one instance per `Talabat Connection` (`TalabatClient(connection.name)`). A faithful port of the proven `talabat_integration` app's `api/auth.py` + `api/client.py` (token caching with an expiry safety buffer, exactly one retry on a 401 after invalidating the cached token, one shared `requests` timeout/network-error wrapper via a module-level `_request()`, raw non-401 responses returned to the caller — callers check `response.ok`/status themselves). `get_access_token()`/`invalidate_access_token()`/`request(method, path, **kwargs)` are the same three operations the legacy module exposed, now as methods scoped to one connection instead of free functions scoped to the single global `Talabat Settings` Single. The one deliberate behavior change: the access-token cache key is `f"marketplace_integrations:talabat:access_token:{connection_name}"` — keyed **per Talabat Connection document**, not globally per environment like the legacy app (which only ever had one credential set per site) — so two connections in the same environment (e.g. two Talabat vendors both on Production) get independent tokens and one invalidating/refreshing its token never disturbs the other's. Saving a connection clears its own cache, so rotated credentials or a changed environment cannot reuse the old token. `patches/v0_0/create_talabat_connection_from_legacy_settings.py` (registered in `[post_model_sync]`) is a one-time, idempotent backfill that creates exactly one marker-tagged `Talabat Connection` (display `connection_name`: `"Talabat Settings (Migrated)"`) from the legacy `Talabat Settings` Single when it's configured (`client_id`/`chain_id` present, secret decryptable via `frappe.utils.password.get_decrypted_password()`) — it never touches or deletes `Talabat Settings`, and does nothing where the legacy DocType is not installed (the Single is detected via its `DocType` record, not a nonexistent normal table).
- **Talabat outlet availability** (`integrations/talabat/outlet.py`, `api/talabat_outlet.py`, and the `Marketplace Account` form script, #72/#73) — per-account, synchronous status retrieval and update through `GET`/`PUT /v2/chains/{chain_id}/vendors/{vendor_id}/status`. The service reuses the enabled Talabat `Marketplace Account` + enabled linked `Talabat Connection` resolution used for operational orders, and always instantiates `TalabatClient` from that linked connection; it never falls back to legacy settings. `Marketplace Account` stores a generic, read-only cache of Talabat's **last confirmed** external status, close reason, close-until instant (converted from Talabat UTC to the Frappe system timezone), time checked, sanitized error, and full response JSON. Successful GET/PUT responses replace that cache and clear the error. A request or response failure preserves the last confirmed fields and records only the generic error, never the remote error body or an attempted state. PUT accepts only `OPEN`, `CLOSED_TODAY`, and `CLOSED_UNTIL`; closing requires Talabat's documented close reason, and a local Frappe datetime for `CLOSED_UNTIL` is serialized as UTC RFC3339. The POST-only API checks `Marketplace Account` write permission. On a saved Talabat account, the form renders the cache as read-only and offers Refresh Status plus only the status-appropriate controlled Open, Close Today, and Close Until actions; an unconfirmed/unknown cached state is refresh-only. Close Until uses Frappe's normal local DateTime field and passes it unchanged to the service. The client reloads only after Talabat confirms the returned state and uses a static sanitized failure message, so it never optimistically displays an attempted value. Non-Talabat account forms hide the entire section; `CLOSED` and `CHECKIN` remain non-actionable, and no schedule/background refresh exists.
- **Talabat catalog mapping** (`integrations/talabat/catalog.py`, `api/talabat_catalog.py`, and Page `marketplace-talabat-catalog-mapping`) — the #39 port of live catalog browsing and explicit mapping maintenance to the shared models. The thin whitelisted API delegates to the Talabat catalog service, which authorizes the selected `Marketplace Account`, verifies its Dynamic Link is a `Talabat Connection`, and calls `TalabatClient` with that connection's chain ID and the account's external vendor ID. The service retrieves Talabat's paginated `/v2/chains/{chain_id}/vendors/{vendor_id}/catalog` endpoint, bounds page/page_size to the documented 1–500 range, and cross-references only that page against enabled `Marketplace Product Mapping.external_reference` (Talabat SKU). It explicitly creates, updates, and re-enables mappings through normal document writes; multiple SKUs may point to the same Item. A mapped SKU can instead be non-destructively disabled through an authorized, idempotent service operation: it rejects missing, disabled, or unauthorized accounts/mappings, returns success when the mapping was already disabled, and preserves the mapping document for history. The Desk page explains the distinct Connection, Account, and saved-mapping concepts; it retains pagination, stale-response protection, manual mapping, explicit inline save, and confirmed disable action. It uses text DOM APIs plus `frappe.utils.escape_html()` for every external/user-sourced value interpolated into a toast or confirmation.
- **Item workbook import/export** (`api/item_import.py`, #34) — the first of the two implemented Product Management import flows. `export_items_xlsx()` builds one worksheet with one row per readable Item (`item_code`, `item_name`, `description`, `item_group`, `brand`, `stock_uom`, `disabled`, `barcode`, plus a bookkeeping `__modified` column) via `frappe.get_list("Item", ...)`, so it's naturally scoped to whatever Items the current user can read. `barcode` reflects only the lowest-`idx` Item Barcode child row — multiple barcodes, UOM conversion rows, supplier details, and variants are explicitly out of scope, matching "not a generic replacement for every ERPNext Item child table" below. The worksheet is deliberately named `"Data Export"`, not something more descriptive — this reuses a hard-coded exemption in the installed `frappe.utils.xlsxutils.make_xlsx()` that skips its lossy html2text auto-conversion of string cells (see `docs/frappe-conventions.md`); without it, `description` (a Text Editor/HTML field) would come back "changed" on every re-upload of an untouched export. `preview_items_import(file_url: str)` and `apply_items_import(file_url: str)` are both POST-only (`methods=["POST"]` — they mutate/preview a bulk write, unlike the plain-GET `export_items_xlsx()`) and both start by calling `_require_item_code_naming()`, which fails loudly with a `ValidationError` unless Stock Settings → Item Naming By is `"Item Code"` — with any other setting, `erpnext.stock.doctype.item.item.Item.autoname()` overwrites `item_code` with a generated naming-series value, silently breaking the "item_code is the stable identity" premise on every apply. `file_url` is read via `_read_uploaded_workbook()`, which loads the `File` doc itself, calls `file_doc.check_permission("read")` (raising `frappe.PermissionError` if the caller may not read it), and only then reads its content by filepath — closing both the missing-permission-check gap and the injection risk of an unchecked `frappe.get_doc("File", {"file_url": file_url})` filter lookup (the `file_url: str` type hint on both whitelisted functions makes Frappe's own `validate_argument_types` reject a non-str value, e.g. a list/dict, before it ever reaches that lookup). They share one parse/diff/validate pass (`_evaluate_rows()`): `item_code` is the stable identity (an unknown code is `new`, a known one is `changed`/`unchanged` by comparing every managed field including the primary barcode, with both sides of every comparison normalized the same way — e.g. stripped — so incidental whitespace never round-trips as a false "changed"); a duplicate non-blank `item_code` appearing on more than one row in the same workbook is reported as a row-level error for every such row, without a DB lookup/diff/write for any of them (order-dependent phantom errors or a raw `DuplicateEntryError` otherwise); `disabled` cell parsing treats blank/`None` as `1` (disabled, fail-safe) and only recognizes `1/0`, `yes/no`, `true/false` (case-insensitive) — anything else is a row error, never silently coerced; mandatory (`item_code`/`item_group`/`stock_uom`) and Link-target-exists checks run by hand (`frappe.db.exists`) rather than via a full `Item.validate()` call, so an unknown Item Group/Brand/UOM is a row error and is never auto-created — but this hand-rolled validation is explicitly best-effort, not a substitute for `Item.validate()`/`.insert()`/`.save()` itself (see `docs/frappe-conventions.md`); and normal `frappe.has_permission("Item", "read"/"write"/"create")` checks gate every row — no project-specific role is hard-coded. A `changed` row whose `__modified` doesn't match the Item's current `modified` is rejected as stale rather than applied (checked by hand for preview's benefit, since preview cannot write, and reinforced again for defense-in-depth at `apply`'s actual `doc.save()` via Frappe's own built-in `check_if_latest()` optimistic-concurrency mechanism — setting `doc.modified` from the workbook value before saving is what triggers that). `apply_items_import()` re-parses and re-revalidates from the uploaded file itself rather than trusting any client-supplied preview result, and writes row-by-row inside a per-row DB savepoint (`frappe.db.savepoint()`/`rollback(save_point=...)`/`release_savepoint()`): a write failure on one row (caught as `ValidationError`/`PermissionError`/`DuplicateEntryError`) is reported as that row's error, its own partial writes are rolled back to that row's savepoint rather than left committed, and rows already written earlier in the same request are unaffected. Item Price/price-list import, multiple barcodes, UOM conversion rows, supplier details, and variants remain explicitly out of scope per #34.
- **Marketplace Product Mapping workbook import/export** (`api/mapping_import.py`, `services/mapping_import.py`, #5) — one workbook row per external listing, keyed only by the customer-facing `(marketplace_account, external_reference)` pair, never a Frappe document name. The thin whitelisted API boundary delegates export, parse, preview, diff, revalidation, and apply to the dedicated service module; this is intentionally a separate layering choice from #34's one-file Item importer. The workbook contains the nine managed mapping fields plus `__modified` for stale-write protection. Export and preview's existing-record lookup explicitly require Mapping `read` permission, rather than relying on Frappe's select-only query behavior; normal Mapping `create`/`write` checks still authorize each proposed row. The importer explicitly checks File read permission before parsing an upload, validates Account/Item/UOM links without auto-creating them, and re-parses/re-validates on Apply. Valid rows are applied one-by-one under DB savepoints; a failed row reports its own error without retaining partial writes or discarding earlier rows. `external_reference` is identity and cannot rename/delete an existing listing: changing it represents a new mapping. Optional platform identifiers preserve their exact strings, while identity/Link fields normalize whitespace to match the Mapping DocType controller. `enabled = 0` disables, and a blank `enabled` cell defaults to enabled as agreed for #5.
- **Marketplace Product Management Desk page** (`marketplace_integrations/page/marketplace_product_management/`, #35) — a System Manager page at `/app/marketplace-product-management` with deliberately separate ERPNext Item and Marketplace Mapping sections. Each section exports only its own workbook, opens Frappe's native single-file XLSX uploader, sends the uploaded `File.file_url` to its matching server-side preview endpoint, and renders the server summary as separate New, Changed, Unchanged, and Errors groups. Apply is enabled only after a successful preview with at least one new/changed row; it submits the same uploaded File to the matching apply endpoint, then replaces the preview with the server's apply result. Each upload starts a new client-side operation, and preview/apply responses may alter the UI only when they still match that operation's File, preventing an out-of-order response from applying a different workbook than the one reviewed. The page never writes documents itself or exposes Frappe Data Import. It builds preview/errors/diffs with DOM text nodes rather than HTML interpolation, so externally-controlled mapping identifiers and server-returned row text cannot execute as markup.
- **Item workbook scaling** (`api/item_import.py`, `services/item_import_jobs.py`, #50) — Item export and Apply can be queued on Frappe's long worker queue so large workbooks do not hold the Desk request open. The page receives owner-targeted `marketplace_item_import_progress` realtime updates and also polls the authenticated job-status endpoint, whose short-lived cache state is visible only to the user who queued it; a completed export creates a private File for that user. The synchronous export/apply endpoints remain for API compatibility. Item Group, Brand, and UOM values are each fetched in one batched Link lookup per workbook evaluation rather than one `db.exists()` call per row; Item Barcode export queries are split into bounded `IN` chunks.
- **Marketplace Order foundation** (`marketplace_integrations/doctype/marketplace_order/`, `marketplace_integrations/doctype/marketplace_order_item/`, `services/marketplace_orders.py`, #56) — the non-submittable operational record for a Talabat order, uniquely keyed by `(Marketplace Account, Talabat order_id)` through a hidden SHA-256 identity key. It stores the selected customer/delivery subset (customer ID/name/phone, formatted address/city, coordinates), Talabat payment totals, and the complete latest payload JSON without creating ERPNext Customer, Address, Contact, or Sales Order records. Child rows are identified/upserted only by Talabat item `_id`; SKU remains a future exact-mapping input, and original versus fulfilled UNIT/KG quantity, weight, weighted-piece, and pricing values remain separate. Missing rows from a later payload are retained so partial recovery imports cannot erase replacement history.
- **Talabat order retrieval and mapper** (`integrations/talabat/orders.py`, `integrations/talabat/order_mapper.py`, `api/talabat_orders.py`, #57) — the manual/reconciliation half of Talabat order handling; the incoming webhook path is #58. `orders.get_account()` resolves and authorizes a Talabat `Marketplace Account` the same way catalog's does, plus two additions specific to order retrieval: both the account and its `Talabat Connection` must be `enabled` (catalog browsing has no such requirement; refreshing/importing operational order data does). `orders.get_order(account, order_id)` (`GET /v2/chains/{chain_id}/orders/{order_id}`) and `orders.get_vendor_orders(account, start_created_at_datetime=None, end_created_at_datetime=None, page=1, page_size=20)` (`GET /v2/chains/{chain_id}/vendors/{vendor_id}/orders`, `page_size` bounded to the documented 1–500) are both side-effect free — they return Talabat's raw `order.Order`/`order.PagedOrderHistory` JSON and never write a document. `get_vendor_orders()`'s argument names match the openapi's now-deprecated `start_created_at_datetime`/`end_created_at_datetime` query parameter names (matching #57's locked scope) but the actual HTTP call sends the current, non-deprecated `start_time`/`end_time` parameters — both parameter pairs mean the same thing per `docs/talabat-integration/openapi.json`. Because `get_order()` is addressed only by `order_id` (no vendor scoping in the URL itself, unlike `get_vendor_orders()`), it also verifies the fetched order's `client.id`/`client.store_id` (per `order.OrderClient` in the openapi) against the account's `external_account_id` before returning — a mismatch raises, so a `Talabat Connection` shared across vendors can't leak one vendor's order into another vendor's `Marketplace Account` via `refresh_order()`. A response with no `client` block at all has nothing to verify against and is let through.

  `order_mapper.normalize_order()` is the single Talabat `order.Order` → normalized-order translation path, shared by manual retrieval today and required for #58's webhook processing later; it maps Talabat's UUID `order_id` to the normalized `external_order_id` (the `Marketplace Order` identity join key), and separately preserves Talabat's own merchant-facing reference — itself confusingly also called `external_order_id` in Talabat's schema — as `platform_order_reference`. It preserves Talabat's exact status/enum spellings (e.g. `CANCELLED`), and always includes the complete raw order object as `latest_payload`. Timestamps (which may carry a trailing `Z` and fractional seconds) are UTC instants on the wire; `_parse_datetime()` converts each one to the site's system timezone via `frappe.utils.get_datetime()` (parses the ISO string into a UTC-aware `datetime`) followed by `frappe.utils.convert_utc_to_system_timezone()` (shifts the wall-clock fields to "System Settings > Time Zone") before formatting with `get_datetime_str()` — `get_datetime_str()` alone only formats a datetime's own fields and does not itself convert timezones, so skipping the conversion step would silently store the raw UTC digits as if they were already site-local wall-clock time. An unparseable timestamp is logged and normalizes to `None` rather than failing the whole order.

  The thin whitelisted `api/talabat_orders.py` is the only layer that calls persistence: `refresh_order(marketplace_order: str)` re-fetches one existing `Marketplace Order`'s current Talabat state by its stored `external_order_id` and reconciles it (a manual recovery action, not the normal webhook path) after checking `write` permission on that specific document; `import_vendor_orders(account: str, start: str, end: str)` requires an explicit date range, checks `read` on the `Marketplace Account` plus doctype-level `create`+`write` on `Marketplace Order`, and pages through vendor history until the documented `total_pages` is exhausted, upserting every order by the same `(Marketplace Account, order_id)` identity a future webhook will use — re-importing the same range is idempotent. `start`/`end` are passed straight through to Talabat's `start_time`/`end_time` query parameters and are interpreted as UTC directly, matching Talabat's own wire format — no local-timezone conversion is applied to them. Each order in the page is normalized and upserted inside its own DB savepoint (`frappe.db.savepoint()`/`rollback(save_point=...)`/`release_savepoint()`, the same per-row pattern `api/item_import.py`'s `_apply_items_import()` uses): a failure on one order (`ValidationError`/`PermissionError`/`DuplicateEntryError`) is rolled back to that order's savepoint and reported in the response's `errors` list (`{"order_id": ..., "error": ...}`) rather than aborting orders already imported earlier in the same call or the rest of the page. `import_vendor_orders()` still runs synchronously in the whitelisted request (unbounded paging inside the request, no `frappe.enqueue`) — queuing it as a background job with progress tracking, following the pattern `services/item_import_jobs.py` already established for Item workbook import/export, is deferred to a follow-up issue rather than done as part of #57. Neither function creates/updates an ERPNext Sales Order or Customer.
- **Talabat order webhook** (`api/talabat_webhooks.py`, `api/request.py`, `integrations/talabat/order_events.py`, #58) — `order_event()` is the public POST transport/authentication boundary. A narrowly scoped `before_request` hook converts Talabat's `Authorization: Bearer <shared-secret>` header to the raw value before Frappe's global OAuth validation, only for this exact route; other routes and already-raw webhook headers are unchanged. It validates a complete-enough `webhook.Order` payload (`order_id` plus Talabat client/vendor identity), resolves exactly one enabled Talabat `Marketplace Account` by `client.id` or `client.store_id`, requires its enabled linked `Talabat Connection`, and uses constant-time comparison against that connection's separate Password-backed `order_webhook_secret`. Invalid, disabled, missing-secret, unknown-vendor, and ambiguous-account deliveries fail before a worker is enqueued; neither the Authorization value nor the masked customer payload is logged. A valid delivery enqueues `process_order_event()` on the default worker queue and returns immediately. The worker first switches to Frappe's system user because the signed public request was received as Guest, then does no GET request: it delegates the delivered full order object through #57's shared `normalize_order()` and #56's idempotent `upsert_marketplace_order()`. `Talabat Connection` persists its read-only `order_webhook_url`, generated from the configured Frappe site URL and concrete method path. It is initialized for new and existing connections, while the form’s explicit Refresh and Copy actions let an administrator update the stored URL after a domain change and then separately replace that endpoint in Talabat’s portal; the secret is never part of the URL. The order URL/secret remain separate from the future catalog-feedback webhook configuration.
- **Talabat operational order form** (`Marketplace Order` form script, `api/marketplace_order_presentation.py`, #59, #68) — the standard Marketplace Order form exposes #56’s stored summary, customer/delivery, payment/totals, cancellation, and ERPNext-sync fields without creating Customer, Address, Contact, or Sales Order records. It is ordered for operational use: identity/current status, fulfillment timing, customer/delivery, items, payment totals, conditional cancellation details, then ERPNext synchronization/technical metadata. Its Refresh from Talabat action delegates to #57’s authorized refresh endpoint. Standard Frappe indicators show the Talabat order status and separate ERPNext sync status, always paired with their text values. The read-only item presentation API authorizes both the specific order and mapping visibility, then resolves each line’s current ERPNext Item through the exact `resolve_item(account, sku)` service; mapping results remain live presentation data and are never written back to Marketplace Order Item. The client uses an accessible read-only table and text DOM nodes for external values, calling out Mapped/Unmapped status, replacement IDs, UNIT/KG original versus fulfilled values, and Talabat prices. It deliberately does not calculate an ERPNext price comparison: #61's configured Price List is used only when a user creates or updates a Sales Order, and no `standard_rate`, arbitrary Item Price, or other fallback is read or inferred by the presentation API.
- **Talabat order actions** (`api/talabat_orders.py`, `integrations/talabat/orders.py`, #60) — authorized order actions use Talabat's single PUT update endpoint for controlled cart edits, replacements/additions, cancellation, and transport-specific dispatch/readiness. The form keeps the stored external status and transport metadata read-only, builds explicit item payloads in a dialog, and persists only the complete state returned by Talabat after a successful request. Server validation enforces the OpenAPI replacement relationship and quantity/weight rules; no local order mutation occurs when Talabat rejects an update.
- **ERPNext Sales Order synchronization** (`services/sales_order_sync.py`, `api/marketplace_orders.py`, and the Marketplace Order controller/form, #61) — an authorized Desk user explicitly sends a Marketplace Order to ERPNext. The service creates one linked, Draft-only Sales Order through ERPNext document APIs; it never runs from an import, refresh, or webhook and never submits, reserves stock, creates delivery, or invoices. Customer policy is one generic ERPNext Customer per Marketplace Account, stored in `erpnext_customer`; Talabat customer PII, Address, and Contact remain only on Marketplace Order. Commercial policy is ERPNext-owned: Sales Order lines use exact enabled Marketplace Product Mapping items and ERPNext's configured account Price List/rules, not Talabat fulfilled prices, totals, discount, tax, or fee fields. The Sales Order retains ERPNext's normal transaction-date default at the time of the explicit sync, while Talabat `promised_for` supplies its required Delivery Date; no Talabat timestamp sets price, tax, or other accounting data. The configured account Company and Selling Price List are mandatory sync prerequisites, with no implicit user/site default. Final `UNIT` line quantities are synchronized; `KG` lines block the whole synchronization until a later UOM policy is explicitly implemented. The deterministic snapshot contains only this commercial input (account configuration, mapped final UNIT lines, their quantities/UOM, and Delivery Date), so Talabat-only operational changes do not dirty it. A changed snapshot marks the record `Modified After Sync`; the same linked Draft Sales Order can be updated only by an explicit user action. A submitted linked Sales Order blocks re-sync and remains `Modified After Sync`. Talabat financial-total/tax/fee reconciliation is deliberately deferred to a separate issue; Talabat payment fields stay as auditable operational data and Sales Order totals can differ from `Marketplace Order.order_total`.
- Everything else in this document (including every remaining Talabat migration step below) is the **agreed target design**, not yet implemented — it lands incrementally under the relevant remaining issues.

## Domain model

```text
Platform-specific Connection
        │
        │ 1:N
        ▼
Marketplace Account ─────────────→ ERPNext Warehouse
        │                           (true stock partition)
        │ 1:N
        ▼
Marketplace Product Mapping
        │
        └─────────────────────────→ ERPNext Item
```

### Platform-specific Connection

A connection answers: **How do we authenticate and talk to this platform?**

Authentication/configuration differs by platform, so credentials are kept in typed platform DocTypes rather than one wide generic credential DocType.

Implemented concrete example (#37 — see "What actually exists right now" above for the full field/behavior list):

```text
Talabat Connection
- connection_name
- client_id
- client_secret
- chain_id
- environment
- enabled
- order_webhook_url (generated, persisted endpoint)
- order_webhook_secret (static Authorization token)
```

A generic abstract client/adapter hierarchy is deliberately deferred. Extract shared code only when a second implemented platform creates real duplication.

### Marketplace Account

A Marketplace Account answers: **Which seller/store/vendor account are we operating on, and which ERPNext stock partition does it use?**

Core relationship:

```text
Marketplace Account
- platform
- external_account_id
- connection (Dynamic Link to the platform-specific Connection)
- warehouse
- erpnext_company (required only for Sales Order synchronization)
- erpnext_selling_price_list (required only for Sales Order synchronization)
- erpnext_customer (generic Customer created on the first successful Sales Order sync)
- last confirmed external outlet-status cache (status/reason/until/check time/error/full payload)
- enabled
```

Examples:

```text
Talabat Vendor A ──→ Talabat Warehouse A
Talabat Vendor B ──→ Talabat Warehouse B

Shopify Main ──────┐
                   ├──→ Shared Online Warehouse
Instashop Main ────┘

Noon Main ─────────────→ Noon Warehouse
```

For this business, channel quantities are **true inventory partitions**, not virtual selling limits. ERPNext Warehouses are therefore the stock-pool model. Never duplicate the same physical quantity across multiple Warehouses merely to represent a sales limit.

### Marketplace Product Mapping

A mapping answers: **Which ERPNext Item does this external marketplace listing represent?**

One document represents one external listing:

```text
Marketplace Product Mapping
- marketplace_account
- item
- external_reference
- external_product_id (optional)
- external_sku (optional)
- external_barcode (optional)
- external_variant_id (optional)
- sales_uom (optional)
- enabled
- sync metadata
```

`external_reference` is the platform implementation's canonical runtime lookup value. Core mapping code does not guess whether the platform uses SKU, barcode, product ID, or variant ID.

Runtime resolution is exact:

```text
Marketplace Account + external_reference
        ↓
Marketplace Product Mapping
        ↓
ERPNext Item
```

No fuzzy matching or automatic SKU/barcode inference belongs in runtime order processing.

The model intentionally allows multiple external listings in the same Marketplace Account to point to the same ERPNext Item:

```text
ITEM-001
└── Talabat Account A
    ├── Listing 82191
    └── Listing 99281
```

Do not add a uniqueness rule on `(marketplace_account, item)`.

## Product management imports

Item maintenance and marketplace mapping maintenance are separate customer workflows.

### Item workbook

```text
one row = one ERPNext Item
```

The custom importer/exporter manages only the explicitly supported Item fields for its issue. It is not a generic replacement for every ERPNext Item child table.

Implemented under #34 — see "What actually exists right now" above for the full field/behavior list (`api/item_import.py`: `export_items_xlsx()`, `preview_items_import()`, `apply_items_import()`).

### Mapping workbook

```text
one row = one external marketplace listing
```

The customer supplies the business identity (Marketplace Account + external reference), not Frappe's internal document `name`.

Both import flows use:

```text
Export current data
        ↓
Edit XLSX
        ↓
Upload
        ↓
Server-side preview (no writes)
        ↓
Explicit Apply
        ↓
Re-parse + re-validate + write
```

The normal customer flow must not depend on Frappe Data Import's Insert/Update mode or internal ID column.

## Inventory rules

- Marketplace Account → Warehouse is the inventory partition boundary.
- Multiple Marketplace Accounts may share one Warehouse.
- A Marketplace Account may have its own Warehouse.
- Marketplace-facing stock reads must use the configured account Warehouse only; never silently sum other Warehouses or fall back to global stock.
- Missing Warehouse/configuration should fail closed according to the implementing issue, not invent a stock source.
- UOM conversion should use ERPNext's native Item/UOM model when a marketplace listing sells a pack/unit different from the Item stock UOM.

## Platform implementation boundaries

Platform-specific code owns:

- authentication/token lifecycle
- endpoint paths and HTTP semantics
- platform-specific identifiers used as `external_reference`
- payload normalization
- webhook authentication and payload validation
- product/catalog/order/stock API behavior documented by that platform

Shared code owns:

- Marketplace Account
- Marketplace Product Mapping
- ERPNext Item/Warehouse relationships
- shared product-management import/export UX
- exact mapping resolution

Do not add fields/methods for a future platform before its real API documentation and issue require them.

## Talabat migration

Talabat is migrated incrementally under #8:

1. ~~create `Talabat Connection` and move the proven auth/client behavior~~ — done, #37 (see "What actually exists right now" above)
2. ~~migrate legacy Talabat Vendor → Marketplace Account~~ — done, #38
3. ~~migrate legacy Talabat Product Mapping → Marketplace Product Mapping~~ — done, #38
4. ~~move the catalog mapping workflow to the new models~~ — done, #39
5. move catalog sync and async feedback handling

Legacy data is preserved until the migration is verified. Closed Talabat issues remain useful evidence of behavior/security decisions that the migration should preserve where still applicable.

**One forward-looking risk flagged during #31 review is now resolved: `Talabat Connection` (#37) was built as a non-Single DocType** (`Marketplace Account.connection_doctype` can point at it without hitting the Single-vs-Dynamic-Link `ProgrammingError` described in earlier revisions of this doc), confirmed by `Talabat Connection`'s own tests inserting multiple independent rows and `TalabatClient` caching tokens independently per row. That resolved risk's original detail is no longer repeated here — see git history if needed.

**Two forward-looking risks flagged during #31 review remain open — notes only, no code guard added yet (`Marketplace Account`'s `account_identity_key` computation is correct as-is for every write-path that exists today):**

- `account_identity_key` is only ever recomputed in `before_save()`, so it **goes stale if a future migration/patch writes `external_account_id` or `platform` via `frappe.db.set_value()` or raw SQL rather than `doc.save()`.** #38 avoids that risk by constructing and saving each `Marketplace Account` document normally. Any future bulk change must do the same, or explicitly recompute `account_identity_key` itself (via `compute_account_identity_key()`) after any bulk write.
- `Marketplace Product Mapping`'s `mapping_identity_key` has the identical exposure under direct writes. #38 likewise uses normal document writes per row. Additionally, `mapping_identity_key` embeds a Link value (`marketplace_account`, the document *name*, not a surrogate ID) — so it also goes stale if a `Marketplace Account` is ever **renamed**, since Frappe's rename machinery updates linked fields via direct `frappe.db.set_value()`-style writes, never through the linking document's own `.save()`/`before_save()`. This is currently latent (neither `Marketplace Account` nor `Marketplace Product Mapping` sets `allow_rename: 1`, so renaming isn't possible today) but worth recording now, before someone adds that flag later without realizing it would silently desync every `mapping_identity_key` referencing the renamed account.

## Not specified here

The following must be designed in their own issues using authoritative platform/ERPNext references; do not infer them from this document:

- Noon/Instashop/Shopify/Amazon/Bosta authentication or endpoints
- external order/webhook payload schemas
- customer creation/deduplication policy outside #61's one generic Customer per Marketplace Account
- exact Sales Order/reservation/Delivery Note lifecycle for marketplace orders
- channel pricing outside #61's explicit ERPNext-Price-List Sales Order policy (including Talabat catalog pricing; #6 currently blocks on that business rule)
- production cutover/removal of `talabat_integration`

## References

- `docs/frappe-conventions.md`
- `docs/issue-conventions.md`
- `docs/talabat-integration/README.md`
- `docs/talabat-integration/openapi.json`
- #33 — Marketplace Integrations foundation
- #34 — Item workbook import/export (implemented)
- #36 — Marketplace Product Management imports
- #8 — Talabat migration
