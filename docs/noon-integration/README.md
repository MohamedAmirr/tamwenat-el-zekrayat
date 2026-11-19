# Noon API reference snapshot

`noon_api_full.md` and `noon_api_full.yml` are a 2026-09-29 AI-normalized
snapshot of the public [Noon API documentation index](https://noon-docs.noonpartners.dev/llms.txt).
They are reference material, not an official OpenAPI specification. The Markdown
file is copied unchanged from the supplied snapshot. The YAML file is copied
with its 51 empty or null `source_url` values restored from the matching
endpoint sections of the Markdown file; the remaining supplied content is
unchanged.

The snapshot has extraction gaps. Many structured `fields` and `rate_limit`
values are the literal string `[object Object]`; those strings are not usable
schemas or quotas. The `BatchChildSkuRename` entry has no method or path and
explicitly marks its request and response schemas unavailable. The API login
entry does not expose a usable response schema. Consult each endpoint's
`source_url` and the official guides before implementing it, and leave any
unavailable value marked as unknown rather than filling it by inference.

For issue #143's authentication flow, also consult the official
[Quick Start](https://noon-docs.noonpartners.dev/docs/getting-started/quick-start),
[Authenticating Your Requests](https://noon-docs.noonpartners.dev/docs/authentication/authenticating-requests),
[Getting Your Credentials](https://noon-docs.noonpartners.dev/docs/authentication/getting-credentials),
[Errors](https://noon-docs.noonpartners.dev/docs/overview/errors),
[Rate Limits & Usage](https://noon-docs.noonpartners.dev/docs/overview/rate-limiting),
and [Static Sandbox](https://noon-docs.noonpartners.dev/docs/overview/sandbox)
guides. These guides are online sources, not copied into this snapshot.

## Order reconciliation and Noon support guidance (#525)

Noon API Platform support case (incident 8-9 Nov 2026, delayed `FBPI::ORDER_SYNC`
events). Noon's answers, relayed by the user (not a checked-in spec):

- There is no guaranteed maximum webhook delay. Reconcile with `ListFbpiOrders`
  every 15-30 minutes per FBPI warehouse, with a `created_after`/`created_before`
  window that overlaps the previous run, 50 orders per page via `next_token`,
  idempotent against the webhook.
- Retry HTTP 429 with exponential backoff.
- Rate Limit Analytics: Noon Developer Portal > Monitoring > Rate Limit Analytics.
  Noon also has an API health page for platform incidents.
- Express orders use the same event and the same APIs.

What the app does: `integrations/noon/orders.py::poll_all_accounts` runs at minutes
`1,27,53` (about every 25 minutes at :01, :27 and :53; `NOON_ORDER_POLL_CRON`, gaps of 26, 26 and 8 minutes, all odd and none a multiple of 5, so none shares a tick with the `*/2` sweep or the `*/10` and on-the-5 jobs) and lists the last
2 hours. The poll has its own per-account RQ job id
(`marketplace-noon-order-import-poll-<account>`), like the nightly `-catch-up` one, so a
queued or running poll never drops a webhook or manual 7-day import (which keep
`marketplace-noon-order-import-<account>`), and a second poll is still deduped. Overlapping
jobs on one account are safe: `import_order` takes the `FOR UPDATE` lock on the order's
identity key first, and `upsert_marketplace_order` falls back on the unique
`order_identity_key` when two workers insert the same new order. The daily 7-day re-list is the existing 23:59
`enqueue_nightly_order_reconciliation` catch-up (`services/marketplace_order_reconciliation.py`),
so later status changes on already-seen orders are still caught. An order whose import
failed more than about 2 hours ago is not retried by the poll; it is retried by the
23:59 catch-up or by the manual "Import Noon Orders" action. `list_orders` retries a 429 page
3 times (2, 4, 8 s, `Retry-After` ignored); `NoonClient.request()` never retries.

## Noon shipment from the Delivery Note (#415)

The Delivery Note of a Noon Sales Order is the shipment. The Noon adapter
(`integrations/noon/delivery_note_shipment.py`) plugs into the neutral carrier contract
(`services/carrier_shipments.py`); see `docs/marketplace-integrations/delivery-note-first.md`.

- **Create on Submit.** The form waits for a background job ("Sending to Noon..."); once the Integration
  Status is Created the job itself submits the note as the user who pressed Submit (decided 2026-11-02,
  `docs/marketplace-integrations/delivery-note-first.md`; the browser only reloads). If that submit fails the
  note stays a Created draft with the reason in Last Integration Error, Needs Attention flags it (Carrier
  Exception, also any Created draft older than 15 minutes) and a person submits it natively. The job asks Noon first (`GetShipment`) on every attempt, the first
  included; only a confirmed "No shipment found" allows `CreateShipment`. A timeout, 5xx, 408/429 or an
  unrecognised answer is Creation Unknown (the note stays in the Shipment Requested stage; press Retry Shipment, a
  Workflow action that runs the same job again and looks the shipment up first; Check with Noon only reads); a
  definite 4xx is Creation Failed (the note goes back to Order Received, where Create Shipment works again).
  Nothing is retried automatically.
- **Shipment number** = the Delivery Note name, stored in Carrier Reference at the first attempt. An amended
  note has a new name, so a new number.
- **AWB.** Type it (sent with courier `noon`), or tick Generate AWB through Noon under it: the job calls
  `GetNoonLogisticsAWBs` once (qty 1), writes the AWB to the note and logs an Integration Request row
  (operation AWB) before `CreateShipment`. A retry reuses the stored AWB. An AWB held by another Delivery Note
  that is not cancelled is refused, ignoring case.
- **Units** come from the order's latest payload, by SKU and quantity (Confirmed and Acknowledged only).
  Units that another live Noon note started are read from that note's request snapshot.
- **No COD, no address, no Shipping Rule.** The carrier is the locked Supplier Noon; the note clears its
  Shipping Rule and the tax row that rule added.
- **Cancel.** Noon is asked first (`GetFbpiOrder`): every unit of the shipment already cancelled is success
  with no cancel call. Otherwise `CancelShipment`: 200 cancels, a definite 4xx leaves the note submitted with
  Noon's reason, anything else is Cancellation Unknown (Retry asks Noon first again). The ERPNext cancel
  follows only after Noon confirms. **Noon's "Shipped" is not a block (#541):** a submitted note showing Shipped is
  in the stage Ready for Pickup and is cancelled from Status > Cancel (`public/js/delivery_note_noon.js::before_workflow_action`,
  the same Noon-first cancel as the native Cancel; a refusal shows "Noon refused: ..." and nothing is cancelled in ERPNext, an
  unknown answer offers Retry / Check with Noon). `picked_up` (used by the Sales Order cancel) is true only when the stage is
  Picked Up, In Transit or Delivered: the Sales Order cancel cancels a Ready for Pickup shipment at Noon first, which holds the note (#552),
  and refuses a note at Picked Up or later up front, pointing to a Sales Return. Changing the AWB or items of a created shipment is cancel and amend: the cancelled note stops at Awaiting Return, a Noon User or System Manager enters the returned quantities in Confirm Returned (which cancels the note), and only then is the note amended. A person cancelling a note or shipment never cancels the Sales Order from here, not even through Confirm Returned; the only trigger is Confirm Returned on a note whose shipment Noon itself cancelled (the cancel is marked ours in the request snapshot, `cancelled_by`, when this app cancels it), plus a stored Noon order with no eligible unit (`services/order_cancel_cascade.py`). **Held at any stage (#527, #552):** once a submitted note has a created Noon shipment, ANY cancel of that shipment (a read that finds every unit cancelled, an all-lines-Out-of-Stock note Noon then cancels, or our own Cancel, native or Status > Cancel from Ready for Pickup, which still sends CancelShipment to Noon automatically; the Sales Order cascade too) leaves the note held: `refresh` / `cancel_remote` do not cancel the note, the workflow stage no longer decides. The Cancel from the form or the Status menu stops after Noon confirmed and shows the held note. The Sales Order cancel cancels every live Noon shipment of the order first (each note is held), then stops once with one instruction: Confirm Returned on each held note, then cancel the order again (a submitted note blocks it). The note stays submitted with Integration Status Cancelled + Shipping Status Awaiting Return (`SHIPPING_AWAITING_RETURN`, `is_held`), one Notification Log is sent, and the native cancel is refused (`guard_cancel`) until a Noon User presses Confirm Returned (`confirm_returned(delivery_note, lines, write_off)`; API `get_return_lines` / `confirm_returned` in `api/noon_fulfillment.py`; form button in `delivery_note_noon.js`). Details: `docs/marketplace-integrations/delivery-note-first.md`.
  Noon documents (CancelShipment in `noon_api_full.md`): "Cancels an existing shipment. All items included in
  that shipment will also be canceled". So the order sync may then cancel or shrink the Sales Order by itself
  and the units may not be shippable again; the Cancel confirmation says so. UNVERIFIED against real Noon (see
  business-spec section 12).
- **Stage of a submitted Noon note (#540).** Shipping Status "Shipped" is set by Noon right after the shipment is
  created and does NOT mean picked up. Noon has no Delivered status (its statuses are Acknowledged, Shipped, Out of
  Stock, Cancelled, Mixed). `delivery_note_workflow.state_for` maps a submitted Noon note (`transporter_name` Noon) to
  Ready for Pickup whatever the status says, except Cancelled / Out of Stock (Returned / Failed). Picked Up and Delivered are manual Workflow actions (Confirm Pickup, Confirm Delivery incl. from In Transit,
  Stock User, bulk from the list Actions menu); `state_of_row` keeps a stored Picked Up / In Transit / Delivered so no
  Refresh, webhook or nightly read moves it. Notes that were moved to In Transit by the old Shipped mapping stay as they are.
  A Noon draft submitted by hand with Confirm Shipment is corrected to Ready for Pickup by `fix_state_after_submit`
  (the timeline comment of that action may still say Picked Up).
- **Status and refresh.** `refresh` (Refresh from Noon button, synchronous: HTTP first, then lock and write)
  calls `GetShipment` and `GetFbpiOrder` and derives Shipping Status from the units in the request snapshot:
  all units agree = Acknowledged / Shipped / Out of Stock, all cancelled = Cancelled (terminal), units differ
  or some cancelled = Mixed. The snapshot holds only `mp_item_nr`, so a unit is placed on a row through its
  `partner_sku` and the product mapping (units of an Item are pooled over its rows); that row status is
  `custom_noon_item_status`. Refresh never edits items. Cancelled units, Mixed or items that differ from the
  snapshot set Last Integration Error and show in Needs Attention (Carrier Exception). A Cancellation Unknown
  note resolves to Cancelled when every unit shows cancelled (no cancel call). **Automatic native cancel (decided 2026-11-01):** when `refresh` (Refresh button, `FBPI::ORDER_SYNC` job or nightly reconcile) finds every unit of a Created, submitted note cancelled at Noon (or resolves Cancellation Unknown to Cancelled), it writes Integration Status Cancelled (clearing its own "Noon shows this shipment as cancelled" text) and calls the neutral `carrier_shipments.cancel_after_carrier_cancelled`: the system cancels the note natively with no Noon call, once (since 2026-11-08 it first cancels the submitted Sales Invoice(s) that bill only this note, e.g. the one a user server script creates on submit, in the same transaction; an invoice with a Payment Entry, Journal Entry or Sales Return against it, a POS or not fully unpaid one, or one that also bills other notes, blocks everything; a return note's standalone credit note is cancelled too, but a credit note issued against another invoice (return_against, outstanding 0 while the total is negative) is refused as "not fully unpaid" because cancelling it would change the original invoice's balance; each cancelled invoice gets a comment on the invoice and is named in the success notification, which goes to the note's submitter and enabled Stock Managers (no Accounts role is notified; a timeline comment notifies nobody): details in `docs/bosta-integration/README.md`, Cancel). Any other linked submitted document or an error leaves the note submitted with the reason in Last Integration Error (Needs Attention "Carrier Exception"). Either result sends one Notification Log, linked to the note, to the submitter and every enabled Stock Manager (details in `docs/bosta-integration/README.md`, Cancel). A draft note is not changed (the Noon error text stays). Noon has no `after_submit` hook (the label job is Bosta's).
- **Triggers.** The `FBPI::ORDER_SYNC` webhook path (`orders.refresh_order`) queues one deduplicated
  `noon-refresh|<note>` job per submitted Noon note (drafts ignored). The nightly `noon-reconcile` long job
  (23:59, from `carrier_shipments.reconcile_open_notes`) commits per note and survives one note's failure:
  Creating (stale) / Creation Unknown = lookup only (`check`, never creates); Cancellation Unknown and Created
  = refresh (a Created note is read until its stage is Delivered, Noon shows it Cancelled, or it is 60 days old; Noon's
  Shipped no longer stops it, #541); a draft with a shipment number whose shipment Noon has = the error "draft holds a Noon
  shipment" (Needs Attention, Carrier Creation Problem).
- **Retired in #415 (phase 4).** Removed: the AWB pool (`awb_pool.py`, `api/noon_awbs.py`, doctype `Noon AWB`, the Generate Noon AWBs button), the old note flow (`delivery_note.js`, `delivery_note_drafts.py`, the `after_insert` hook, Retry endpoint), the Sales Order shipment actions, the handover chain (`handover_state.py`, Mark Handed Over, its Needs Attention rows), `custom_carrier_status` / `custom_carrier_error` and the doctypes `Marketplace Order Shipment`, `...Unit`, `...AWB`. Kept in `fulfillment.py`: Mark Out of Stock, the order invoice, the stuck-reservation release and the shared helpers. The per-shipment invoice returns on the Delivery Note with #416. Order reconciliation, the return AWB scan, the sync debounce and Mark Out of Stock read started units and AWBs from the Noon Delivery Notes (`started_units_of_order`, `recent_awbs_of_orders`, `has_created_note`).
- **FBPI warehouse check (#416).** The Noon Marketplace Account form has a Check Noon Warehouse button
  (`api/noon_catalog.check_noon_warehouse`, needs write on the account). It calls `ListWarehouses` (all pages;
  Noon filters only by fulfillment system code, so the account's `noon_warehouse_code` is picked from the list)
  and stores `noon_fulfillment_system_code`, `noon_warehouse_active` and `noon_warehouse_checked_on`
  (read-only). The call is logged as an Integration Request (operation Warehouse, reference the account).
  Noon does not document which code means FBPI. The code is the constant `catalog.FBPI_SYSTEM_CODE = "fbpi"`
  (no account field): it is the single value observed on the user's account, not documented by Noon, so verify it
  on the first real shipment. `catalog.fbpi_ready(account) -> (ok, reason)` is true only when the stored code
  equals it (case-insensitive) and the warehouse is active; any other code is refused with a message showing the
  code Noon returned. It reads stored values and never calls Noon. `check_fbpi_warehouse` (in `validate_ready`) refuses Create/Submit with its reason. Changing the
  account's warehouse code or connection clears the stored answer (check again). Mark Out of Stock and the
  invoice actions reuse `fbpi_ready` in the next sub-issue #416 runs.
- **Mark as Out of Stock (#416).** A "Mark as Out of Stock" Button (Custom Field `custom_noon_mark_out_of_stock`) in the
  row form of a submitted Noon Sales Order item; `noon_fulfillment.js` shows it only for rows
  `api.noon_fulfillment.get_out_of_stock_rows` returns (needs write on the Sales Order only). The old unit
  checklist dialog and its endpoints are gone. `mark_item_out_of_stock(sales_order, sales_order_item)` refuses a
  non-FBPI warehouse (`fbpi_ready`), refuses (naming the note) a row whose Item is in a Draft or submitted
  Delivery Note, and reserves the Item's units in `noon_pending_oos_units` before queueing one job per order and
  unit set. After a confirm ("final at Noon, may cost cancellation fees") the form shows "Sending to Noon..." and
  polls `check_mark_out_of_stock` (outcome kept in the cache for an hour; the reservation and the log are the
  record). The job sends UpdateOrder once, then GetFbpiOrder, `import_order` and `sync_sales_order`, whose planner
  removes the row through native Update Items (the edit guard passes it inside `marketplace_syncing_orders`).
  Definite 4xx: error dialog with Retry, reservation released. Unknown (timeout, 5xx, 408/429): the reservation
  stays; Retry (a press while the reservation exists) runs GetFbpiOrder first and sends UpdateOrder only for units
  Noon does not already show `INTEGRATION_ITEM_STATUS_OUT_OF_STOCK`. Noon confirmed but the local step failed:
  the reservation stays (Needs Attention "units pending Out of Stock"), Retry repeats only the local step. Every
  Noon call is an Integration Request (operation Out of Stock, reference the Sales Order, no customer data).
  Release Stuck Out of Stock Reservation stays for a dead worker.
- **Invoices (#416, spec section 9).** Backup step: never blocks Submit or Cancel (no document event calls it).
  "Upload Shipment Invoice" is a button on a submitted Noon Delivery Note whose Integration Status is Created;
  "Upload Order Invoice" (and Retry Order Invoice) are in the Noon Fulfillment group of the submitted Noon
  Sales Order. The PDF is attached by the person (Attach, invoice number, issue date; nothing is generated), must
  be private, a real PDF and at most 5 MiB, and is saved as a native private File attached to the document the
  button was used on (`fulfillment.attach_invoice_file`). Then a job sends it: `delivery_note_shipment.run_upload_invoice`
  (UploadShipmentInvoice, shipment number = `custom_carrier_reference`) or `fulfillment.run_upload_order_invoice`
  (UploadFbpiOrderInvoice). One idempotency key per press; Retry reuses it, a new press makes a new key (Noon
  supersedes the earlier invoice for the same shipment or order, so the dialog warns first). Both refuse unless
  `catalog.fbpi_ready(account)` is ok. The order invoice keeps its state in `Marketplace Order.noon_order_invoice_*`;
  the note records `custom_noon_invoice_uploaded_on` (written only on a
  successful upload, cleared on amend). One upload per note runs at a time (a double press is refused). The dialog ("Sending to Noon...", polls `api.noon_fulfillment.check_invoice_upload`)
  ends in a small alert or an error dialog with Retry. A shipment-invoice failure keeps no state on the note:
  Retry works from the open dialog (the key and the attached copy come back with the first answer), after a
  reload a new press is a new key. Rights: write on the Delivery Note / write on the Sales Order (no Marketplace
  Order right needed). Every call is an Integration Request (operation Invoice, reference the document, the
  idempotency key as correlation id, no file bytes); the invoice writes no Marketplace Sync Log row.
  Open items to verify on the first real use: when Noon accepts an invoice (order or shipment state), rate limits.

## Provider contract

The Noon adapter follows the standing carrier contract in `docs/bosta-integration/README.md` ("Provider contract"): the same members (`SHIPPING_CARRIER`, `SERVICE_NAME`, `applies_to`, `guard_changes`, `clear_inherited`, `validate_ready`, `find_remote`, `create_remote`, `created_values`, `after_created`, `cancel_remote`, `refresh`, `reconcile`, `refuse_sales_order_cancel`, `problem_states`). Noon specifics: `find_remote` is GetShipment by warehouse code and shipment number (absent only per `fulfillment._is_shipment_not_found`); `create_remote` runs the AWB step, then CreateShipment; `cancel_remote` asks GetFbpiOrder first, then CancelShipment; `after_created` is a no-op (Noon has no label endpoint); `problem_states` is empty (Noon problems reach Needs Attention through Last Integration Error). A typed own AWB is always sent with courier `noon`. Editing the AWB or items of a created shipment is cancel and amend (Noon has no item-edit endpoint). Neutral code never names Noon.
