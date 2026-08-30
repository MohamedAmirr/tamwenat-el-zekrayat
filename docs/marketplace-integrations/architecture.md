# marketplace_integrations: target architecture

This document is the high-level source of truth for the marketplace integration architecture chosen for Tamwenat El Zekrayat.

It describes the target model and the boundaries already agreed for the new `marketplace_integrations` app. It is intentionally not a platform API specification. Platform-specific endpoint/auth/payload behavior must come from that platform's checked-in documentation before implementation.

## Status and app lifecycle

- `apps/marketplace_integrations/` is the target shared marketplace app (tracked by #33). It is created/migrated incrementally; do not assume a file exists until its issue is implemented.
- `apps/talabat_integration/` is the existing legacy Talabat implementation. It remains installed/usable during migration under #8 and must not be deleted before equivalent behavior/data is verified in the new app.
- New cross-marketplace functionality belongs in `marketplace_integrations`.
- Changes to `talabat_integration` should be limited to legacy fixes or explicit migration work.
- Talabat API truth remains `docs/talabat-integration/openapi.json` plus its README. The shared architecture does not replace the platform contract.

### What actually exists right now (#30 — scaffold only)

- A plain `bench new-app` scaffold, no business doctypes yet — `apps/marketplace_integrations/marketplace_integrations/hooks.py` sets `required_apps = ["erpnext"]` (its future core models link to ERPNext Item/Warehouse/UOM, per the domain model below) and nothing else non-default.
- Bind-mounted into the dev bench the same way as `talabat_integration`: `./apps/marketplace_integrations:/home/frappe/bench-workspace/frappe-bench/apps/marketplace_integrations` in `docker-compose.dev.yml`.
- Installed on `dev.localhost` alongside `frappe`, `erpnext`, and `talabat_integration`; `bench --site dev.localhost migrate` and `bench --site dev.localhost run-tests --app marketplace_integrations` both run cleanly with all four apps present.
- Everything else in this document (Marketplace Account, Marketplace Product Mapping, Platform-specific Connection, import flows) is the **agreed target design**, not yet implemented — it lands incrementally under #31, #32, #36, and the Talabat migration steps below.

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

Initial concrete example:

```text
Talabat Connection
- client_id
- client_secret
- chain_id
- environment
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

1. create `Talabat Connection` and move the proven auth/client behavior
2. migrate legacy Talabat Vendor → Marketplace Account
3. migrate legacy Talabat Product Mapping → Marketplace Product Mapping
4. move the catalog mapping workflow to the new models
5. move catalog sync and async feedback handling

Legacy data is preserved until the migration is verified. Closed Talabat issues remain useful evidence of behavior/security decisions that the migration should preserve where still applicable.

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
