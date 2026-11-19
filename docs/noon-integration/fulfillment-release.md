# Noon FBPI fulfillment release notes (#146)

> **Superseded in part by #415 (phase 4).** The Marketplace Order shipment flow described below (Create / Cancel / Add AWB / Discard / Retry, the AWB pool, Mark Handed Over, the `Marketplace Order Shipment`, `...Unit` and `...AWB` tables, `custom_carrier_status` / `custom_carrier_error`) was removed; the Delivery Note is the shipment (`docs/noon-integration/README.md`, `business-spec.md`). Still current: Sales Order eligibility and sync, Mark Out of Stock, the order-level invoice (it now needs a Delivery Note with a Created Noon shipment), the returns lookup, and the real-world verification lists, which now live in the #415 issue ("Verify on the first real shipment"). Read the rest as history. **Also superseded by #416:** the Mark Out of Stock unit checklist dialog (and its `get_shippable_units` / `mark_out_of_stock` endpoints and the "Mark Out of Stock" Sync Log rows) is replaced by the "Mark as Out of Stock" button on a Sales Order item row, logged as Integration Requests; see `docs/noon-integration/README.md`.

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
  Invoice (or one order-level invoice), and Retry. The Marketplace Order keeps
  the Noon Fulfillment tab as read-only audit data (shipment, unit and AWB
  tables). The buttons call the same endpoints as before, so they still need
  write permission on the Marketplace Order. All of it is backed by three new child doctypes (`Marketplace
  Order Shipment`, `...Unit`, `...AWB`) and a `Fulfillment` sync_type on
  `Marketplace Sync Log`.
