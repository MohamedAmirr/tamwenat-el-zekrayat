# Shopify Reconciliation Dashboard (#139) — Setup & Operations

Operator-facing guide to the "Shopify Reconciliation" Workspace. Sections that
were originally a hand-built custom Page (Order Import failures, refund
evidence gaps) now ship as native Frappe Script Reports instead, after an
HTML-escaping bug on the hand-built Page showed that class of bug is worth
avoiding by using Frappe's own report renderer. The dashboard's original 5
Number Cards have themselves since been replaced by the **Shopify
Reconciliation Summary** report below, because a Number Card can't offer a
live user-adjustable date range and the new business metrics (order value,
sync/webhook success rates, refunded amounts) need one. Everything here is
System-Manager-only (see `docs/marketplace-integrations/shopify-reconciliation-v1-exclusions.md`
for what this dashboard deliberately does NOT cover).

## Where to find it

Desk sidebar → **Shopify Reconciliation** workspace. It shows 5 shortcuts:
the **Shopify Reconciliation Summary**, **Shopify Order Import Failures**,
**Shopify Refund Evidence Gaps**, and **Shopify Refunded Orders** Script
Reports, and the existing **Marketplace Shopify Catalog Mapping** page (for
catalog-sweep access — see "Catalog Sweeps" below).

## Script Reports

### Shopify Reconciliation Summary

`marketplace_integrations/report/shopify_reconciliation_summary/` — a
standard Frappe Script Report. Its `execute()` calls
`services/shopify_reconciliation.py::get_reconciliation_summary()`, a
"metric name + value" row list (not a per-record table like the other three
reports below) covering 10 metrics in two groups:

**Current-state metrics (rows 1-5, ignore the `From Date`/`To Date` filter)**
— these reproduce the exact filter logic of the 5 Number Cards this report
superseded, so they carry over unchanged:

