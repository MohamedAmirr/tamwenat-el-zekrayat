# Noon FBPI fulfillment release notes (#146)

Covers only the Noon Sales Order eligibility/sync policy and FBPI shipment
fulfillment (create/cancel/add-AWB/mark-out-of-stock/shipment invoice) built
in #146. Returns import, the daily reconciliation job, and the reconciliation
Script Report are explicitly **out of scope** here — they are a follow-up
issue that depends on #146.

## What this ships

- Noon joins Talabat and Shopify as a supported platform for ERPNext Sales
  Order synchronization (`services/sales_order_sync.py`), with its own
  eligibility rule, line-grouping, date, currency and customer handling.
- A Noon-only "Noon Fulfillment" tab on Marketplace Order: Create Shipment,
  Cancel Shipment, Add AWB, Mark Out of Stock (UpdateOrder), Upload Shipment
  Invoice, and Retry — backed by three new child doctypes (`Marketplace
  Order Shipment`, `...Unit`, `...AWB`) and a `Fulfillment` sync_type on
  `Marketplace Sync Log`.
- No ERPNext stock or accounting side effects: no Delivery Note, Sales
  Invoice, Payment Entry, Credit Note, or stock ledger entry is ever created
  by any fulfillment action. Shipments are Noon-side evidence only.

## Setup

