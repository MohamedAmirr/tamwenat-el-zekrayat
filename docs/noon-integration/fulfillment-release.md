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
- A "Noon Fulfillment" button group on the **Sales Order** form (submitted
  Sales Order linked to a Noon Marketplace Order): Create Shipment,
  Cancel Shipment, Add AWB, Mark Out of Stock (UpdateOrder), Upload Shipment
  Invoice (or one order-level invoice), and Retry. The Sales Order also has a
  **Noon Fulfillment tab** (#378, Noon orders only): its Sales Invoice, then one block
  per shipment with status, AWB, Delivery Note (link with draft/submitted state), `n of m
  units shipped` and an items table (item, SKU, qty, Noon unit status, on the Delivery
  Note yes/no). The Marketplace Order keeps its Noon Fulfillment tab as read-only audit
  data (shipment, unit and AWB tables). The buttons call the same endpoints as before, so they still need
  write permission on the Marketplace Order. All of it is backed by three new child doctypes (`Marketplace
  Order Shipment`, `...Unit`, `...AWB`) and a `Fulfillment` sync_type on
  `Marketplace Sync Log`.
- No ERPNext stock or accounting side effects from the Noon calls: no Payment
  Entry, Credit Note, or stock ledger entry is created by a Noon call. The ERPNext
  documents a fulfillment action can create are the shipment's one **Delivery Note**
  (#368, #390): drafted as units become `INTEGRATION_ITEM_STATUS_SHIPPED` and submitted
  when every active unit is SHIPPED, or by Mark Handed Over (see "Mark Handed Over"
  below), and (#377) the Sales Order's one draft **Sales Invoice** built from the submitted
  notes (see "Draft Sales Invoice" below). The app never submits the invoice and never
  uploads it to Noon.

## Setup

1. The Marketplace Account must already have `noon_warehouse_code` and
   `noon_country_code` set (required by #145's order import, reused here).
2. No new account-level configuration is needed for fulfillment itself — the
   same Noon Connection used for order import/pricing/inventory is reused.
3. The Noon Fulfillment buttons only appear on a **submitted Sales Order**
   linked to a Marketplace Order whose `platform` is `Noon` (they used to be on
   the Marketplace Order form; none are there now). They are hidden for a user
   without write permission on Marketplace Order, and Mark Handed Over is also
   hidden without submit permission on Delivery Note (it submits the note). The client code is
   `public/js/noon_fulfillment.js`, loaded with `frappe.require` by
   `public/js/sales_order.js` only for such a Sales Order. The Marketplace Order
   tab on the Sales Order also lists the shipments read-only (number, status,
   AWB, Delivery Note link, units not added).
4. The "Noon Fulfillment" button group shows only the actions that currently
   have something to act on, decided by one read-only call
   (`api.noon_fulfillment.get_available_actions`, read permission) using the
   same eligibility rules as the actions: Create Shipment and Mark Out of
   Stock need a shippable unit; Cancel Shipment and Add AWB need a Created
   shipment; Upload Invoice needs a Created shipment with no invoice upload
   Pending; Retry needs a Pending, Error or Cancel Pending shipment, or a
   Created one with a Pending added AWB or a Pending/Error invoice;
   Mark Handed Over needs a Created shipment with a draft Delivery Note to submit or
   units no live Delivery Note covers yet, and is not offered once the shipment's note is
   submitted;
   Cancel Shipment and Discard Shipment are not offered while the shipment's Delivery Note
   is live; Discard Shipment needs an Error or Cancel Pending shipment; Release Stuck
   Out of Stock Reservation needs a pending reservation. With no applicable
   action the group is absent, and a fully cancelled order shows a "Cancelled
   on Noon; no fulfillment actions available" headline. The same call returns
   the shipments the dialogs and the summary use, so if it fails no button and no
   shipment list is shown (each action still validates server side).

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
2. Create the destination either from the Noon Connection form ("Register
   Webhook in Noon", #253, which creates it with the URL and key below and
   subscribes `FBPI::ORDER_SYNC`) or by hand: in Noon's Partner Center,
   Integrator Configuration, Webhook tab, register a destination pointing at
   the connection's `Webhook URL`
   (`https://<your-site-domain>/api/method/marketplace_integrations.api.noon_webhooks.event_notification`).
3. For a hand-made destination, set the header `X-Api-Key` in that same Webhook
   tab to exactly the connection's `Webhook Secret` (the form button sends it
   for you). Existing connections must change their Webhook tab header from
   `X-Noon-Webhook-Secret` to `X-Api-Key` manually.
4. Subscribe that destination to the `FBPI::ORDER_SYNC` event type. Any
   other event type delivered to this receiver is acknowledged (200) and
   ignored — it does not need to be excluded from the subscription, but
   subscribing only to what's handled avoids pointless deliveries.
5. The connection's `Project Code` field (already required for every Noon
   Connection) is what this receiver uses to resolve an inbound event back
   to the right connection/account — no separate configuration is needed for
   that resolution.

For `FBPI::ORDER_SYNC` this receiver reads only `payload.order_nr`, and only as a lookup
key (see `docs/noon-integration/event-notifications.md`; `RETURNS::REFERENCE_UPDATE` is
covered under Returns below). If a Marketplace Order for (account, `order_nr`) exists, the
event queues the deduplicated per-order GetFbpiOrder refresh
(`orders.enqueue_order_refresh`); otherwise it re-triggers the existing #145 order-import
job for the resolved account. Either way current order state is re-fetched from Noon's
own API rather than trusting anything in the notification body.

**Accepted delay when an import is already running:** a webhook event that
arrives while that account's import job is already queued/running does not
queue a second job (the existing per-account job_id dedup collapses it) -
the receiver still returns 200 and logs success, but that specific order
isn't actually re-imported until the already-running job (or, in the worst
case, the next hourly poll) picks it up, up to about an hour later. See
`docs/noon-integration/event-notifications.md`'s "Accepted dedup gap" for
the full explanation - this is accepted behavior, not a bug. Likewise, the
import only looks back 7 days, so a webhook for an older order changes nothing;
only a manual "Refresh from Noon" picks up that change.

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
- **Readable names.** Noon's order payload has no product title, so item names
  come only from the ERPNext Item behind the product mapping (one lookup per
  distinct SKU); an unmapped SKU is shown as the SKU alone. The Create Shipment
  and Mark Out of Stock checklists show `Item name (SKU)`, with a `(1 of 3)`
  counter when a SKU repeats and the raw `mp_item_nr` only in the tooltip. The
  Noon Shipments table shows `Shipment n` (full number in the tooltip), an Items
  column (`Item name (SKU) x2, ...`) and AWBs as `Noon <number>`; shipment
  dropdowns read `Shipment n - status` while keeping the full number as the
  value. Error messages name units as `Item name (SKU)` and no longer mention
  action codes. Fulfillment Needs Attention rows read `Create shipment failed on
  order <platform order number>: ...` (every action code has a plain verb, an
  unknown one is shown as stored). Marketplace Sync Log `request_data` gets the
  extra keys `order_reference`, `shipment_label` and `items`; the existing keys
  (`action`, `integration_shipment_nr`, notes) are unchanged because Needs
  Attention parses them. Noon-sourced error text is shown unchanged.
- **Noon is the only courier.** Every new shipment gets a Noon AWB (the
  pre-generated pool first, else one `GetNoonLogisticsAWBs` call), unless the user
  types or scans the AWB printed on a Noon sticker into the optional **AWB Nr**
  field of the Create Shipment dialog (see "Pre-printed sticker AWB" below); the
  dialog has no AWB source or courier field, and `create_shipment` refuses any
  source other than Noon. **Add AWB** is Noon's
  `AddShipmentCourierAwbs`, meant for adding a replacement AWB when warehouse
  scanning of the current one fails: the user types the new AWB number and the
  Courier field is a one-option Select (Noon). Noon documents `courier` as a
  free string ("'noon' or your own logistics provider"); the value is always
  stored and sent as lowercase `noon`, and the server refuses any other
  courier. Shipments stored earlier with AWB source Own Courier, and AWB rows
  with other courier text, are left as they are and keep working with Retry,
  Cancel and Discard.
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
  let you change units or invoice file after the first attempt.
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
- A Noon AWB is chosen once per shipment and reused on every retry — it
  is never regenerated or swapped. It is the sticker AWB typed in the dialog when
  there is one, else it comes from the pre-generated pool when one is available,
  otherwise from a single-AWB Noon call (see "Noon AWB pool" below).
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
and, since #390, judges each changed line on its own rather than blocking the whole
order once Delivery Notes exist. An out-of-stock mark after a delivery or shipment
removes or reduces the Sales Order line automatically: this covers a line removed
while another line is already delivered, and a line cut down to its started or
delivered quantity. A cut below the delivered, billed or started quantity ends as
Needs Attention naming the line; every other case the planner cannot act on is left
as a `Modified After Sync` advisory for a person (delivered or billed goods need a
return or credit note).

What surfaces this:
- #178 part 3: the poll now also bumps the Sales Order sync debounce when a
  LINKED order's evidence fingerprint changed (units cancelled/out of stock/
  shipped, shipment membership), and `integrations/noon/order_reconciliation.py`
  turns the per-unit states into reconciliation evidence (full cancellation,
  per-line fulfillment started, the `fulfillment_started_qty` per line and the
  `partial_line_update` flag (#390), unknown stage for `UNSPECIFIED`). A `Noon Return`
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

## Delivery Notes, Mark Handed Over and the item lock (#368, #390)

**One Delivery Note per shipment (#390).** A Noon shipment has exactly one Delivery
Note. `Marketplace Order Shipment Unit.delivery_note` (a read-only Link) records which
units are on it; a unit is **covered** while that note is live (draft, submitted, or the
draft that amends a cancelled one: `handover_state.live_note`). The shipment fields
`delivery_note` and `delivery_notes` are no longer written; they stay read-only for older
data (the backfill patch `backfill_noon_unit_delivery_notes` copies a live note of such a
shipment onto its unit rows that are still empty, except the units listed as unresolved).

**Automatic path.** After every order import, `queue_shipped_units_if_needed` queues
`fulfillment.process_shipped_units` (`enqueue_after_commit`, `job_id`
`marketplace-noon-fulfillment-<order>-shipped`, deduplicated) when a Created shipment has a
SHIPPED unit (`integration_status` in `latest_payload`) that is not covered, or all its
active units are SHIPPED while its note is still a draft. The job runs as Administrator
(it is queued by the import or the webhook, detached from any session), waits until the
Sales Order is submitted, and per Created shipment (own savepoint):

- puts the uncovered SHIPPED units on the shipment's draft through the shared service
  (the first ones create it, later ones are added), and
- submits that draft (`delivery_note_drafts.submit_note`, inside a savepoint) when every
  active unit is SHIPPED. Active means not cancelled (`mp_status` CANCELLED) and not
  `OUT_OF_STOCK`, so a unit that was cancelled or went out of stock before the rest
  shipped is left out. A draft from an earlier Mark Handed Over is the shipment's draft.
- A submit failure (for example not enough stock) keeps the draft, stores the escaped
  error in `Marketplace Order Shipment.delivery_note_error` and shows a Noon Fulfillment
  row in Needs Attention; the next import or Mark Handed Over tries again.
- A SHIPPED unit that cannot go on the note because it is already submitted gets no new
  note; Needs Attention names the unit and the note. Same for a unit that becomes
  resolvable (mapped, on the order) after the submit: while the note is a draft a retry
  adds it, after the submit the unresolved record turns final and the order shows it in
  Needs Action. This replaces #368's "a retry starts a new draft once the first is
  submitted".
- A SHIPPED unit that is not in a Created shipment of the order (no unit row, or the
  shipment is Pending, Error, Cancel Pending, Cancelled or Discarded) gets no Delivery
  Note; Needs Attention names it until it is in a Created shipment and covered, or is no
  longer SHIPPED. These rows are computed live (`handover_state.shipped_problems`); a fixed
  number of queries reads only the orders that have a SHIPPED item row.

**Trade-off.** Stock does not leave the warehouse in ERPNext until the whole shipment is
SHIPPED (or Mark Handed Over is used); until then its units sit on a draft Delivery Note.
Make sure the Sales Order warehouse has stock, or the submit fails and shows in Needs
Attention.

**Mark Handed Over** is the manual "all shipped" override on a `Created` shipment. The
checked-in Noon spec has no handover endpoint, so it makes **no Noon call** and is
synchronous (no queue). It puts **every uncovered unit** of the shipment (SHIPPED or not,
one per `Marketplace Order Shipment Unit`, by `partner_sku`) on the shipment's draft
through the shared service `services/delivery_note_drafts.py`, which maps them through
the Marketplace Product Mapping, caps them at what the Sales Order still has undelivered
(including existing draft Delivery Notes) and builds the draft with ERPNext's own mapper
(a Product Bundle is unpacked by ERPNext), then **submits** the draft. The user needs
write permission on the Marketplace Order and **submit** permission on Delivery Note
(`frappe.has_permission("Delivery Note", "submit", throw=True)` on the server; the button
is hidden without it). The shipment row records `handed_over_on`.

- Idempotent per shipment: with nothing uncovered and no draft, a second click returns
  the existing note and creates nothing. A shipment whose note is already submitted but
  still has an uncovered unit is refused (one note per shipment). A submit failure is
  returned as `submit_error` and stored in `delivery_note_error`.
- A Delivery Note `on_trash` handler (`handover_state.clear_deleted_delivery_note`)
  locks the parent Marketplace Order, clears `delivery_note` on the unit rows that track
  the note (and the legacy shipment fields), resets `handed_over_on` when no unit of the
  shipment is left on a note (so a wrong draft can still be deleted) and bumps the
  order's `modified`. The units are uncovered: the next import drafts the shipped ones
  again, Mark Handed Over drafts them all. Cancelling a note keeps the link (the amend
  flow: a note that `amended_from` the cancelled one counts as live).
- Units that could not be drafted (no enabled mapping; Item not on the Sales
  Order; the platform order has more units of the Item than the Sales Order row
  quantity, an "excess") are stored on the shipment (`skipped_units`) and keep the
  order in Needs Action (`DELIVERY_NOTE_UNITS_SKIPPED`) until they are drafted,
  covered, or the shipment is Cancelled or Discarded; a later save, sync or re-check
  cannot clear it. The other units still get their note. A mapping save and every Sales
  Order sync retry them automatically while the note is a draft (each shipment in its
  own savepoint; a failure is logged; when the Sales Order is not submitted the entries
  become final); a retry never submits. When the Sales Order is
  behind the platform order a normal sync is queued first and the order shows Syncing
  until it has run. Details and the conflict rules:
  `docs/marketplace-integrations/sync-status.md`, Group E.
- **A Delivery Note someone made by hand resolves the record.** A unit already covered
  by a draft or submitted Delivery Note on the matching Sales Order row counts as
  resolved when the Sales Order row quantity is at least the units the platform order
  holds (the Sales Order is not behind), and the retry clears the record and the
  Needs Action. Only the part the Sales Order really lacks stays an excess. Such a unit
  has no `delivery_note` link, so a shipment recorded this way keeps `handed_over_on` and
  the import keeps re-checking its SHIPPED units (harmless: nothing more is drafted).
- **Cancel Shipment and Discard Shipment are refused while a Delivery Note of the
  shipment is live** (draft or submitted, not cancelled or deleted): the message tells
  the user to delete or cancel that Delivery Note first. The Sales Order buttons are
  hidden in that state (`get_available_actions`), the server check is the gate.
- The Sales Order must already exist and be submitted.
- **Reminder.** Needs Attention (`services/marketplace_attention.py`, the Noon
  Fulfillment row) lists a Created shipment with no `handed_over_on` and no live
  Delivery Note older than `NOON_HANDOVER_REMINDER_HOURS` (36, chosen by the user,
  measured from the shipment row's creation). It clears when `handed_over_on` is set
  or the shipment is Cancelled or Discarded. A Delivery Note created automatically when
  the units became SHIPPED counts as handed over, as a draft or submitted.

**Item lock.** A submitted Sales Order linked to a Noon order refuses changes to
item, quantity, rate and row add or remove (`services/noon_item_edit_guard.py`, a check on the Sales Order Item rows (`before_update_after_submit`, `before_insert`, `on_trash`), because `update_child_qty_rate` changes the rows before it saves the parent),
because Noon has no order update and the two would diverge. The message points to
the Noon Fulfillment buttons on the Sales Order, and the Sales Order form shows the same hint. The sync
and the reconciliation planner still update the order (so Mark Out of Stock and
Cancel Shipment reach the Sales Order through the planner), and non-item fields
stay editable. Shopify and Talabat orders are not affected.

Unverified against a real Noon account: the handover signal itself (verification
item 2), and which statuses Noon reports after CreateShipment (item 6 of the
user's checklist for this issue). The automatic path of #390 relies on
`INTEGRATION_ITEM_STATUS_SHIPPED` meaning the unit left the warehouse; the user chose to
rely on it before it is verified. Mark Handed Over does not depend on it: it is a manual
override.

**What the checked-in Noon contract says about shipment and order state (checked 2026-10-28).**
State reaches us only by fetching: `FBPI::ORDER_SYNC` triggers the import (or, since #390,
the refresh of the one order its `order_nr` names; the receiver reads nothing else from the
payload), and `ListFbpiOrders` / `GetFbpiOrder`
return, per unit (`items[]`): `mp_item_nr`, `partner_sku`, `mp_status`
(`MP_ITEM_STATUS_UNSPECIFIED`, `_CONFIRMED`, `_CANCELLED`), `integration_status`
(`INTEGRATION_ITEM_STATUS_UNSPECIFIED`, `_ACKNOWLEDGED`, `_OUT_OF_STOCK`, `_SHIPPED`),
`delivered_invoice_price` and `cancellation_reason_code`; the order carries
`fbpi_order_nr`, `mp_code`, `mp_order_nr`, country and currency codes, `merchant_code`,
`warehouse_code` and `order_created_at`. `GetShipment` returns `awbs[]` and `items[]` with
no status field. There is no DELIVERED, handover or pickup value anywhere in the spec.
`INTEGRATION_ITEM_STATUS_SHIPPED` is documented as an enum value but not documented as
what CreateShipment or a handover produces (verification items 2 and 6): whether it
appears after CreateShipment, after a physical handover or later is unverified. So no
documented field is known to say "handed over to Noon", and none is relied on.

## Cancelling the Sales Order: wait for Noon (#376)

Design note: `docs/noon-integration/sales-order-cancel.md`. A Noon Sales Order cannot be cancelled
while a shipment is Pending, Created, Cancel Pending or Error (Cancelled and Discarded never block):
`services/noon_cancel_guard.py`, `before_cancel` on Sales Order, rules in
`fulfillment.sales_order_cancel_blockers`. A handed-over shipment (`handed_over_on` or a live Delivery
Note) gets its own message (cancel on Noon, then the Delivery Note). The button **Cancel on Noon**
(action `cancel_on_noon`, `api/noon_fulfillment.py::get_cancel_on_noon_preview` and
`cancel_created_shipments`) asks for confirmation and queues the existing `cancel_shipment` for every
Created shipment; the user cancels the Sales Order once the shipments show Cancelled. Units outside a
shipment are not touched and nothing is marked out of stock. Close is not guarded. The planner's
cancel (customer cancelled on Noon) passes through `frappe.flags.marketplace_syncing_orders`.

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
10. **CreateShipment accepts an AWB not returned by GetNoonLogisticsAWBs
    (pre-printed sticker).** The sticker AWB path (#385) sends an AWB the app
    never fetched; confirm Noon accepts it and attaches it to the shipment.

Do not roll this out to production Noon fulfillment before these ten items
have been checked against a real Noon account.

## Noon AWB pool (bulk AWB pre-generation)

- **Generate:** on a Noon Marketplace Account, **Generate Noon AWBs** asks for a
  quantity (1-1000) and queues `api/noon_awbs.py::generate_awbs`, which runs
  `integrations/noon/awb_pool.py::run_generate` on the `long` queue (deduplicated
  per account, no Noon HTTP call in the Desk request). The worker calls
  `GetNoonLogisticsAWBs` once (`country_code` from the account, `qty`) and stores
  every returned AWB as an `Available` `Noon AWB` row. The result (stored count,
  or the failure) reaches the requesting user as a realtime message; failures are
  also written to the Error Log. The form shows **Available Noon AWBs: N** as an
  intro line. Needs write on the account and create on `Noon AWB`.
- **`Noon AWB` doctype** (System Manager only): Marketplace Account, AWB Nr,
  country code, Status (`Available`/`Used`), generated on, and, once used, the
  Marketplace Order, integration shipment nr and used on. AWB Nr is unique per
  account through a hidden hashed `awb_identity_key` (composite-uniqueness
  pattern); an AWB Noon returns twice is skipped, not duplicated.
- **Claim:** when a Noon-source shipment has no AWB yet,
  `run_create_shipment()` releases the order lock and calls
  `claim_pool_awb()`, one `UPDATE ... WHERE status = 'Available' ... LIMIT 1`
  (oldest first, same account and country) that records the order and shipment on
  the row, so two workers can never take the same AWB. A repeat claim for the
  same shipment returns the row already claimed. The claimed AWB then goes
  through the unchanged path (appended as the shipment's AWB row, reused on
  retry). An empty pool falls back to the previous qty-1 `GetNoonLogisticsAWBs`
  call. Lock handling around Noon HTTP calls is unchanged.
- **A typed sticker AWB bypasses the claim.** When the AWB is already on the
  shipment's AWB row, `run_create_shipment()` skips both the claim and
  `GetNoonLogisticsAWBs` (the `not awb_rows` guard), and a retry resends the same AWB.
  See "Pre-printed sticker AWB".
- **A claimed AWB never returns to the pool.** A discarded, cancelled or failed
  shipment keeps consuming the AWB it claimed; Discard Shipment does not release
  it. A worker that dies between the claim and writing the shipment's AWB row
  leaves that AWB `Used` with the order/shipment recorded; the retry reuses it.
- Unverified (no live Noon call was made): whether Noon rate-limits or caps large
  quantities, and whether pre-generated AWBs expire.

## Pre-printed sticker AWB (#385)

The Create Shipment dialog has one optional **AWB Nr** field (type or scan the AWB on
the Noon sticker; empty means a Noon AWB as before). The client sends `awb_nr` only
when filled; spaces-only counts as empty. `create_shipment()` then, under the order lock:

- rejects more than 140 characters (the column length);
- rejects an AWB already on any shipment AWB row of an order on the same Marketplace
  Account, whatever the shipment status; the message does not name the other order;
- inserts a `Noon AWB` row for it (status `Used`, order and shipment set), so the
  unique `awb_identity_key` rejects a pool match (Available or Used) and a
  simultaneous request on another order at database level; a duplicate-key error
  becomes the same translated rejection;
- writes one `noon_shipment_awbs` row in the same save (courier `noon`, source Noon,
  status Pending).

The worker is unchanged: the existing AWB row means no pool claim and no
`GetNoonLogisticsAWBs`, and Retry resends the same AWB (decision 14). Whether Noon accepts
an AWB it did not return is verification item 10.

## Draft Sales Invoice (#377)

`services/noon_sales_invoice.py` keeps ONE draft Sales Invoice per Noon Sales Order,
stored in the read-only `Marketplace Order.noon_sales_invoice`. `fulfillment._submit_shipment_draft`
queues `sync_sales_invoice` after a shipment's Delivery Note is **submitted** (never for a
draft note; Mark Handed Over and the SHIPPED worker both go through it), `enqueue_after_commit`,
`job_id` per Sales Order, deduplicated, running as Administrator. The job locks the
Marketplace Order row, then the Sales Order row, and then:

- no invoice: ERPNext's `make_sales_invoice` mapper builds a draft from the first submitted,
  not yet invoiced Delivery Note (`update_stock` 0, so a credit note, #262, works);
- invoice still a draft: the rows of every other submitted note that has no row on a live
  invoice and is not fully billed are appended (the mapper caps each row at what the note still
  has unbilled, so nothing is billed twice);
- invoice submitted or cancelled: nothing is added; Needs Attention (Noon Fulfillment) names the
  submitted notes not yet invoiced until they are billed by hand;
- failure: the escaped error is stored in `noon_sales_invoice_error`, shown in Needs
  Attention, and retried by the next note submit or the next order import.

A draft invoice makes a marketplace-driven Sales Order cancel wait
(`hold_cancel_for_draft_documents`, `DRAFT_DOCUMENTS_BLOCK_CANCEL`) until the invoice is
submitted, cancelled or deleted. Nothing is sent to Noon: the manual Upload Shipment Invoice
and Upload Order Invoice below are unchanged (a user-picked PDF).

## Order-level invoice (UploadFbpiOrderInvoice)

An alternative to per-shipment invoices for merchants who issue one invoice
for the whole order (`POST /fbpi/v1/fbpi-order/invoice/upload`). Button
"Upload Order Invoice" in the Noon Fulfillment group on the Sales Order, same dialog as the
shipment invoice (private PDF attachment, Invoice Nr, Issued At).

- State lives on the Marketplace Order (Noon Fulfillment tab, "Order
  Invoice" section): `noon_order_invoice_file`, `_nr`, `_issued_at`,
  `_status` (Not Uploaded/Pending/Uploaded/Error), `_last_error`, and a
  hidden `_idempotency_key`.
- It runs exactly like the shipment invoice: order lock, private/`%PDF-`/5 MiB
  checks, a fresh idempotency key per explicit upload, Pending committed
  before the enqueue, the worker releases the lock around the Noon call and
  ignores a stale key, errors go to the dedicated last-error field, and each
  call writes a `Fulfillment` Sync Log row (`UploadOrderInvoice`, never the
  base64). A "Retry Order Invoice" button re-enqueues a Pending/Error upload
  with its saved key. Re-uploading over an Uploaded order invoice is allowed
  (Noon supersedes the previous one); a second upload while Pending is not.
- **Precondition:** the order must have at least one shipment with status
  `Created` (mirrors the shipment-invoice gating). Noon's endpoint documents
  no shipment requirement; this is a deliberate local choice, and the worker
  re-checks it before calling Noon.
- **No mixing:** the order invoice is refused while any shipment invoice is
  Pending or Uploaded, and a shipment invoice is refused while the order
  invoice is Pending or Uploaded. An `Error` invoice on either side does not
  block the other kind.
- Unverified against a real account: the endpoint's documented success
  and error bodies are only "a successful response" and "an unexpected error
  response", so the failure message shown is whatever
  `client.error_message()` extracts; idempotency/supersede behavior is the
  same open item as checklist item 8.

## Out of scope (#146)

- `CreateSandboxOrder` tooling. (Bulk AWB pre-generation and the order-level
  `UploadFbpiOrderInvoice` were added later; see "Noon AWB pool" and
  "Order-level invoice".)
- Automatic Sales Order amendment after delivery (cancellation of an
  undelivered, unbilled order is done by the reconciliation planner, #179), Sales Invoice (#377), Payment Entry, Credit Note, or stock ledger entries. (A draft Sales
  Return and draft Credit Note are created later only by the user actions in
  the returns section, #261/#262.)
- A Noon webhook/event-notification receiver was #145's own deferral at the
  time this release shipped; it has since been built under #163 — see
  "Noon event-notification webhook (#163)" above. The hourly poll remains
  in place as a safety net regardless.

## Returns: FBPI return reference lookup and evidence (#153)

Return evidence capture is read-only toward Noon. Toward ERPNext it creates
nothing on its own: the only documents it can create are the draft Sales Return
and draft Credit Note described under "Inspection, draft Sales Return and draft
Credit Note (#261, #262)" below, each only on a user action and never
submitted. No Payment Entry, GL entry or Sales Order change is ever created. Operations
can record a one-time internal receipt acknowledgement on a `Noon Return`;
it records the ERPNext user, time, and optional note only — it is not sent to
or treated as an acknowledgement or resolution on Noon. The lookup records
what Noon's `ListReturnReferences`
(`POST /returns/v1/return-references/list`) returns, as a new standalone
`Noon Return` doctype (not a Marketplace Order child table, so an unlinked
return still persists as a real record).

Three triggers:
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
- **`RETURNS::REFERENCE_UPDATE` webhook (#263)**: queues the same lookup for
  the event's `barcode` (see "Return reference webhook (#263)" below).

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

**Decision (2026-10-15):** the user chose to accept the risk of using the return
actions in production before these five items are verified.

### Return reference webhook (#263)

Besides the manual lookup and the daily AWB scan, a `RETURNS::REFERENCE_UPDATE` event queues the same lookup (`found_via` "Webhook"). Its payload fields are unverified; see `docs/noon-integration/event-notifications.md`. It only creates or updates `Noon Return` rows.

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
