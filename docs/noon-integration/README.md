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

## Noon shipment from the Delivery Note (#415)

The Delivery Note of a Noon Sales Order is the shipment. The Noon adapter
(`integrations/noon/delivery_note_shipment.py`) plugs into the neutral carrier contract
(`services/carrier_shipments.py`); see `docs/marketplace-integrations/delivery-note-first.md`.

- **Create on Submit.** The form waits for a background job ("Sending to Noon...") and submits only once
  the Integration Status is Created. The job asks Noon first (`GetShipment`) on every attempt, the first
  included; only a confirmed "No shipment found" allows `CreateShipment`. A timeout, 5xx, 408/429 or an
  unrecognised answer is Creation Unknown (Check Noon, then Submit again); a definite 4xx is Creation Failed.
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
  follows only after Noon confirms. Changing the AWB or items of a created shipment is cancel and amend.
- **Status and refresh.** `refresh` (Refresh from Noon button, synchronous: HTTP first, then lock and write)
  calls `GetShipment` and `GetFbpiOrder` and derives Shipping Status from the units in the request snapshot:
  all units agree = Acknowledged / Shipped / Out of Stock, all cancelled = Cancelled (terminal), units differ
  or some cancelled = Mixed. The snapshot holds only `mp_item_nr`, so a unit is placed on a row through its
  `partner_sku` and the product mapping (units of an Item are pooled over its rows); that row status is
  `custom_noon_item_status`. Refresh never edits items. Cancelled units, Mixed or items that differ from the
  snapshot set Last Integration Error and show in Needs Attention (Carrier Exception). A Cancellation Unknown
  note resolves to Cancelled when every unit shows cancelled (no cancel call). **Automatic native cancel (decided 2026-11-01):** when `refresh` (Refresh button, `FBPI::ORDER_SYNC` job or nightly reconcile) finds every unit of a Created, submitted note cancelled at Noon (or resolves Cancellation Unknown to Cancelled), it writes Integration Status Cancelled (clearing its own "Noon shows this shipment as cancelled" text) and calls the neutral `carrier_shipments.cancel_after_carrier_cancelled`: the system cancels the note natively with no Noon call, once. A linked submitted document or an error leaves the note submitted with the reason in Last Integration Error (Needs Attention "Carrier Exception"). Either result sends one Notification Log, linked to the note, to the submitter and every enabled Stock Manager (details in `docs/bosta-integration/README.md`, Cancel). A draft note is not changed (the Noon error text stays). Noon has no `after_submit` hook (the label job is Bosta's).
- **Triggers.** The `FBPI::ORDER_SYNC` webhook path (`orders.refresh_order`) queues one deduplicated
  `noon-refresh|<note>` job per submitted Noon note (drafts ignored). The nightly `noon-reconcile` long job
  (23:59, from `carrier_shipments.reconcile_open_notes`) commits per note and survives one note's failure:
  Creating (stale) / Creation Unknown = lookup only (`check`, never creates); Cancellation Unknown and Created
  = refresh; a draft with a shipment number whose shipment Noon has = the error "draft holds a Noon
  shipment" (Needs Attention, Carrier Creation Problem).
- **Retired in #415 (phase 4).** Removed: the AWB pool (`awb_pool.py`, `api/noon_awbs.py`, doctype `Noon AWB`, the Generate Noon AWBs button), the old note flow (`delivery_note.js`, `delivery_note_drafts.py`, the `after_insert` hook, Retry endpoint), the Sales Order shipment actions, the handover chain (`handover_state.py`, Mark Handed Over, its Needs Attention rows), `custom_carrier_status` / `custom_carrier_error` and the doctypes `Marketplace Order Shipment`, `...Unit`, `...AWB`. Kept in `fulfillment.py`: Mark Out of Stock, the order invoice, the stuck-reservation release and the shared helpers. The per-shipment invoice returns on the Delivery Note with #416. Order reconciliation, the return AWB scan, the sync debounce and Mark Out of Stock read started units and AWBs from the Noon Delivery Notes (`started_units_of_order`, `recent_awbs_of_orders`, `has_created_note`).
- **FBPI warehouse check (#416).** The Noon Marketplace Account form has a Check Noon Warehouse button
  (`api/noon_catalog.check_noon_warehouse`, needs write on the account). It calls `ListWarehouses` (all pages;
  Noon filters only by fulfillment system code, so the account's `noon_warehouse_code` is picked from the list)
  and stores `noon_fulfillment_system_code`, `noon_warehouse_active` and `noon_warehouse_checked_on`
  (read-only). The call is logged as an Integration Request (operation Warehouse, reference the account).
  Noon does not document which code means FBPI, so the user looks at the stored code and types it once into
  the FBPI Code field (`noon_fbpi_code`). `catalog.fbpi_ready(account) -> (ok, reason)` is true only when the
  FBPI Code is set, equals the stored code and the warehouse is active; it reads stored values and never calls
  Noon. `check_fbpi_warehouse` (in `validate_ready`) refuses Create/Submit with its reason. Changing the
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