- No ERPNext stock or accounting side effects from the Noon calls: no Sales
  Invoice, Payment Entry, Credit Note, or stock ledger entry is ever created
  by any fulfillment action. The one ERPNext document a fulfillment action can
  create is the **draft** Delivery Note of Mark Handed Over (#368, see "Mark
  Handed Over" below); it is never submitted by the app.

## Setup

1. The Marketplace Account must already have `noon_warehouse_code` and
   `noon_country_code` set (required by #145's order import, reused here).
2. No new account-level configuration is needed for fulfillment itself — the
   same Noon Connection used for order import/pricing/inventory is reused.
3. The Noon Fulfillment buttons only appear on a **submitted Sales Order**
   linked to a Marketplace Order whose `platform` is `Noon` (they used to be on
   the Marketplace Order form; none are there now). They are hidden for a user
   without write permission on Marketplace Order, and Mark Handed Over is also
   hidden without create permission on Delivery Note. The client code is
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
   Mark Handed Over needs a Created shipment that is not handed over yet (no live
   Delivery Note, no recorded handover) or that still has unresolved handover units;
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
same `run_order_import` job #145's scheduled poll already enqueues — **the
25-minute poll stays on as the safety net**; nothing about it changes or is
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

For `FBPI::ORDER_SYNC` this receiver never parses `payload` (see
`docs/noon-integration/event-notifications.md`; `RETURNS::REFERENCE_UPDATE` is
covered under Returns below) — a handled event only re-triggers the
existing #145 order-import job for the resolved account, which re-fetches
current order state from Noon's own API rather than trusting anything in
the notification body.

**Accepted delay when an import is already running:** a webhook event that
arrives while that account's import job is already queued/running does not
queue a second job (the existing per-account job_id dedup collapses it) -
the receiver still returns 200 and logs success, but that specific order
isn't actually re-imported until the already-running job (or, in the worst
case, the next 25-minute poll) picks it up, up to 26 minutes later (the longest gap between polls) (or the 23:59 catch-up for an order older than 2 hours). See
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
- The scheduled Noon order import bumps a changed linked order, and the
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

## Delivery Note-first shipment (#391)

Noon shipments are created from a draft Delivery Note (`docs/marketplace-integrations/delivery-note-first.md`, epic #392). The old **Create Shipment** and **Mark Handed Over** buttons are hidden on the Sales Order and their endpoints refuse with a message pointing to Create > Delivery Note. Mark Out of Stock, Add AWB, invoices, Discard and Cancel Shipment are unchanged. The Sales Order **Retry** of a Pending/Error shipment that has a draft Delivery Note is routed through that note (same job id and write-back, so the note and the shipment cannot diverge and only one job runs); Retry of shipments without a note is unchanged.

**Flow.** On a submitted Noon Sales Order use **Create > Delivery Note** and save. Saving queues one job per note (`long` queue, deduplicated job id, `enqueue_after_commit`); nothing is sent to Noon in the request. The worker (`integrations/noon/delivery_note_shipment.py`, `run`) takes the units from the note rows: each Item maps (Marketplace Product Mapping) to one Noon `partner_sku` found on the order, the row quantity in the sales UOM is the number of units, and it takes that many currently shippable `mp_item_nr`s of the SKU (`fulfillment.get_shippable_units`). It writes one Pending shipment (+ units, + a typed AWB row) with `delivery_note` set and calls `run_create_shipment` unchanged: fresh GetFbpiOrder check, GetShipment first, AWB from `lr_no` if typed (sticker, same checks as #385: refused when used on another order of the account), else the AWB pool, else GetNoonLogisticsAWBs. No row lock is held across a Noon call.

**Result on the Delivery Note.** Success: `transporter` = the Supplier whose `supplier_name` is "Noon" (created by the patch `create_noon_transporter_supplier`, found by name because the docname depends on Buying Settings; an existing one that is not flagged Is Transporter is reported by the patch, not changed), `lr_no` = AWB (form labels: `transporter` "Carrier", `lr_no` "AWB / Tracking No"; Property Setters, moved under the customer on the Details tab), `lr_date` = today, `custom_carrier_status` = Created. Failure: `custom_carrier_status` = Error and the HTML-escaped message in `custom_carrier_error`; the draft stays. The form shows the status, AWB and error, and **Retry Noon Shipment** on Error (write permission on that Delivery Note; a Stock User has it natively; the worker writes the Noon records as the system user). Retry re-queues the same job and reuses the note's existing shipment (same shipment number, GetShipment first), so nothing is created twice. A note stuck in Pending (its job was lost, for example a failed deferred enqueue) shows Retry too once it has not been modified for 15 minutes (`STALE_PENDING_MINUTES`; the server refuses earlier). The worker locks the Delivery Note row, then the Marketplace Order row, and commits before any Noon call; a person's save of the note either finishes first or then sees the shipment row and is refused. If recording an error itself fails, the note still ends in Error with the message.

**Refused before any Noon call (Error):** fewer shippable units than the quantity, an Item without an enabled mapping to a SKU on the order, an Item mapped to more than one Noon SKU on the order, a non-whole quantity, a typed AWB already in use. These write no shipment; the Sync Log row ("Fulfillment Error" in Needs Attention) names the Delivery Note and closes when the note is fixed, deleted or created.

**Guards (Delivery Note `doc_events`, only for notes whose rows all come from one submitted Noon Sales Order; returns and every other note are untouched).** `validate`: once a shipment row linked to the note is Pending, Created or Error, `transporter`, `lr_no` (the AWB) and the item rows (item, quantity, add, remove) cannot change; other fields can. A sticker AWB typed into `lr_no` before any shipment row exists is still accepted, and the worker's own write-back is not affected. A shipment row belongs to the note if it is the row's first note or in its `delivery_notes` list (an old-flow shipment can hold several). `before_submit`: refused until the shipment is Created, except for a Noon note from before this flow (no carrier status and no shipment row), which submits as it always did. The app never submits. Needs Attention: the Noon Fulfillment row names the Delivery Note of a shipment in Error.

**The Delivery Note carries the other shipment calls (#393).**
- *Cancel and delete.* A Delivery Note created through this flow cannot be cancelled or deleted while its Noon shipment is live (`Pending` with an AWB row, `Created`, `Cancel Pending`, `Error` with an AWB row). The guard is server side (`before_cancel` and `on_trash`, `delivery_note_shipment.guard_cancel_or_delete`), so the API and list bulk delete cannot bypass it; its message points to the form. A shipment that never reached Noon (`Pending` or `Error` with **no AWB row**, because the AWB row is written before CreateShipment) is set to `Discarded` locally with no Noon call and the note goes on. An `Error` shipment that has an AWB row may exist on Noon (a transport failure after CreateShipment), so it is refused with "use Retry Noon Shipment first, or Discard Noon Shipment"; once Retry makes it Created it can be cancelled. **Discard Noon Shipment** (button on the draft note, only while its shipment is in Error; `discard_delivery_note_shipment`, document-level `write`, short confirmation, no Noon call) sets the shipment `Discarded` (units released), clears `lr_no`/`lr_date` and leaves the note in Error so Retry Noon Shipment can create a new shipment or the note can be deleted; a Created shipment cannot be discarded this way. If Noon had created the shipment anyway it keeps existing there. The refusal message for cancel and delete names the exact button, and the form shows an intro line while the shipment is Created saying the note cannot be cancelled or deleted until the Noon shipment is cancelled and how. Lock order is Delivery Note, then Marketplace Order, everywhere (including `handover_state.clear_deleted_delivery_note`, which runs after the guard on `on_trash`).
- *Form.* Cancelling a submitted note opens a dialog ("Cancelling the Noon shipment...") from the form's `before_cancel` event (after Frappe's own Cancel confirmation; `frappe.validated = false` stops the cancel). `whitelisted cancel_delivery_note_shipment` (document-level `cancel` for a submitted note, `delete` for a draft) sets the shipment `Cancel Pending` and queues `delivery_note_shipment.run_cancel`, which runs the existing `run_cancel_shipment` worker (same job id and deduplication as the Sales Order cancel) and, for a draft note, then marks the note Error "cancelled, use Retry Noon Shipment" and clears `lr_no` (the cancelled AWB must not be sent again as a sticker). The dialog polls `get_delivery_note_cancel_state` and lets the cancel continue only when nothing live is left. A definite Noon refusal (a 4xx other than 408/429) puts a note-created shipment back to `Created` with Noon's error (`run_cancel_shipment`; shipments without a note keep the old behaviour), so the note keeps working (submit, Add AWB, invoice); a 5xx, timeout or 429 leaves it `Cancel Pending` with the error. The dialog shows the message (escaped), the note is unchanged, and cancelling again re-sends. `Cancel Pending` is in the lock set: transporter, `lr_no`, the carrier status and the item rows cannot be edited, the note cannot be submitted, and a create job refuses to run. **Known limit:** closing the dialog or reloading the page does not stop a Noon cancel that has started, and the note itself is then still not cancelled (the dialog warns; open the note and cancel it again). The note's `custom_carrier_status` has a separate `Cancelled` value for a cancelled or discarded shipment (neutral banner; Retry Noon Shipment creates a new shipment; Needs Attention opens only `Error` notes). Which shipments belong to this flow is decided from the shipment rows (a row with `handed_over_on` is an old Mark Handed Over shipment and keeps its Sales Order actions and no guard), never from the editable status, which is locked once a shipment exists. **Delete has no form event** in Frappe 16 (`Form.savetrash` calls `frappe.model.delete_doc` with no hook), so a draft note gets a **Cancel Noon Shipment** button (same dialog); the delete itself is then done natively, and the server guard refuses it before that.
- *Sales Order.* `Sales Order before_cancel` is refused while a draft Delivery Note of the order has a live Noon shipment (ERPNext only blocks on submitted links).
- *Amended notes.* An amended note (`amended_from`) gets a new shipment only if the shipment of the note it amends is Cancelled or Discarded; its copied `lr_no` is cleared when it equals the old note's.
- *Add AWB, shipment invoice, Retry on the note.* Buttons in the Noon group on the Delivery Note of a Created shipment, through `add_delivery_note_awb`, `upload_delivery_note_invoice`, `retry_delivery_note_awb_or_invoice` (document-level `write` on the note, order writes run as the system), calling the existing `add_awb`, `upload_invoice`, `retry` flows and workers. A replacement AWB does not change `lr_no` (the lock keeps the AWB the shipment was created with). The Sales Order no longer offers Add AWB, Upload Invoice or their Retry for a shipment that has a Delivery Note (the endpoints refuse too); it keeps Mark Out of Stock, Release Stuck Reservation, the order-level invoice, and the old actions for shipments with no note.
- *Kept rule.* `fulfillment.cancel_shipment` and `discard_shipment` still refuse from the Sales Order while a Delivery Note is live; the note's own entry point passes `delivery_note=` to bypass it. This keeps old-flow shipments as before and stops a Sales Order cancel from leaving a live note without a shipment.

**Not in this issue.** Removing the old handover code (#394) and the migration of shipments created under the old flow. Until then a Noon order whose units are already in an old-flow shipment cannot create a new one from a Delivery Note (the units are claimed: Error "only N are available").

## Mark Handed Over and the item lock (#368)

**Mark Handed Over** is a manual action on a `Created` shipment. The checked-in
Noon spec has no handover endpoint, so it makes **no Noon call** and is
synchronous (no queue). It sends the shipment's units (one per `Marketplace
Order Shipment Unit`, by `partner_sku`) to the shared service
`services/delivery_note_drafts.py`, which maps them through the Marketplace
Product Mapping, caps them at what the Sales Order still has undelivered
(including existing draft Delivery Notes) and creates a draft Delivery Note
through ERPNext's own mapper (a Product Bundle is unpacked by ERPNext). A person
reviews and submits it; the app never does. The user needs write permission on
the Marketplace Order and create permission on Delivery Note (both checked server
side). The shipment row records `handed_over_on` and `delivery_note` (read-only).

- Idempotent per shipment: once the shipment has a live Delivery Note (or was
  recorded as handed over) and nothing is unresolved, a second click returns it and
  creates nothing. `delivery_note` is a Link (the first note) and `delivery_notes`
  lists every note created for the shipment. A Delivery Note `on_trash` handler
  (`handover_state.clear_deleted_delivery_note`) locks the parent Marketplace Order,
  clears the link, the list and `handed_over_on` first (so a wrong draft can still be
  deleted) and bumps the order's `modified`; the next click drafts the whole shipment
  again, and units another live note still covers count as resolved. Cancelling a note
  keeps the link (the amend flow: a note that `amended_from` the cancelled one counts as
  the shipment's live note).
- Units that could not be drafted (no enabled mapping; Item not on the Sales
  Order; the platform order has more units of the Item than the Sales Order row
  quantity, an "excess") are stored on the shipment (`skipped_units`) and keep the
  order in Needs Action (`DELIVERY_NOTE_UNITS_SKIPPED`) until they are drafted,
  covered, or the shipment is Cancelled or Discarded; a later save, sync or re-check
  cannot clear it. The other units still get their draft. A click while a record
  exists retries just those units. A mapping save and every Sales Order sync retry
  them automatically (each shipment in its own savepoint; a failure is logged; when
  the Sales Order is not submitted the entries become final). When the Sales Order is
  behind the platform order a normal sync is queued first and the order shows Syncing
  until it has run. Details and the conflict rules:
  `docs/marketplace-integrations/sync-status.md`, Group E.
- **A Delivery Note someone made by hand resolves the record.** A unit already covered
  by a draft or submitted Delivery Note on the matching Sales Order row counts as
  resolved when the Sales Order row quantity is at least the units the platform order
  holds (the Sales Order is not behind), and the retry clears the record and the
  Needs Action. Only the part the Sales Order really lacks stays an excess.
- **A retry extends the first note.** Leftover units are added to the shipment's first
  Delivery Note while it is still a draft (rows of the same Sales Order row get their
  quantity raised). Only when the first note is submitted does the retry create a new
  draft, and the shipment then tracks every note it created.
- **Cancel Shipment and Discard Shipment are refused while a Delivery Note of the
  shipment is live** (draft or submitted, not cancelled or deleted): the message tells
  the user to delete or cancel that Delivery Note first. The Sales Order buttons are
  hidden in that state (`get_available_actions`), the server check is the gate.
- The Sales Order must already exist and be submitted.
- **Reminder.** Needs Attention (`services/marketplace_attention.py`, the Noon
  Fulfillment row) lists a Created shipment with no `handed_over_on` and no live
  Delivery Note older than `NOON_HANDOVER_REMINDER_HOURS` (36, chosen by the user,
  measured from the shipment row's creation). It clears when `handed_over_on` is set
  or the shipment is Cancelled or Discarded. Nothing is detected automatically.

**Item lock.** A submitted Sales Order linked to a Noon order refuses changes to
item, quantity, rate and row add or remove (`services/noon_item_edit_guard.py`, a check on the Sales Order Item rows (`before_update_after_submit`, `before_insert`, `on_trash`), because `update_child_qty_rate` changes the rows before it saves the parent),
because Noon has no order update and the two would diverge. The message points to
the Noon Fulfillment buttons on the Sales Order or to cancel and amend the Delivery Note; the Sales Order form shows the same hint. The sync
and the reconciliation planner still update the order (so Mark Out of Stock and
Cancel Shipment reach the Sales Order through the planner), and non-item fields
stay editable. Shopify and Talabat orders are not affected.

Unverified against a real Noon account: the handover signal itself (verification
item 2), and which statuses Noon reports after CreateShipment (item 6 of the
user's checklist for this issue). Mark Handed Over does not depend on either: it is a
manual record.

**What the checked-in Noon contract says about shipment and order state (checked 2026-10-28).**
State reaches us only by fetching: `FBPI::ORDER_SYNC` triggers the import and the receiver
never parses the payload (it carries just `order_nr`), and `ListFbpiOrders` / `GetFbpiOrder`
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
  undelivered, unbilled order is done by the reconciliation planner, #179), automatic
  Delivery Notes (only the manual draft of Mark Handed Over, #368), Sales
  Invoice, Payment Entry, Credit Note, or stock ledger entries. (A draft Sales
  Return and draft Credit Note are created later only by the user actions in
  the returns section, #261/#262.)
- A Noon webhook/event-notification receiver was #145's own deferral at the
  time this release shipped; it has since been built under #163 — see
  "Noon event-notification webhook (#163)" above. The 25-minute poll remains
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
- **Manual barcode lookup (New Return, #550)**: the **New Return** button on
  the Noon Return list (`noon_return_list.js`; it replaces the standard "Add
  Noon Return" action, and the old "Look Up Noon Return" button on the
  Marketplace Account form is gone, so there is one entry point) opens a
  dialog: Marketplace Account (shown only when the user can read more than
  one enabled Noon account), an optional Merchant Code (blank by default,
  from `list_merchant_codes`), and a Barcode. Look Up (or Enter in the
  barcode box) calls the whitelisted `api/noon_returns.py::lookup_return()`,
  which enqueues `integrations/noon/returns.py::run_manual_lookup()` on the
  `long` queue (job ID deduplicated on account + barcode + merchant code) — no
  Noon HTTP call happens in the Desk request itself — and the dialog polls
  `get_lookup_result` every 2 s for up to 60 s (states: looking up, no items,
  error with Retry, timeout "check the list shortly"). Each found item is a
  row (Purchase Item Nr, Partner SKU, Order Nr, linked Marketplace Order)
  with a choice Restock (stored `Sellable`) / Damaged / Damaged, Noon paid,
  blank by default (blank = skip). A row that already has an outcome shows it
  read-only; an unlinked row shows "Not linked to a Marketplace Order" and
  has no choice. One Confirm (disabled until a row has a choice) calls
  `apply_outcomes`, which also acknowledges receipt, and then shows each
  row's result with links to the Noon Return and the draft Sales Return,
  refusals included. The Credit Note is still created from each Noon Return
  form. The server text (`error`, row `message`) is pre-escaped and shown
  as is; everything else is escaped in the client. The Noon Return form's
  Record Inspection dialog offers the same three choices, and Create Sales
  Return reappears (after a cancelled draft) for every outcome.
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
- **Damaged** and **Damaged, Noon paid** (#549; the unit comes back to us but is
  not restocked): each drafts one return Delivery Note exactly like Sellable
  (the UI label of the stored value `Sellable` is "Restock"), but the row's
  warehouse is the Marketplace Account's **Damaged Warehouse**
  (`noon_damaged_warehouse`), never the Noon warehouse, so the unit stays out of
  sellable stock and can be written off later with a Stock Entry. If the damaged
  warehouse is not set the outcome is refused with a message (nothing is
  assumed). The two differ only in what staff recorded; the Credit Note stays one
  unit at the item's own price, and no Payment Entry is made for Noon's refund.

It refuses, creating nothing, when the return is unacknowledged or unlinked, the
Marketplace Order has no submitted Sales Order, the Partner SKU has no enabled
mapping, the account has no warehouse (Sellable) or no damaged warehouse
(Damaged outcomes), or there is not exactly one candidate
Delivery Note row (the message names the candidates). A recorded outcome cannot
be changed. Re-running Sellable returns the existing draft or submitted return;
a cancelled or deleted one is replaced. ERPNext cannot make a Sales Return from a
Sales Order, and this app never creates a Delivery Note for a Noon order, so
staff must already have delivered the order with a Delivery Note.

**Create Credit Note** (#262) is offered once an outcome is recorded, for both
every outcome. It drafts a return Sales Invoice (qty -1, one unit) with
ERPNext's `make_sales_return` against the one submitted, non-return Sales
Invoice row for the mapped Item on the Sales Order. It refuses, creating
nothing, when there is not exactly one such invoice row, or when that invoice
updates stock (a Credit Note from it would return the stock a second time). It
needs Sales Invoice create permission, creates no Payment Entry, makes no Noon
call, and is never submitted. Re-running returns the existing draft or submitted
Credit Note; a cancelled or deleted one is replaced.

**Needs Attention clearing.** A `Noon Return` linked to an order keeps the
order's `return` evidence (and so the compensating-transaction outcome) only
while it is unresolved. Resolved means a submitted Credit Note AND a
recorded outcome (Sellable, Damaged or Damaged, Noon paid) whose Sales Return is
submitted too, so a stock movement cannot stay in draft. Drafts, cancelled
documents and returns with no recorded outcome count as unresolved.
The hourly sweep re-plans such orders, so no document events are needed.

### Lookup by merchant code and batch confirm (#549, epic #551)

- **Merchant code scoping.** `returns.queue_manual_lookup(account, barcode,
  merchant_code=None)`: a blank code sends `{"barcode": ...}` only (Noon then
  scopes to the connection's `default_project_code`); a set code sends
  `merchant_codes: [code]` and is refused before anything is queued unless it is in
  `_account_merchant_codes(account)`. The job id includes the merchant code, so the
  same barcode with another code is its own job. The webhook and the daily AWB scan
  keep the derived behavior.
- **Result polling.** `lookup_return` returns `{queued, lookup_key}`. The worker
  stores `{state: queued|done|error, names, error}` in `frappe.cache` under that
  key for one hour (still writing the Marketplace Sync Log row); `get_lookup_result`
  returns the state plus the saved Noon Returns read with `frappe.get_list`. Zero
  items is `done` with no rows. `list_merchant_codes` lists the codes to choose from.
  All three need Marketplace Account write and a Noon-bound account.
- **Batch confirm.** `returns.apply_outcomes(rows)` (`api/noon_returns.py::apply_outcomes`,
  at most 200 rows of `{noon_return, outcome}`): per row, in its own savepoint, it
  checks Noon Return write, acknowledges receipt with the fixed note "Received
  through New Return", then records the inspection. A refused row (acknowledgement
  included) rolls back alone and the others still apply; each result is
  `{noon_return, ok, outcome, sales_return, message}` with an escaped message. No
  Noon call and nothing is submitted. The list dialog that drives it is the New
  Return dialog (#550, see "Three triggers" above).
- **Response contract for #550 (escaping).** The row result `message` and the
  stored lookup `error` are already HTML-escaped on the server; `noon_return` and
  `outcome` are not escaped. The client must not escape `message` or `error` again
  (it would show `&amp;lt;`), and must escape `noon_return` and `outcome` itself
  when putting them into HTML.
- **Lookup state edge cases.** `queue_manual_lookup` writes `queued` before
  enqueueing; when the enqueue is deduplicated (no new job) a previous `done` or
  `error` result is restored so it cannot sit in `queued` until the cache entry
  expires. `get_lookup_result` only reads keys starting with
  `marketplace-noon-return-result-`; any other key gets the expired-result error.
- **Damaged Warehouse.** `Marketplace Account.noon_damaged_warehouse`, when set,
  must differ from `warehouse`, be a leaf Warehouse and belong to the account's
  `erpnext_company` (checked on save).

## Out of scope (#153)

- Any ERPNext-side correction from a return other than the draft Sales Return of
  #261 and the draft Credit Note of #262 (Payment Entry, refund payment, Sales
  Order change, or any other Sales Invoice adjustment). The local receipt
  acknowledgement is operational evidence only.
- A separate reconciliation report: the evidence surfaces through Needs
  Attention and the planner instead (#164).
- Any write-path Noon call (e.g. acknowledging or resolving a return) —
  `ListReturnReferences` is the only endpoint this issue calls.
