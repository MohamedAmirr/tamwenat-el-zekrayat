# Noon FBPI fulfillment release notes (#146)

Covers the Noon Sales Order eligibility/sync policy and FBPI shipment
fulfillment (create/cancel/add-AWB/mark-out-of-stock/shipment invoice) built
in #146, plus the FBPI return reference lookup/evidence capture built in
#153 (see "Returns: FBPI return reference lookup and evidence (#153)"
below). The reconciliation Script Report is not built (findings are Needs Attention rows, #164) — see
"Out of scope" at the end of each section for what is and isn't covered.

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
4. The "Noon Fulfillment" button group shows only the actions that currently
   have something to act on, decided by one read-only call
   (`api.noon_fulfillment.get_available_actions`, read permission) using the
   same eligibility rules as the actions: Create Shipment and Mark Out of
   Stock need a shippable unit; Cancel Shipment and Add AWB need a Created
   shipment; Upload Invoice needs a Created shipment with no invoice upload
   Pending; Retry needs a Pending, Error or Cancel Pending shipment, or a
   Created one with a Pending Own Courier AWB or a Pending/Error invoice;
   Discard Shipment needs an Error or Cancel Pending shipment; Release Stuck
   Out of Stock Reservation needs a pending reservation. With no applicable
   action the group is absent, and a fully cancelled order shows a "Cancelled
   on Noon; no fulfillment actions available" headline. If the call fails the
   full button list is shown (each action still validates server side).

## Noon event-notification webhook (#163)

`api/noon_webhooks.py::event_notification` is a public, POST-only receiver
for Noon's FBPI event notifications — see
`docs/noon-integration/event-notifications.md` for the envelope shape,
sourced from Noon's public docs and user-approved for this work. It is an
additional, faster trigger for the
same `run_order_import` job #145's hourly poll already enqueues — **the
hourly poll stays on as the safety net**; nothing about it changes or is
removed by this receiver.

Setup, per Noon Connection:

1. Save the `Noon Connection`: `Webhook Secret` is generated automatically
   when empty (an existing value is kept) and the read-only `Webhook URL` is
   filled. Use the form's "Copy Webhook URL" / "Copy Webhook Key" buttons. If the
   site URL changes, use "Refresh Webhook URL" to re-derive and save it (it
   refuses on an unsaved form, so save first), then update the destination in
   Noon by hand.
2. In Noon's Partner Center, Integrator Configuration, Webhook tab, register
   a destination pointing at the connection's `Webhook URL`
   (`https://<your-site-domain>/api/method/marketplace_integrations.api.noon_webhooks.event_notification`).
3. In that same Webhook tab, set the header `X-Api-Key` to
   exactly the connection's `Webhook Secret`. Existing connections must
   change their Webhook tab header from `X-Noon-Webhook-Secret` to `X-Api-Key`
   manually.
4. Subscribe that destination to the `FBPI::ORDER_SYNC` event type. Any
   other event type delivered to this receiver is acknowledged (200) and
   ignored — it does not need to be excluded from the subscription, but
   subscribing only to what's handled avoids pointless deliveries.
5. The connection's `Project Code` field (already required for every Noon
   Connection) is what this receiver uses to resolve an inbound event back
   to the right connection/account — no separate configuration is needed for
   that resolution.

This receiver never parses `payload` as business data (see
`docs/noon-integration/event-notifications.md`) — a handled event only re-triggers the
existing #145 order-import job for the resolved account, which re-fetches
current order state from Noon's own API rather than trusting anything in
the notification body.

**Accepted delay when an import is already running:** a webhook event that
arrives while that account's import job is already queued/running does not
queue a second job (the existing per-account job_id dedup collapses it) -
the receiver still returns 200 and logs success, but that specific order
isn't actually re-imported until the already-running job (or, in the worst
case, the next hourly poll) picks it up, up to about an hour later. See
`docs/noon-integration/event-notifications.md`'s "Accepted dedup gap" for
the full explanation - this is accepted behavior, not a bug.

## Permissions

