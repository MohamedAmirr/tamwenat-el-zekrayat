# Shopify Reconciliation Dashboard (#139) — Setup & Operations

Operator-facing guide to the "Shopify Reconciliation" Workspace and its custom Page
(`shopify-reconciliation-dashboard`). Both are System-Manager-only (see
`docs/marketplace-integrations/shopify-reconciliation-v1-exclusions.md` for what this
dashboard deliberately does NOT cover).

## Where to find it

Desk sidebar → **Shopify Reconciliation** workspace. It shows 5 Number Cards (click any
one to open the correctly filtered list view) plus a shortcut into the
**Shopify Reconciliation Dashboard** page for the sections a Number Card can't express.

## Number Cards (Workspace)

| Card | What it means | Underlying data | How to act |
|---|---|---|---|
| Shopify Catalog Push Errors | Count of `Marketplace Product Mapping` rows stuck at `sync_status = Error` — a price/listing push to Shopify failed for that mapping. | `Marketplace Product Mapping` | Open the mapping, check `last_error`, retry the push (single mapping button or bulk action from the list view). |
| Shopify Inventory Sync Errors | Count of `Marketplace Product Mapping` rows stuck at `inventory_sync_status = Error` — a quantity push failed. | `Marketplace Product Mapping` | Open the mapping, check `inventory_last_error`, retry inventory sync from the mapping form or list. |
| Shopify Webhook Deliveries: Error | Count of `Shopify Webhook Delivery` rows currently at `status = Error`. Most of these self-heal via the hourly `retry_errored_webhook_deliveries()` sweep. | `Shopify Webhook Delivery` | If the count stays non-zero across several hours, open a delivery and read its `error` field. |
| Shopify Webhook Deliveries: Exhausted Retries | Count of deliveries at `status = Error` AND `attempt_count >= 5` (`MAX_WEBHOOK_DELIVERY_ATTEMPTS` in `integrations/shopify/webhooks.py`). The hourly sweep's own query excludes these (`attempt_count < max_attempts`) — they will **never** self-heal. | `Shopify Webhook Delivery` | Manual investigation required. Read `error`/`topic`/`order_gid` on the delivery, fix the root cause, then manually re-trigger processing (e.g. via `bench console` calling `_process_delivery()`, or by resetting `attempt_count` if the fix makes success certain). |
| Marketplace Orders: Sync Error | Count of `Marketplace Order` rows at `erpnext_sync_status = Sync Error` — the order was imported but its ERPNext Sales Order sync failed. | `Marketplace Order` | Open the order; it already has a resync retry button (#130). Its `Marketplace Sync Log` rows (`sync_type = "Sales Order Sync"`) show why. |

## Custom Page sections

### Catalog Sweeps (Section 1 — unmapped variants)

One row per enabled Shopify `Marketplace Account`, showing:
- Last sweep age/status (from the account-keyed cache written by
  `run_catalog_sweep()`, read via `get_last_catalog_sweep()`).
- Exception counts broken out by category: **Unmapped** (a live Shopify variant with
  no enabled mapping), **Deleted** (a saved mapping whose Shopify variant no longer
  exists), **Variant-less** (a saved mapping whose `external_reference` isn't even
  shaped like a variant GID — usually a bad Excel-importer row).
- A **truncated** note if the sweep found more than 500 exceptions (only the first 500
  are cached/rendered; the sweep itself still processes the whole catalog).
- A **refresh failures** count if any mapping's descriptive-field refresh failed and
  was logged to the Error Log.

Click **Run sweep now** to queue a fresh sweep for that account — this reuses the
same background job/realtime-progress mechanism as the **Marketplace Shopify Catalog
Mapping** page. For the full live exception table with inline "assign an Item" editing,
open that page directly (there's no separate list view for exceptions — they're
computed fresh at sweep time, never persisted).

### Recent Order Import Failures (Section 4)

A table of the most recent `Marketplace Sync Log` rows where `sync_type = "Order
Import"` and `status = "Error"` — i.e., Shopify order retrieval/import itself failed
(as opposed to a successfully-imported order whose later ERPNext sync failed, which
shows up as `sync_type = "Sales Order Sync"` instead — see **`sync_type`** below).

Each row's `traceback` is the raw error captured at the time of the failed import
(also duplicated to the Error Log via the existing `frappe.log_error()` call in
`integrations/shopify/orders.py`, so nothing here replaces that established path —
this table just makes it queryable/dashboard-visible). There is no `Marketplace
Order` for these rows yet, since the order was never successfully imported.

To act: click a row to open the `Marketplace Sync Log` document and read
`request_data` (the account + order GID that failed) and `traceback`. Once the root
cause is fixed, re-run `queue_order_import()` for the affected account/date range, or
wait for the next `reconcile_all_accounts_orders()` scheduled sweep.

### Refund Evidence Gaps (Section 6)

A live query (no caching, no new field) for Shopify `Marketplace Order` rows where
`platform = "Shopify"`, `external_financial_status` is `REFUNDED` or
`PARTIALLY_REFUNDED`, and `refunds` is empty (`NULL`, `''`, or `'[]']`). This is a data
gap: Shopify's financial status says money was refunded, but this order's `refunds`
field — which should have been populated the next time the order was re-imported
(webhook or reconciliation) — never was.

To act: manually re-import the order (`import_order()`/the order-import page) to force
a fresh fetch, which should populate `refunds` and clear it from this table. If it
still shows up as a gap after a fresh import, the refund may genuinely not have
line-item detail in Shopify's API response yet — investigate on the Shopify admin side.

## `Marketplace Sync Log.sync_type`

`Marketplace Sync Log` now carries a `sync_type` Select field distinguishing two very
different kinds of attempt that both use this doctype:

- **`Sales Order Sync`** (the original, default value) — an attempt to sync an
  already-imported `Marketplace Order` into an ERPNext Sales Order
  (`services/sales_order_sync.py::sync_sales_order()`). Always has a
  `marketplace_order` set.
- **`Order Import`** — an attempt to retrieve/import a Shopify order from Shopify's
  API that failed before any `Marketplace Order` row existed to link to
  (`integrations/shopify/orders.py::import_orders_updated_between()`). Because there is
  nothing to link yet, `marketplace_order` is left blank for these rows — the field is
  only mandatory when `sync_type = "Sales Order Sync"`.

Filter the `Marketplace Sync Log` list view by `sync_type` to separate the two failure
populations; the dashboard's Section 4 table is already pre-filtered to
`sync_type = "Order Import"`.
