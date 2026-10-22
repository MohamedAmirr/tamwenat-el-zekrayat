# Shopify Reconciliation Dashboard — Production Smoke-Test Checklist (#142)

Run this against a real (non-production-data) environment after deploying #139's
Workspace and Script Reports, to confirm each dashboard section surfaces its
corresponding failure state end-to-end. Sections 5 and 6 were originally a
hand-built custom Page; section 5 now ships as the "Shopify Order Import
Failures" native Script Report, and section 6 (originally also meant to ship
as a "Shopify Refund Evidence Gaps" Script Report) now ships instead as a
"Show Refunded Orders" list-view filter button on the Marketplace Order list.
Automated coverage for the underlying queries lives in
`services/test_shopify_reconciliation_dashboard_fixtures.py` (one test per state
below) and each report's own `test_*.py` — this checklist is the manual,
whole-stack confirmation that the Desk UI itself renders correctly, which an
integration test against the ORM doesn't prove.

For each state: create the fixture, refresh the dashboard, confirm it appears, then
delete/reset the fixture.

## 1. Shopify Catalog Push Errors

- [ ] Open a `Marketplace Product Mapping` row for a test account and set
      `sync_status = Error` (or trigger a real failed catalog push against a
      test/sandbox Shopify store).
- [ ] Open the **Shopify Reconciliation Summary** report — the "Shopify Catalog
      Push Errors (current)" row's `Value` increases by 1.
- [ ] Separately, open the `Marketplace Product Mapping` list filtered to
      `sync_status = Error` and confirm the test row is present (the Summary
      report's rows are plain counts with no click-through).
- [ ] Reset the row (`sync_status` back to its prior value) and confirm the
      Summary report's count decreases again.

## 2. Shopify Inventory Sync Errors

- [ ] Same as above, but with `inventory_sync_status = Error`.
- [ ] Confirm the "Shopify Inventory Sync Errors (current)" row in the Summary
      report, and the corresponding filtered `Marketplace Product Mapping` list.

## 3. Shopify Webhook Deliveries: Error / Exhausted Retries

- [ ] Create (or wait for) a `Shopify Webhook Delivery` row at `status = Error`
      with `attempt_count < 5` — confirm it is counted under the "Shopify Webhook
      Deliveries: Error (current)" row but NOT under "Exhausted Retries" in the
      **Shopify Reconciliation Summary** report.
- [ ] Confirm the hourly `retry_errored_webhook_deliveries()` scheduled job (or a
      manual `bench execute` of it) eventually clears it if the underlying cause is
      transient.
- [ ] Set (or wait for) a row to `attempt_count >= 5` at `status = Error` — confirm
      it now ALSO appears under "Shopify Webhook Deliveries: Exhausted Retries", and
      confirm the hourly sweep does NOT touch it (its own query excludes
      `attempt_count >= max_attempts` by design).
- [ ] Confirm both counts are reflected in the corresponding "Shopify Webhook
      Deliveries: Error (current)" / "Shopify Webhook Deliveries: Exhausted
      Retries (current)" rows in the **Shopify Reconciliation Summary** report.

## 4. Shopify Orders: Sync Error

- [ ] Force a `Marketplace Order` into `erpnext_sync_status = Sync Error` (e.g. sync
      an order whose ERPNext prerequisites are intentionally missing on a test site).
- [ ] Confirm the "Shopify Orders: Sync Error (current)" row in the **Shopify
      Reconciliation Summary** report reflects it.
- [ ] Separately, open the `Marketplace Order` list filtered to
      `erpnext_sync_status = Sync Error`, open the order, and use its existing
      resync retry button (#130) to confirm the remediation path still works.

## 5. Order Import failures (Shopify Order Import Failures report)

- [ ] Trigger a Shopify order import that will fail (e.g. a test account whose
      Shopify Connection has an intentionally revoked/invalid token, or a
      deliberately malformed order id).
- [ ] Confirm a new `Marketplace Sync Log` row appears with `sync_type = "Order
      Import"` and `status = "Error"`.
- [ ] Open the **Shopify Order Import Failures** report (via the Workspace
      shortcut, or Report List) — confirm the failure appears, most recent
      first, with a working link to the log document.
- [ ] Confirm the SAME failure was also written to the Error Log
      (`frappe.log_error()`) — both must exist, not just one.

## 6. Refund evidence check (Show Refunded Orders list-view filter)

- [ ] On a test/sandbox Shopify store, issue a refund on a test order so its
      `displayFinancialStatus` becomes `REFUNDED` or `PARTIALLY_REFUNDED`.
- [ ] Before the order is ever re-imported/re-synced, confirm its `Marketplace
      Order.refunds` field is still empty (`NULL`/`''`/`'[]'`) by opening the
      order document directly.
- [ ] On the `Marketplace Order` list view, click the **Show Refunded Orders**
      button — it filters to `platform = Shopify` AND
      `external_financial_status` in (`REFUNDED`, `PARTIALLY_REFUNDED`) — and
      confirm the test order appears in the filtered list even though its
      `refunds` field is still empty (i.e. evidence is missing).
- [ ] Re-import the order (manual order import, or wait for the next
      webhook/reconciliation pass) so `refunds` gets populated.
- [ ] Confirm the order's `refunds` field is now populated (open the document
      directly — it still appears under the **Show Refunded Orders** filter
      either way, since that filter is based on `external_financial_status`,
      not on whether `refunds` evidence exists), and that the existing refund
      Notification (#136) fired once for this transition.

## Sign-off

- [ ] All 6 states above were verified end-to-end in this environment.
- [ ] The Workspace, both Script Reports (Shopify Reconciliation Summary,
      Shopify Order Import Failures), and the Marketplace Order list view's
      **Show Refunded Orders** button are only visible/usable by System
      Manager users (confirm by checking as a non-System-Manager test user
      that none of the workspace, its reports, or the button are reachable).
- [ ] No fixture data created for this checklist was left behind in the environment.