| Metric | What it means | Underlying data | How to act |
|---|---|---|---|
| Shopify Catalog Push Errors (current) | Count of `Marketplace Product Mapping` rows stuck at `sync_status = Error` — a price/listing push to Shopify failed for that mapping. | `Marketplace Product Mapping` | Open the mapping, check `last_error`, retry the push (single mapping button or bulk action from the list view). |
| Shopify Inventory Sync Errors (current) | Count of `Marketplace Product Mapping` rows stuck at `inventory_sync_status = Error` — a quantity push failed. | `Marketplace Product Mapping` | Open the mapping, check `inventory_last_error`, retry inventory sync from the mapping form or list. |
| Marketplace Orders: Sync Error (current) | Count of `Marketplace Order` rows at `erpnext_sync_status = Sync Error` — the order was imported but its ERPNext Sales Order sync failed. | `Marketplace Order` | Open the order; it already has a resync retry button (#130). Its `Marketplace Sync Log` rows (`sync_type = "Sales Order Sync"`) show why. |
| Shopify Webhook Deliveries: Error (current) | Count of `Shopify Webhook Delivery` rows currently at `status = Error`. Most of these self-heal via the hourly `retry_errored_webhook_deliveries()` sweep. | `Shopify Webhook Delivery` | If the count stays non-zero across several hours, open a delivery and read its `error` field. |
| Shopify Webhook Deliveries: Exhausted Retries (current) | Count of deliveries at `status = Error` AND `attempt_count >= 5` (`MAX_WEBHOOK_DELIVERY_ATTEMPTS` in `integrations/shopify/webhooks.py`). The hourly sweep's own query excludes these (`attempt_count < max_attempts`) — they will **never** self-heal. | `Shopify Webhook Delivery` | Manual investigation required. Read `error`/`topic`/`order_gid` on the delivery, fix the root cause, then manually re-trigger processing (e.g. via `bench console` calling `_process_delivery()`, or by resetting `attempt_count` if the fix makes success certain). |

**Date-ranged business metrics (rows 6-10, scoped to `From Date`/`To Date`
on the report, empty = no bound on that side)**:

| Metric | What it means | How it's computed |
|---|---|---|
| Total Shopify Orders Received (in selected range) | Count of Shopify `Marketplace Order` rows whose `received_on` falls in range. | `get_orders_received_count_in_range()`. |
| Total Order Value (in selected range) | Sum of `order_total` for those same orders. `order_total` is always populated from Shopify's `shopMoney` amount, i.e. it's in that order's `shop_currency`, never `presentment_currency`. If every order in range shares one `shop_currency` the sum is shown directly (with that currency in the `Detail` column); if more than one currency is present (e.g. multiple Shopify shops with different home currencies), the amounts are **not** silently added together — `Value` is blank and `Detail` shows each currency's own subtotal instead, e.g. `Mixed currencies, not summed: 100.00 EGP, 10.00 USD`. | `get_total_order_value_in_range()`. |
| Sales Order Sync Success Rate (in selected range) | Of the orders received in range, the percentage with `erpnext_sync_status = Synced` (vs. `Sync Error`, `Not Synced`, or `Modified After Sync`). `Detail` shows the raw `numerator/denominator`. With zero orders in range, `Value` is blank and `Detail` reads `0/0 - none in selected range` rather than dividing by zero. | `get_sales_order_sync_success_rate_in_range()`. |
| Total Refunded Amount (in selected range) | Sum of refund amounts for Shopify orders whose `last_platform_update_on` falls in range and whose `refunds` JSON is populated. Each element of `refunds` is a Shopify GraphQL `Refund` node (see `integrations/shopify/order_mapper.py` and `test_orders.py::make_order_payload()`); the amount summed per refund is `totalRefundedSet.shopMoney.amount` (an order can have more than one refund, all are summed). Same mixed-currency handling as Total Order Value above — grouped and flagged, not silently summed, using each refund's own `currencyCode`. | `get_total_refunded_amount_in_range()`. |
| Webhook Processing Success Rate (in selected range) | Of `Shopify Webhook Delivery` rows whose `received_at` falls in range, the percentage that reached `status = Processed` (vs. `Error` or still-pending `Received`). `Detail` shows the raw `numerator/denominator`; zero deliveries in range behaves like the sync success rate above (`Value` blank, no crash). | `get_webhook_processing_success_rate_in_range()`. |

All 10 metrics are computed fresh on every run — no caching, no new
persisted field — by `services/shopify_reconciliation.py`, the single source
of truth shared with the other three Script Reports below.

### Shopify Order Import Failures

`marketplace_integrations/report/shopify_order_import_failures/` — a
standard Frappe Script Report. Its `execute()` calls the same
`services/shopify_reconciliation.py::get_order_import_failures()` function
that ran this query before the conversion, so there is one source of truth
for the query, not two implementations that could drift.

Shows the most recent `Marketplace Sync Log` rows where `sync_type = "Order
Import"` and `status = "Error"` — i.e., Shopify order retrieval/import
itself failed (as opposed to a successfully-imported order whose later
ERPNext sync failed, which shows up as `sync_type = "Sales Order Sync"`
instead — see **`sync_type`** below), most recent first.

Each row's `traceback` is the raw error captured at the time of the failed
import (also duplicated to the Error Log via the existing
`frappe.log_error()` call in `integrations/shopify/orders.py`, so nothing
here replaces that established path — this report just makes it
queryable/dashboard-visible). There is no `Marketplace Order` for these rows
yet, since the order was never successfully imported.

To act: click the `Log` column to open the `Marketplace Sync Log` document
and read `request_data` (the account + order GID that failed) and
`traceback`. Once the root cause is fixed, re-run `queue_order_import()` for
the affected account/date range, or wait for the next
`reconcile_all_accounts_orders()` scheduled sweep.

### Shopify Refund Evidence Gaps

`marketplace_integrations/report/shopify_refund_evidence_gaps/` — a standard
Frappe Script Report. Its `execute()` calls the same
`services/shopify_reconciliation.py::get_refund_evidence_gaps()` function
that ran this query before the conversion, so there is one source of truth
for the query, not two implementations that could drift.

A live query (no caching, no new field) for Shopify `Marketplace Order` rows
where `platform = "Shopify"`, `external_financial_status` is `REFUNDED` or
`PARTIALLY_REFUNDED`, and `refunds` is empty (`NULL`, `''`, or `'[]'`). This
is a data gap: Shopify's financial status says money was refunded, but this
order's `refunds` field — which should have been populated the next time the
order was re-imported (webhook or reconciliation) — never was.

To act: click the `Order` column, or manually re-import the order
(`import_order()`/the order-import page) to force a fresh fetch, which
should populate `refunds` and clear it from this report. If it still shows
up as a gap after a fresh import, the refund may genuinely not have
line-item detail in Shopify's API response yet — investigate on the Shopify
admin side.

### Shopify Refunded Orders

`marketplace_integrations/report/shopify_refunded_orders/` — a standard
Frappe Script Report. Its `execute()` calls
`services/shopify_reconciliation.py::get_refunded_orders()`.

The general-purpose "show me every refunded order" view: ALL Shopify
`Marketplace Order` rows where `external_financial_status` is `REFUNDED` or
`PARTIALLY_REFUNDED`, regardless of whether `refunds` evidence is populated
— unlike **Shopify Refund Evidence Gaps** above, which only shows the subset
missing evidence. A `Has Refund Evidence` checkbox column (reusing the exact
same NULL/`''`/`'[]'` emptiness test as the gaps report) lets you see at a
glance which rows are also gaps.

Supports a native Frappe Report date-range filter (`From Date`/`To Date`,
shown as date pickers above the report table) on `last_platform_update_on`
— the field that changes when Shopify sends a refund update, as opposed to
`received_on` (the original order date, not relevant to "when was it
refunded").

## Catalog Sweeps

There is no dashboard section of its own for catalog sweeps (unmapped
variants, deleted mappings, variant-less mappings). Use the "Marketplace
Shopify Catalog Mapping" shortcut on the Workspace instead — that existing
page already shows the full live exception table (per enabled account, with
inline "assign an Item" editing) and the "Run sweep now" action, so nothing
here duplicates it.

## `Marketplace Sync Log.sync_type`

`Marketplace Sync Log` carries a `sync_type` Select field distinguishing two
very different kinds of attempt that both use this doctype:

- **`Sales Order Sync`** (the original, default value) — an attempt to sync
  an already-imported `Marketplace Order` into an ERPNext Sales Order
  (`services/sales_order_sync.py::sync_sales_order()`). Always has a
  `marketplace_order` set.
- **`Order Import`** — an attempt to retrieve/import a Shopify order from
  Shopify's API that failed before any `Marketplace Order` row existed to
  link to (`integrations/shopify/orders.py::import_orders_updated_between()`).
  Because there is nothing to link yet, `marketplace_order` is left blank for
  these rows — the field is only mandatory when `sync_type = "Sales Order
  Sync"`.

Filter the `Marketplace Sync Log` list view by `sync_type` to separate the
two failure populations; the **Shopify Order Import Failures** report is
already pre-filtered to `sync_type = "Order Import"`.
