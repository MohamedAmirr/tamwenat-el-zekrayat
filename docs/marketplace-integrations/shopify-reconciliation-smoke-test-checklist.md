# Shopify Reconciliation Dashboard — Production Smoke-Test Checklist (#142)

Run this against a real (non-production-data) environment after deploying #139's
Workspace/Number Cards/Page, to confirm each dashboard section surfaces its
corresponding failure state end-to-end. Automated coverage for the underlying
queries lives in `services/test_shopify_reconciliation_dashboard_fixtures.py`
(one test per state below) — this checklist is the manual, whole-stack confirmation
that the Desk UI itself renders correctly, which an integration test against the ORM
doesn't prove.

For each state: create the fixture, refresh the dashboard, confirm it appears, then
delete/reset the fixture.

## 1. Shopify Catalog Push Errors

- [ ] Open a `Marketplace Product Mapping` row for a test account and set
      `sync_status = Error` (or trigger a real failed catalog push against a
      test/sandbox Shopify store).
- [ ] Open the **Shopify Reconciliation** workspace — the "Shopify Catalog Push
      Errors" card count increases by 1.
- [ ] Click the card — it opens the `Marketplace Product Mapping` list, filtered to
      `sync_status = Error`, and the test row is present.
- [ ] Reset the row (`sync_status` back to its prior value) and confirm the count
      decreases again.

## 2. Shopify Inventory Sync Errors

- [ ] Same as above, but with `inventory_sync_status = Error`.
- [ ] Confirm the "Shopify Inventory Sync Errors" card and its click-through list.

## 3. Shopify Webhook Deliveries: Error / Exhausted Retries

- [ ] Create (or wait for) a `Shopify Webhook Delivery` row at `status = Error`
      with `attempt_count < 5` — confirm it appears under "Shopify Webhook
      Deliveries: Error" but NOT under "Exhausted Retries".
- [ ] Confirm the hourly `retry_errored_webhook_deliveries()` scheduled job (or a
      manual `bench execute` of it) eventually clears it if the underlying cause is
      transient.
- [ ] Set (or wait for) a row to `attempt_count >= 5` at `status = Error` — confirm
      it now ALSO appears under "Shopify Webhook Deliveries: Exhausted Retries", and
      confirm the hourly sweep does NOT touch it (its own query excludes
      `attempt_count >= max_attempts` by design).

## 4. Marketplace Orders: Sync Error

- [ ] Force a `Marketplace Order` into `erpnext_sync_status = Sync Error` (e.g. sync
      an order whose ERPNext prerequisites are intentionally missing on a test site).
- [ ] Confirm the "Marketplace Orders: Sync Error" card count and click-through list
      show it.
- [ ] From the click-through list, open the order and use its existing resync retry
      button (#130) to confirm the remediation path still works from here.

## 5. Order Import failures (custom Page, Section 4)

- [ ] Trigger a Shopify order import that will fail (e.g. a test account whose
      Shopify Connection has an intentionally revoked/invalid token, or a
      deliberately malformed order id).
- [ ] Confirm a new `Marketplace Sync Log` row appears with `sync_type = "Order
      Import"` and `status = "Error"`.
- [ ] Open the **Shopify Reconciliation Dashboard** page — confirm the failure
      appears in the "Recent Order Import Failures" table, most recent first, with a
      working link to the log document.
- [ ] Confirm the SAME failure was also written to the Error Log
      (`frappe.log_error()`) — both must exist, not just one.

## 6. Refund evidence gaps (custom Page, Section 6)

- [ ] On a test/sandbox Shopify store, issue a refund on a test order so its
      `displayFinancialStatus` becomes `REFUNDED` or `PARTIALLY_REFUNDED`.
- [ ] Before the order is ever re-imported/re-synced, confirm its `Marketplace
      Order.refunds` field is still empty (`NULL`/`''`/`'[]'`) and that it appears in
      the dashboard's "Refund Evidence Gaps" table.
- [ ] Re-import the order (manual order import, or wait for the next
      webhook/reconciliation pass) so `refunds` gets populated.
- [ ] Confirm the order now DISAPPEARS from the "Refund Evidence Gaps" table, and
      that the existing refund Notification (#136) fired once for this transition.

## Sign-off

- [ ] All 6 states above were verified end-to-end in this environment.
- [ ] The Workspace and Page are only visible to System Manager users (confirm by
      checking as a non-System-Manager test user that neither the workspace nor its
      cards/page are reachable).
- [ ] No fixture data created for this checklist was left behind in the environment.
