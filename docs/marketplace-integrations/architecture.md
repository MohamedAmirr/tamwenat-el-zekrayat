# marketplace_integrations: target architecture

This document is the high-level source of truth for the marketplace integration architecture chosen for Tamwenat El Zekrayat.

It describes the target model and the boundaries already agreed for the new `marketplace_integrations` app. It is intentionally not a platform API specification. Platform-specific endpoint/auth/payload behavior must come from that platform's checked-in documentation before implementation.

## Status and app lifecycle

- `apps/marketplace_integrations/` is the target shared marketplace app (tracked by #33). It is created/migrated incrementally; do not assume a file exists until its issue is implemented.
- `apps/talabat_integration/` is the existing legacy Talabat implementation. It remains installed/usable during migration under #8 and must not be deleted before equivalent behavior/data is verified in the new app.
- New cross-marketplace functionality belongs in `marketplace_integrations`.
- Changes to `talabat_integration` should be limited to legacy fixes or explicit migration work.
- Talabat API truth remains `docs/talabat-integration/openapi.json` plus its README. The shared architecture does not replace the platform contract.

### What actually exists right now (#30 scaffold, #31 Marketplace Account, #32 Marketplace Product Mapping, #37 Talabat Connection + client, #38 Talabat data migration)

- `apps/marketplace_integrations/marketplace_integrations/hooks.py` sets `required_apps = ["erpnext"]` (its core models link to ERPNext Item/Warehouse/UOM, per the domain model below) and nothing else non-default.
- Bind-mounted into the dev bench the same way as `talabat_integration`: `./apps/marketplace_integrations:/home/frappe/bench-workspace/frappe-bench/apps/marketplace_integrations` in `docker-compose.dev.yml`.
- Installed on `dev.localhost` alongside `frappe`, `erpnext`, and `talabat_integration`; `bench --site dev.localhost migrate` and `bench --site dev.localhost run-tests --app marketplace_integrations` both run cleanly with all four apps present.
- **`Marketplace Account`** (`marketplace_integrations/marketplace_integrations/doctype/marketplace_account/`) — one document per external seller/store/vendor account and its ERPNext inventory partition, the generalized replacement for legacy `Talabat Vendor`. Fields: `account_name`, `platform` (Select, currently only `Talabat`), `external_account_id` (`ignore_xss_filter: 1` — opaque, byte-preserving), `connection_doctype` (Link → DocType, required, hidden/read-only — set programmatically by platform-specific code, not via the form) + `connection` (Dynamic Link, `options: connection_doctype`), `warehouse` (Link → Warehouse, optional — no stock fallback), `enabled`. Naming is expression-based and independent of identity (`autoname: "MKTACC-.#####"`), not a `field:`/custom-string autoname — the whole point is avoiding the identity-drift bug class `Talabat Product Mapping` had before its own #19 fix. Secondary (platform, external_account_id) uniqueness is a hidden `account_identity_key` field — a fixed-length SHA-256 hash of the pair (`compute_account_identity_key()`, mirroring `Talabat Product Mapping`'s `compute_item_vendor_key()` pattern), computed in `before_save()`, with a DB-level unique index plus a `validate()`-time friendly precheck. `validate()` also requires a `connection` and rejects one that doesn't exist as a document of `connection_doctype` (defense-in-depth alongside Frappe's own built-in Dynamic Link existence check). No generic platform registry/adapter framework exists yet — `connection_doctype` is a plain field until Talabat-specific code sets it under #37/#38.
- **`Marketplace Product Mapping`** (`marketplace_integrations/marketplace_integrations/doctype/marketplace_product_mapping/`) — one document per external marketplace listing mapped to one ERPNext Item, the generalized replacement for legacy `Talabat Product Mapping`. Fields: `marketplace_account` (Link → Marketplace Account, required), `item` (Link → Item, required), `external_reference` (Data, required, `ignore_xss_filter: 1` — the platform adapter's opaque canonical runtime lookup value; core mapping code never infers whether it's a SKU/barcode/product ID/variant ID), `external_product_id`/`external_sku`/`external_barcode`/`external_variant_id` (Data, optional, same `ignore_xss_filter: 1` opaque treatment), `sales_uom` (Link → UOM, optional), `enabled`, plus generic sync metadata (`sync_status` Select `Not Synced/Queued/Synced/Error`, `last_sync_reference` Data opaque read-only, `last_synced_on` Datetime read-only, `last_error` Small Text read-only — no sync logic itself lives here, just the fields the existing Talabat async catalog flow already needs). Same identity pattern as `Marketplace Account`: expression-based naming (`autoname: "MPM-.#####"`, `naming_rule: Expression`) fully decoupled from any external identifier, with a hidden `mapping_identity_key` field — a SHA-256 hash of `(marketplace_account, external_reference)` (`compute_mapping_identity_key()`), computed in `before_save()`, backed by a DB-level unique index plus a `validate()`-time friendly precheck. `before_save()` also writes the stripped `external_reference` back onto the field itself, not just into the hash, so the stored value and the key stay in sync. `marketplace_account` and `item` both set `search_index: 1` (high-cardinality lookup fields — thousands of rows per account expected); `title_field: "external_reference"` and `search_fields` covering `marketplace_account`/`item`/`external_reference` give Desk breadcrumbs/global search/Link fields something human-readable instead of the opaque `MPM-#####` name. Deliberately **not** unique on `(marketplace_account, item)` — the business model allows multiple listings in the same account to map to the same Item (e.g. a bundle vs. a single-unit listing). `services/mappings.py`'s `resolve_item(marketplace_account, external_reference)` is the one runtime entry point platform adapters call for exact mapping resolution — returns the mapped Item's name only when **both** the mapping and its `Marketplace Account` are enabled (a disabled account fails closed even if the mapping itself is still enabled); returns `None` if no such mapping exists or if a blank/missing `marketplace_account`/`external_reference` is passed (never raises). No fuzzy/fallback matching against `external_sku`/`external_barcode`/`external_variant_id` is ever attempted.
- **`Talabat Connection`** (`marketplace_integrations/marketplace_integrations/doctype/talabat_connection/`) — the first concrete Platform-specific Connection doctype, holding one Talabat Partner API credential/config set. A **non-Single** DocType (multiple real rows — a per-account connection inherently needs several, which a Single can't represent; see the resolved risk note below). Fields: `connection_name` (Data, `reqd`, title field), `client_id`/`client_secret`/`chain_id` (`reqd`, `ignore_xss_filter: 1` — opaque, byte-preserving; `client_secret` is a Password field, so it also never round-trips its plaintext through the ordinary document representation, only via `get_password()`), `environment` (Select `Sandbox`/`Production`, default `Sandbox`), `enabled` (Check, default checked), and the hidden/read-only `legacy_migration_source` marker used only by the one-time legacy backfill. Naming is expression-based (`autoname: "TALCONN-.#####"`, `naming_rule: Expression`), decoupled from every field — same rationale as `Marketplace Account`/`Marketplace Product Mapping`: none of `connection_name`/`client_id`/`chain_id` is guaranteed stable, and none should silently become part of `name`. No composite-uniqueness key field exists here (unlike those two doctypes) — there's no external-identifier pair this doctype needs to guard against duplicating; that's a deliberate scope call, not an oversight, and can be revisited if a real duplicate-connection problem shows up. Controller (`talabat_connection.py`) is intentionally thin: `get_base_url()` (environment → Talabat Partner API base URL) plus token-cache invalidation on update — no HTTP calls live on the DocType controller.
- **`patches/v0_0/migrate_talabat_accounts_and_mappings.py`** — the #38 post-model-sync, idempotent legacy-data migration. It treats each legacy source doctype independently: a missing table is an empty list, vendor-only data migrates its accounts, and mapping-only data fails rather than silently no-oping because a source vendor is required to establish the account. Where either source has rows, it requires exactly one #37 marker-tagged `Talabat Connection` (`legacy_migration_source = "talabat_settings"`), then preflights every source row before creating or updating matching `Marketplace Account` and `Marketplace Product Mapping` documents through normal `.insert()`/`.save()` paths so their identity keys remain valid. Blank vendor IDs, blank SKUs, blank or missing Items, missing source vendors, and duplicate normalized SKUs per vendor fail clearly before target writes. Vendor names, active state, item/SKU, and available sync metadata are copied; source rows/tables are never rewritten, deleted, or renamed, and no Warehouse is created or assigned. It safely no-ops only with no source rows.
- **`integrations/talabat/client.py`**'s `TalabatClient` — the Talabat Partner API client, one instance per `Talabat Connection` (`TalabatClient(connection.name)`). A faithful port of the proven `talabat_integration` app's `api/auth.py` + `api/client.py` (token caching with an expiry safety buffer, exactly one retry on a 401 after invalidating the cached token, one shared `requests` timeout/network-error wrapper via a module-level `_request()`, raw non-401 responses returned to the caller — callers check `response.ok`/status themselves). `get_access_token()`/`invalidate_access_token()`/`request(method, path, **kwargs)` are the same three operations the legacy module exposed, now as methods scoped to one connection instead of free functions scoped to the single global `Talabat Settings` Single. The one deliberate behavior change: the access-token cache key is `f"marketplace_integrations:talabat:access_token:{connection_name}"` — keyed **per Talabat Connection document**, not globally per environment like the legacy app (which only ever had one credential set per site) — so two connections in the same environment (e.g. two Talabat vendors both on Production) get independent tokens and one invalidating/refreshing its token never disturbs the other's. Saving a connection clears its own cache, so rotated credentials or a changed environment cannot reuse the old token. `patches/v0_0/create_talabat_connection_from_legacy_settings.py` (registered in `[post_model_sync]`) is a one-time, idempotent backfill that creates exactly one marker-tagged `Talabat Connection` (display `connection_name`: `"Talabat Settings (Migrated)"`) from the legacy `Talabat Settings` Single when it's configured (`client_id`/`chain_id` present, secret decryptable via `frappe.utils.password.get_decrypted_password()`) — it never touches or deletes `Talabat Settings`, and does nothing where the legacy DocType is not installed (the Single is detected via its `DocType` record, not a nonexistent normal table).
- **Talabat catalog mapping** (`integrations/talabat/catalog.py`, `api/talabat_catalog.py`, and Page `marketplace-talabat-catalog-mapping`) — the #39 port of live catalog browsing and explicit mapping maintenance to the shared models. The thin whitelisted API delegates to the Talabat catalog service, which authorizes the selected `Marketplace Account`, verifies its Dynamic Link is a `Talabat Connection`, and calls `TalabatClient` with that connection's chain ID and the account's external vendor ID. The service retrieves Talabat's paginated `/v2/chains/{chain_id}/vendors/{vendor_id}/catalog` endpoint, bounds page/page_size to the documented 1–500 range, and cross-references only that page against `Marketplace Product Mapping.external_reference` (Talabat SKU). It explicitly creates or updates mappings through normal document writes; multiple SKUs may point to the same Item. The non-colliding Desk page uses app-local copies of the generic `RemoteDataGrid`, retains pagination, stale-response protection, manual mapping, and explicit inline save, and uses text DOM APIs plus `frappe.utils.escape_html()` for every external/user-sourced value interpolated into a toast.
- Everything else in this document (import flows, and every remaining Talabat migration step below) is the **agreed target design**, not yet implemented — it lands incrementally under #36 and the Talabat migration steps below.

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
- customer creation/deduplication policy
- exact Sales Order/reservation/Delivery Note lifecycle for marketplace orders
- channel pricing source (including Talabat price source; #6 currently blocks on that business rule)
- production cutover/removal of `talabat_integration`

## References

- `docs/frappe-conventions.md`
- `docs/issue-conventions.md`
- `docs/talabat-integration/README.md`
- `docs/talabat-integration/openapi.json`
- #33 — Marketplace Integrations foundation
- #36 — Marketplace Product Management imports
- #8 — Talabat migration