**System Manager only.** `Marketplace Order`'s only configured DocPerm role
is System Manager (read/write/create/delete/report) — every fulfillment endpoint in
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
- GetShipment "not found" for Create Shipment and Add AWB retries is HTTP 404
  or, as seen on production Noon (verification item #4), HTTP 400
  `INVALID_ARGUMENT` whose message contains "No shipment found". Any other
  HTTP error, or a malformed 200 AWB list, blocks the subsequent mutating
  call.

## Known risk: divergence after submission (decision 8)

Per #131, a Marketplace Order's linked Sales Order is submitted immediately
on sync. Since #179/#180 the reconciliation planner can cancel, close the
remainder of, or update the quantities of a submitted Sales Order (and reopen a
Closed one first) when a unit later goes `OUT_OF_STOCK` or `CANCELLED` (via
Noon's own action, our own `Mark Out of Stock`, or our own `Cancel Shipment`),
but only while nothing was delivered or billed and no affected line has started
fulfillment; every other case is left as a `Modified After Sync` advisory for a
person (delivered or billed orders need a return or credit note).

What surfaces this:
- #178 part 3: the poll now also bumps the Sales Order sync debounce when a
  LINKED order's evidence fingerprint changed (units cancelled/out of stock/
  shipped, shipment membership), and `integrations/noon/order_reconciliation.py`
  turns the per-unit states into reconciliation evidence (full cancellation,
  per-line fulfillment started, unknown stage for `UNSPECIFIED`). A `Noon Return`
  linked to the order is `return` evidence (#164); an own `Mark Out of Stock` is not
  distinguishable from a customer cancel. The ERP side is still corrected
  manually only when the planner cannot act (see above).
- Immediately (a linked, submitted order with no eligible unit left yields an empty snapshot for the reconciliation planner instead of raising `SyncBlocker`; a never-synced order still raises): the next poll's save runs `before_save` →
  `set_sync_status_from_current_snapshot()`, which detects the eligible-line
  hash changed and
  sets `erpnext_sync_status = "Modified After Sync"`.
- The hourly Noon order import bumps a changed linked order, and the
  platform-neutral hourly `sweep_stuck_sales_order_reconciliations` (#180)
  re-checks stuck (Modified After Sync / Sync Error) or Closed linked orders
  with trigger source `Scheduled Sweep`. Needs Attention (#164) lists
  shipments stuck in Pending longer than `NOON_PENDING_SHIPMENT_STUCK_HOURS`
  (1 hour, a placeholder with no documented basis; measured from the shipment
  row's creation, in the existing per-order Noon Fulfillment row; Cancelled and
  Discarded are excluded) and Noon Returns with no Marketplace Order
  (`Noon Return Unlinked`). A Noon Return linked to an order with a Sales Order
  re-queues its sync (debounce) and ends Modified After Sync with the
  compensating-transaction message; no Sales Order is changed. A return linked
  before the Sales Order exists (e.g. during Sync Error) is caught when that
  Sales Order is first created: the order ends Modified After Sync with the same
  message instead of Synced.
- The Desk UI shows a confirmation warning before `Mark Out of Stock` and
  `Cancel Shipment` when a submitted Sales Order (`erpnext_sales_order`) is
  already linked, but this is advisory only — it does not block the action.

Correcting the ERPNext side is automatic only where the planner may act (#179:
cancel, close the remainder, update quantities/rates; #180: reopen a Closed
order first). Amending after delivery, issuing a Credit Note or a return, and
anything else the planner reports as `Modified After Sync` is a **manual
accounting action**. Cancelling a Noon order from ERPNext is not supported
(#181 reports Noon as blocked: no order-level cancel).

## Real-world verification checklist

Sandbox test (user-reported, 2026-10-07): a sandbox order was created and
item 1 (initial status CONFIRMED + ACKNOWLEDGED) was observed on it. Items
2-9 (GetNoonLogisticsAWBs, CreateShipment, AddShipmentCourierAwbs,
CancelShipment, UploadShipmentInvoice, UpdateOrder out-of-stock) were NOT
tested. Sandbox orders carry mp_code `test_integration_mp`, which this app
skips, so they are never imported (sandbox support is out of scope).
Real-order verification is still required for every item. Every behavior
below is built against the checked-in `docs/noon-integration/noon_api_full.md`
contract and must be confirmed against a real Noon account before being
relied on in production:

1. **Initial `integration_status`/`mp_status` of new orders.** #146 assumes
   newly confirmed orders surface as `INTEGRATION_ITEM_STATUS_ACKNOWLEDGED`
   (the only status this release treats as shippable). Seen as
   `MP_ITEM_STATUS_CONFIRMED` + `INTEGRATION_ITEM_STATUS_ACKNOWLEDGED` on
   sandbox orders only; still unverified for real orders. If Noon's real
   default/initial value is `UNSPECIFIED` instead, nothing will ever become
   shippable until this is checked against real order data.
2. **Whether CreateShipment moves units to `SHIPPED`.** Not documented
   either way in the checked-in contract.
3. **Duplicate `integration_shipment_nr` is actually rejected** by
   CreateShipment — decision 15's retry-by-resend behavior relies on this.
4. **GetShipment on an unknown `integration_shipment_nr`** - verified on
   production: Noon returns HTTP 400 `INVALID_ARGUMENT` "No shipment found
   with integration_shipment_nr ...", not 404. Both are treated as absence
   (`_is_shipment_not_found()`); 200 means the shipment already exists.
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

## Out of scope (#146)

- Order-level `UploadFbpiOrderInvoice` (shipment-level was chosen instead),
  bulk AWB pre-generation, `CreateSandboxOrder` tooling.
- Automatic Sales Order amendment after delivery (cancellation of an
  undelivered, unbilled order is done by the reconciliation planner, #179), Delivery Note, Sales
  Invoice, Payment Entry, Credit Note, or stock ledger entries.
- A Noon webhook/event-notification receiver was #145's own deferral at the
  time this release shipped; it has since been built under #163 — see
  "Noon event-notification webhook (#163)" above. The hourly poll remains
  in place as a safety net regardless.

## Returns: FBPI return reference lookup and evidence (#153)

Return evidence capture is read-only toward Noon. Toward ERPNext it creates
nothing on its own: the only document it can create is the draft Sales Return
described under "Inspection and draft Sales Return (#261)" below, and only when
a user records a Sellable outcome. No Credit Note, Sales Invoice, Payment Entry,
GL entry or Sales Order change is ever created. Operations
can record a one-time internal receipt acknowledgement on a `Noon Return`;
it records the ERPNext user, time, and optional note only — it is not sent to
or treated as an acknowledgement or resolution on Noon. The lookup records
what Noon's `ListReturnReferences`
(`POST /returns/v1/return-references/list`) returns, as a new standalone
`Noon Return` doctype (not a Marketplace Order child table, so an unlinked
return still persists as a real record).

Two triggers:
- **Manual barcode lookup**: the "Look Up Noon Return" button on a Noon
  Marketplace Account (`marketplace_account.js`) prompts for a barcode and
  calls the whitelisted `api/noon_returns.py::lookup_return()`, which
  enqueues `integrations/noon/returns.py::run_manual_lookup()` on the `long`
  queue (job ID deduplicated on account + barcode) — no Noon HTTP call
  happens in the Desk request itself.
- **Daily AWB scan (experimental)**: a `hooks.py` `scheduler_events["daily"]`
  entry (`integrations.noon.returns.scan_awbs_all_accounts`) scans AWBs from
  existing `Marketplace Order Shipment AWB` child rows and calls
  `ListReturnReferences` per AWB, per enabled Noon account. This is built on
  an **unverified assumption** that a response reference of type `awb_nr`
  means the outbound shipment AWB this app itself generated — see item 2
  below.

Order linking is an exact match of the response's `order_nr` against either
`external_order_id` or `platform_order_reference` on a Marketplace Order
scoped to the same account and `platform == "Noon"` — no fuzzy fallback, and
it additionally requires EXACTLY one match across both fields combined:
zero matches or more than one match both leave the return unlinked rather
than attaching to an arbitrary/best-guess order (review finding #5 — a
`frappe.db.get_value()` lookup would otherwise silently pick a first match
when more than one order satisfies `order_nr`). Dedupe key is
`(marketplace_account, purchase_item_nr)`: a re-lookup updates the existing
row's `last_seen_on`/`references`, never creates a duplicate — this is
retry-based safety, not pure race-freedom: a duplicate-key collision or a
1020/`QueryDeadlockError` from two concurrent lookups is caught, rolled
back, and retried once as an update (review finding #2). Every
`ListReturnReferences` call writes one committed `Marketplace Sync Log` row
(`sync_type = "Return Lookup"`, `trigger_source` = `"Manual"` for a barcode
lookup or `"Scheduled Reconciliation"` for the daily AWB scan).

The daily AWB scan is bounded to AWBs from shipments that are not
`Cancelled`/`Discarded` and were modified within the last 30 days (review
finding #3) — it no longer re-scans every AWB an account has ever had. A
failure on one AWB (a lookup failure or an `upsert_return_item()` failure)
is logged and skipped, not fatal to the rest of that account's scan.

### Returns verification checklist

There is no sandbox access for this issue either — these five items must be
confirmed against a real Noon account before this feature is relied on in
production:

1. **What `order_nr` actually is.** This release assumes it matches either
   `external_order_id` (`fbpi_order_nr`) or `platform_order_reference`
   (`mp_order_nr`) on a Marketplace Order — never confirmed against Noon's
   own docs (the paired `.yml` entry for this endpoint is empty/unconfirmed).
   Also confirm the "exactly one match or unlinked" ambiguity rule above
   against real data — if `order_nr` is meant to be unique per field in
   practice, a real multi-match case may indicate a mapping bug elsewhere
   rather than genuine ambiguity.
2. **What an `awb_nr`-typed reference actually means.** The daily AWB scan's
   entire premise — that it refers to the outbound shipment AWB this app
   recorded — is unverified; this is why that job is flagged experimental.
3. **Whether `merchant_codes` behaves as documented, and whether the
   derived list can go stale or inaccessible.** This release omits the
   field (relying on the connection's `default_project_code` scoping) when
   the account has no imported orders yet to derive codes from —
   unconfirmed whether that fallback actually works as Noon's docs imply.
   Separately, the derived list is capped at 200 (Noon's documented limit)
   by keeping the 200 most-recently-seen `merchant_code` values across this
   account's imported orders — if an account legitimately sells under more
   than 200 distinct merchant codes, or a merchant code an order was placed
   under becomes otherwise inaccessible/stale, older codes silently drop out
   of scope rather than erroring. This has not been confirmed against a real
   account with that many codes.
4. **Error body shape.** Confirm `client.error_message()` produces a usable
   message for this endpoint specifically, same caveat as fulfillment's own
   item 9 above.
5. **Behavior above 200 items.** Confirm how Noon's response behaves (e.g.
   pagination, truncation, or an error) when a lookup's `items[]` would
   otherwise exceed 200 entries — not documented in the checked-in contract.

Do not roll this out to production Noon returns handling before these five
items have been checked against a real Noon account.

### Inspection, draft Sales Return and draft Credit Note (#261, #262)

After a return is acknowledged as received, a user with write permission records
the merchant's decision with **Record Inspection** on the `Noon Return`:

- **Sellable**: creates one **draft** return Delivery Note for one unit (qty -1)
  using ERPNext's own `make_sales_return` against the one submitted, non-return
  Delivery Note row for the mapped Item on the linked Sales Order. The return
  row's warehouse is forced to the Marketplace Account's warehouse, the only one
  the Noon stock sync reads, so staff submitting the draft raises the quantity
  that the hourly sync pushes. No Noon call is made, nothing is auto-submitted,
  and the user needs create permission on Delivery Note.
- **Damaged**: recorded only. Noon is responsible for damaged items, so no
  document is created and no stock moves.

It refuses, creating nothing, when the return is unacknowledged or unlinked, the
Marketplace Order has no submitted Sales Order, the Partner SKU has no enabled
mapping, the account has no warehouse, or there is not exactly one candidate
Delivery Note row (the message names the candidates). A recorded outcome cannot
be changed. Re-running Sellable returns the existing draft or submitted return;
a cancelled or deleted one is replaced. ERPNext cannot make a Sales Return from a
Sales Order, and this app never creates a Delivery Note for a Noon order, so
staff must already have delivered the order with a Delivery Note.

**Create Credit Note** (#262) is offered once an outcome is recorded, for both
Sellable and Damaged. It drafts a return Sales Invoice (qty -1, one unit) with
ERPNext's `make_sales_return` against the one submitted, non-return Sales
Invoice row for the mapped Item on the Sales Order. It refuses, creating
nothing, when there is not exactly one such invoice row, or when that invoice
updates stock (a Credit Note from it would return the stock a second time). It
needs Sales Invoice create permission, creates no Payment Entry, makes no Noon
call, and is never submitted. Re-running returns the existing draft or submitted
Credit Note; a cancelled or deleted one is replaced.

**Needs Attention clearing.** A `Noon Return` linked to an order keeps the
order's `return` evidence (and so the compensating-transaction outcome) only
while it is unresolved. Resolved means a submitted Credit Note AND either a
Damaged outcome or a Sellable outcome with a submitted Sales Return. Drafts,
cancelled documents and returns with no recorded outcome count as unresolved.
The hourly sweep re-plans such orders, so no document events are needed.

## Out of scope (#153)

- Any ERPNext-side correction from a return other than the draft Sales Return of
  #261 and the draft Credit Note of #262 (Payment Entry, refund payment, Sales
  Order change, or any other Sales Invoice adjustment). The local receipt
  acknowledgement is operational evidence only.
- A separate reconciliation report: the evidence surfaces through Needs
  Attention and the planner instead (#164).
- Any write-path Noon call (e.g. acknowledging or resolving a return) —
  `ListReturnReferences` is the only endpoint this issue calls.