1. The Marketplace Account must already have `noon_warehouse_code` and
   `noon_country_code` set (required by #145's order import, reused here).
2. No new account-level configuration is needed for fulfillment itself — the
   same Noon Connection used for order import/pricing/inventory is reused.
3. The Noon Fulfillment tab and its action buttons only appear on a
   Marketplace Order whose `platform` is `Noon`.

## Permissions

**System Manager only.** `Marketplace Order`'s only configured DocPerm role
is System Manager (read/write/create/delete) — every fulfillment endpoint in
`api/noon_fulfillment.py` calls `doc.check_permission("write")` before doing
anything, so this is enforced through the existing doctype permission model,
not a separate check. There is no dedicated "Noon Fulfillment" role; granting
fulfillment access to a narrower audience than full Marketplace Order access
is not supported by this release.

## Lifecycle limits

- **Create Shipment**: only units that are `MP_ITEM_STATUS_CONFIRMED` +
  `INTEGRATION_ITEM_STATUS_ACKNOWLEDGED` in the order's `latest_payload`, and
  not already claimed by a non-Cancelled shipment or reserved for a queued
  Mark Out of Stock request, are shippable. A fresh
  `GetFbpiOrder` re-check happens right before the actual Noon call — units
  can fall out of eligibility between the request and the worker running.
- **Cancel Shipment** / **Add AWB** / **Upload Invoice**: only apply to a
  `Created` shipment. A duplicate AWB number on the same shipment is rejected;
  an invoice upload is rejected while an earlier one is Pending. Queued Add
  AWB and Upload Invoice workers recheck `Created` before calling Noon, so a
  cancellation completed during queue delay prevents the later mutation. An
  unsent AWB row becomes `Skipped`; an unsent invoice becomes `Error`.
- **Mark Out of Stock**: same shippable-unit rule as Create Shipment. Noon
  documents UpdateOrder as valid "before shipment creation" only. Selected
  units are reserved on the order before queueing, so Create Shipment cannot
  claim them while the job waits. The worker rechecks eligibility under the
  order lock before calling Noon and releases the reservation when it finishes.
  If enqueue fails, the request releases its persisted reservation immediately.
  Noon's handling of stale units still needs verification item #7 below.
- Shipment numbers (`<fbpi_order_nr>-<n>`) are never reused, even after
  cancellation — `n` counts every shipment ever created on the order.
- Retrying re-runs a shipment's last pending/error action only; it does not
  let you change units, AWB source, or invoice file after the first attempt.
  Add AWB and invoice retries require the shipment to remain `Created`; the
  retry response reports when its job is already queued. To ship different
  units, create a new (separate) shipment.
- A shipment stuck in `Error` (a failed Create that Retry can't move past)
  or `Cancel Pending` (a Cancel call that never got a 200 from Noon) can be
  recovered with the **Discard Shipment** action: it marks the shipment
  `Discarded` and releases its claimed units back to shippable, with no
  Noon call at all - this is the recovery path "create a new separate
  shipment" above referred to before Discard existed; the units a discarded
  shipment held are now free to be claimed by a brand-new shipment rather
  than being permanently stuck. A worker that died before releasing a
  queued Mark Out of Stock reservation (`noon_pending_oos_units`) can be
  recovered the same way via **Release Stuck Out of Stock Reservation**.
- A Noon AWB is generated once per shipment and reused on every retry — it
  is never regenerated. There is no bulk AWB pre-generation.
- Every fulfillment call is enqueued on the `long` queue and deduplicated by
  job ID — a second click while an action is already queued/running for the
  same shipment is a no-op toast, not a duplicate job. For Mark Out of Stock,
  the persisted unit reservation rejects an overlapping second request; each
  accepted request gets a distinct job ID so a recent completed job cannot
  suppress a new attempt after an error.
- GetShipment HTTP 404 is provisionally treated as "not found" for Create
  Shipment and Add AWB retries. Any other HTTP error, or a malformed 200 AWB
  list, blocks the subsequent mutating call. Noon's actual "not found"
  response remains to be verified against a real account (item #4 below).

## Known risk: divergence after submission (decision 8)

Per #131, a Marketplace Order's linked Sales Order is submitted immediately
on sync — this app never amends or cancels a submitted Sales Order. If a
unit later goes `OUT_OF_STOCK` or `CANCELLED` (via Noon's own action, our own
`Mark Out of Stock`, or our own `Cancel Shipment`), **the already-submitted
Sales Order keeps overstating the order, and nothing in #146 corrects it
automatically.**

What surfaces this:
- Immediately: the next poll's save runs `before_save` →
  `set_sync_status_from_current_snapshot()`, which detects the eligible-line
  hash changed (or raises `SyncBlocker` if no eligible units remain) and
  sets `erpnext_sync_status = "Modified After Sync"`.
- Later: the daily reconciliation job and Script Report, both deferred to
  the follow-up issue.
- The Desk UI shows a confirmation warning before `Mark Out of Stock` and
  `Cancel Shipment` when a submitted Sales Order (`erpnext_sales_order`) is
  already linked, but this is advisory only — it does not block the action.

Correcting the ERPNext side (amending the Sales Order, cancelling it, or
issuing a Credit Note) is a **manual accounting action** that this release
does not automate.

## Real-world verification checklist

There is no sandbox access for this issue — every behavior below is built
against the checked-in `docs/noon-integration/noon_api_full.md` contract
only and must be confirmed against a real Noon account before being relied
on in production:

1. **Initial `integration_status`/`mp_status` of new orders.** #146 assumes
   newly confirmed orders surface as `INTEGRATION_ITEM_STATUS_ACKNOWLEDGED`
   (the only status this release treats as shippable). If Noon's real
   default/initial value is `UNSPECIFIED` instead, nothing will ever become
   shippable until this is checked against real order data.
2. **Whether CreateShipment moves units to `SHIPPED`.** Not documented
   either way in the checked-in contract.
3. **Duplicate `integration_shipment_nr` is actually rejected** by
   CreateShipment — decision 15's retry-by-resend behavior relies on this.
4. **GetShipment on an unknown `integration_shipment_nr`** — confirm it
   returns HTTP 404 (not a 200 with an empty/misleading body), since 404 is
   provisionally treated as absence and 200 as "the shipment already exists."
5. **Duplicate AddShipmentCourierAwbs** (adding the same `awb_nr` twice) —
   confirm it is safe to resend, since the Add AWB retry path checks
   `GetShipment.awbs` first but falls back to resending if not found there.
6. **CancelShipment on an already-cancelled shipment** — confirm it is safe
   to resend, since GetShipment has no status field to check before retrying.
7. **UpdateOrder on an already-shipped or `UNSPECIFIED` unit** — confirm the
   actual error/response shape, since this release only prevents it
   client-side via the shippable-unit check, not via any Noon-documented
   validation.
8. **Invoice idempotency/supersede behavior** — confirm a repeated
   `idempotency_key` really returns the original result, and that a fresh
   key on a deliberate re-upload really supersedes the prior invoice.
9. **Error body shape per endpoint** — confirm `client.error_message()`
   produces a usable message for each of the seven fulfillment endpoints,
   not just the order-import endpoints #145 already validated this against.

Do not roll this out to production Noon fulfillment before these nine items
have been checked against a real Noon account.

## Out of scope (this issue)

- Returns import, the daily reconciliation job, and the reconciliation
  Script Report (follow-up issue, depends on #146).
- Order-level `UploadFbpiOrderInvoice` (shipment-level was chosen instead),
  bulk AWB pre-generation, `CreateSandboxOrder` tooling.
- Automatic Sales Order amendment or cancellation, Delivery Note, Sales
  Invoice, Payment Entry, Credit Note, or stock ledger entries.
- A Noon webhook/event-notification receiver (#145's own deferral — this
  release remains polling/manual-trigger only).
