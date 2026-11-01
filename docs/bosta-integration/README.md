# Bosta integration (epic #413)

Status: Batch A built (#408 create on Submit, #409 status sync). Batch B is not built: #410 cancel, delete and edit, #411 returns, #412 costs. The spec is `business-spec.md`; Bosta API facts are `docs.yaml`; the architecture summary is in `docs/marketplace-integrations/architecture.md` ("Bosta shipment of a Delivery Note").

The new design replaces the old Sales Order based flow. Nothing of the old flow is kept behind a compatibility layer.

## How it works

1. A person creates a draft Delivery Note from the Sales Order and sets the Carrier (native `transporter_name`) to the Supplier "Bosta". The Shipping section appears for any carrier listed in `frappe.boot.marketplace_shipping_carriers`.
2. The person picks the Bosta city (button **Pick Bosta City**, suggested from the address), checks the COD, saves, and presses Submit. The COD is computed by default (the note's total minus the order's advance, proportional per note) and **recomputed on every save while the note is editable (Not Created / Creation Failed) until the person confirms it**: ticking **COD confirmed**, or changing the amount, confirms it (a typed 0 is a deliberate choice). Submit refuses a note whose COD was never confirmed. The merchant can change the COD at any time until the shipment is started (Not Created / Creation Failed), also after confirming; a confirmed amount is never recomputed, but if the items or the advance change so that it differs from the computed amount, the form shows a note with the computed amount (`delivery_note_shipment.cod_hint`). An amended note must confirm again, and so must a note amended from a Bosta note and given another carrier (its Bosta fields are cleared). Marker: Check field `custom_cod_confirmed` (a Currency cannot tell "never set" from 0).
3. The form's `before_submit` calls `api.bosta_shipments.request_shipment`; the server sets Integration Status `Creating`, writes an Integration Request log row and enqueues `run_create`. The form shows "Sending to Bosta..." and polls every 2 seconds (up to 60).
4. Created: the form reloads and the native Submit continues. Failed or Unknown: Submit stops with the reason (Unknown offers **Check with Bosta** in the same dialog). **Close, keep sending** ends the wait without stopping the job. The server `before_submit` refuses a note that is not Created, whatever the client says. The label job that follows Created writes `custom_shipping_label` with `update_modified=False`, so it cannot make the native Submit fail with "modified after you opened it". Webhook and refresh writes (`statuses._write_state`) to a draft note do the same. A posted copy loaded before such a write cannot overwrite the app's fields: `guard_changes` puts the stored values of `SYSTEM_FIELDS` back on every save instead of refusing. The Sales Order summary is restored the same way (`statuses.keep_stored_summary`).
5. Webhooks and a nightly reconciliation (`59 23 * * *`) keep `custom_shipping_status` and the Sales Order counts (`custom_shipping_status_summary`) current.

Integration Status (`custom_integration_status`) options: Not Created, Creating, Creation Unknown, Created, Creation Failed, Cancellation Pending, Cancellation Unknown, Cancelled. An empty value means Not Created. The native `docstatus` is the other half of the state. "Unknown" is never "Failed": a timeout, connection error, 5xx/429 or malformed 2xx leaves it Unknown, and only a 4xx (not 408/429) is Creation Failed. Bosta has no idempotency key, so there is no automatic retry and a lookup (`POST /deliveries/search`, `businessReference` = the Delivery Note name) runs before any later attempt. "Later attempt" is decided by `custom_carrier_reference` being set on the note (written when the first attempt starts), never by counting Integration Request rows: Log Settings deletes them after 90 days. The generic retry (`carrier_shipments.retry_shipment`, Noon's Retry button endpoint, write permission only) refuses a Bosta note; only Submit (submit permission, stale-modified check) starts a create. The error text (`custom_last_integration_error`) is stored raw, because Bosta's text is untrusted, and every place that shows it escapes it once (form intro, Needs Attention).

Webhooks: an event that arrives while the note is still Creating (or before any note can claim the delivery, while a create is in flight) stays `Queued` in the Integration Request log and is applied when the shipment is Created (`statuses.replay_pending_events`), or by the nightly run; one that nobody could take for 3 days is closed as ignored. The apply compares the event time with `custom_bosta_last_event_at` inside the row lock, so a late event writes nothing. An event that contradicts the note makes a refresh from Bosta; if the refresh fails the log is `Failed`, not `Success`. The Sales Order summary counts only Bosta Delivery Notes and takes the Sales Order row lock first.

Needs Attention "Bosta Cancelled Delivery" clears when the Delivery Note is cancelled in ERPNext or 14 days after Bosta's cancellation. #410 (cancel from ERPNext) must leave out the cancels the user started themselves.

## Native options checked and rejected

- Shipment doctype with `awb_number` / `tracking_url`: rejected, the Delivery Note is the shipment (decision in `docs/marketplace-integrations/delivery-note-first.md`) and one native `lr_no` already holds the tracking number.
- Separate status doctype, GL posting, workflow: rejected. One Integration Status field plus native `docstatus` is enough; no accounting entry is needed.
- Custom Integration Log doctype: rejected, the native Integration Request plus Custom Fields is used (service = the carrier name).
- Retry queue: rejected for create (unsafe without an idempotency key); the user re-presses Submit or Check.

## Inventory: kept, rewritten, deleted

Kept (unchanged except where noted): `integrations/bosta/client.py`, `constants.py`, `locations.py` (cache only), `city_match.py`, `api/bosta_webhooks.py` (only a comment changed), `api/bosta_settings.py`, the `Bosta Settings` doctype, the pickup location picker, the `import_bosta_module_sidebar` patch.

Rewritten: `integrations/bosta/statuses.py` (webhook, reconcile, summary), `labels.py` (label on the Delivery Note), `api/bosta_shipments.py` (request, check, refresh, label, cities), `services/carrier_shipments.py` (Bosta row, `boot_session`), `services/marketplace_attention.py` and `marketplace_channels.py` (read Delivery Notes), `api/order_hover_card.py` and its JS (counts), `public/js/sales_order_list.js` (summary formatter), the Bosta sidebar and workspace shortcut ("Bosta Delivery Notes").

Created: `integrations/bosta/delivery_note_shipment.py` (the adapter), `public/js/delivery_note_shipping.js` (neutral shell), `public/js/delivery_note_bosta.js`, Delivery Note / Integration Request / Sales Order Custom Fields in `fixtures/custom_field.json`, patches `create_bosta_transporter_supplier` and `migrate_bosta_shipments_to_delivery_notes`.

Deleted: the `Bosta Shipment` and `Bosta Shipment Item` doctypes, `bulk.py`, `deliveries.py`, `editing.py`, `bosta_shipment.js`, `bosta_state_presentation.js`, `bosta_state.css`, all Sales Order Bosta UI (tab, dialogs, bulk create, handover, cancel, edit), the `Bosta User` role, the "Bosta Awaiting Your Action" Notification, the stock reservation and physical handover code, the `*/30` poll, six old backfill patches, their tests and their translations.

## How a next carrier plugs in

1. One adapter module exposing the interface in `services/carrier_shipments.py` (`applies_to`, `guard_changes`, `require_created`, `guard_cancel_or_delete`, `refuse_sales_order_cancel`, `queue`, `retry`, plus `SHIPPING_CARRIER` to show the Shipping section).
2. One row in `CARRIERS` (Noon first, then Bosta; `saved_state` lives in `carrier_shipments`, a note is judged by its stored carrier and by the new copy).
3. A Supplier named like the carrier and its carrier-specific Custom Fields (with their own `depends_on`).
4. A small JS file calling `frappe.marketplace_integrations.shipping.register_carrier(name, {request_method, check_method, refresh_method, add_buttons})`, listed after `delivery_note_shipping.js` in `hooks.py` `doctype_js`.

## Intended extension points for Noon (documented only, not built)

Noon joins the same Shipping section in a separate epic. These are the carrier differences that must stay behind the adapter, so Noon needs no renames or shell rewrite. No hook exists for them yet; add each one when Noon needs it.

- Address source: Bosta reads the Delivery Note shipping address; Noon uses the Marketplace Order data.
- Tracking number: Bosta is API-assigned; Noon takes a manual AWB from a pool before the first save.
- Submit wait: Bosta's Submit waits on a queued job (`request_method`); Noon creates the shipment on save and Submit would not wait.
- Fields editable before creation: Bosta locks carrier, tracking number, COD, city and items once started; Noon locks items and quantities only.
- Status fields: Noon still uses `custom_carrier_status` / `custom_carrier_error`; moving it to `custom_integration_status` / `custom_last_integration_error` is part of the Noon epic.

## Unverified Bosta facts (open questions, safest behavior built)

- Is Bosta search eventually consistent? A lookup that finds nothing after an Unknown does not prove absence; a new create is allowed only on a user-started retry that ran the lookup first.
- The issue text lists state 48/49 on a Created note both as Cancelled and as a conflict; it is treated as Cancelled.
- A blank Shipping Status on a just-created note is counted as "Created" in the Sales Order summary.
- The initial state code after create (10, "Pickup requested") is unverified.
- Notes without a Sales Order are allowed (the platform check is skipped).
- Post-submit API edits of the read-only integration fields are not guarded server side (Batch B).
- A 429 during the nightly run stops the run early and is logged.
- The exception reason is not stored (not in the minimal webhook payload).
- Bosta cancel/terminate semantics are Batch B.
- Default COD: the Sales Order total minus advance payments, proportional per note.

## Migration (patch `migrate_bosta_shipments_to_delivery_notes`)

- All checks run before anything is changed, so a stopped run leaves the old fields, columns, permissions and notifications in place and can simply be run again.
- Stops with a list of **every** Bosta Shipment that is not Canceled and has no existing Delivery Note or no Bosta delivery id: a deleted note, a row without ids, a failed or pending row. Cancel them, let them finish, or give them a Delivery Note on the old code, then migrate again.
- Stops when one note has two live shipments with different Bosta delivery ids, or already holds another id. Several shipments of one note: the non-Canceled one wins. Rows are processed by name; a second run changes nothing.
- Copies state onto the notes (COD marked confirmed), recalculates the Sales Order summary for every migrated order, deletes the three old Sales Order fields (`custom_bosta_delivery_tab`, `custom_bosta_delivery_html`, `custom_bosta_delivery_status`, with their Property Setters and the leftover column), the Bosta Custom DocPerms and Notifications, and the `Bosta User` role.
- Frappe's `remove_orphan_doctypes` runs at the very end of migrate (after patches and fixtures sync) and only deletes the DocType rows; the `tabBosta Shipment` / `tabBosta Shipment Item` tables stay and are dropped by hand (`docs/user-actions.md`). If the DocType row exists but its table does not, the patch fails loudly.
