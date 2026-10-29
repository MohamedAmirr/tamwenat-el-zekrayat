# Delivery Note-first fulfillment (decided 2026-10-29)

Status: decided. Epic #392; sub-issue 1 (#391, Noon create from a draft Delivery Note) and #393 (the note carries cancel, delete, Add AWB, shipment invoice and Retry) are built; #394 (remove the superseded handover flow) is later, see `docs/noon-integration/fulfillment-release.md` (Delivery Note-first shipment). This document records the agreed direction so the plan, the issues and the code follow it.

## Decision

Fulfillment is driven from the Delivery Note instead of from each platform's own shipment flow.

1. A person creates a draft Delivery Note from the Sales Order (ERPNext's `make_delivery_note`).
2. Creating the draft triggers a per-platform adapter that calls the platform or carrier API to create the shipment (Noon: `CreateShipment`; Bosta: create delivery).
3. The tracking number and AWB come back and are saved on the Delivery Note (or on ERPNext's native Shipment doctype if the native check below prefers it).
4. A person reviews and submits the Delivery Note. The app never submits it.
5. If the carrier call fails, the draft stays and the error shows in Needs Attention with a retry.
6. Deleting or cancelling the Delivery Note cancels the carrier shipment. Cancelling a Sales Order with a live Delivery Note is blocked by ERPNext's own linked-document check, so no custom cancel-refusal rules are needed.

Why: one flow for every platform (a new platform is one adapter), stock moves correctly for Shopify and Talabat (today only Bosta creates a Delivery Note, see #368), and the dependency between shipment and Delivery Note is enforced by ERPNext instead of by custom tracking code.

## Rules agreed

- One Delivery Note per shipment. For Noon, units of the same SKU are interchangeable, so the adapter picks `mp_item_nr` values by SKU and quantity from the Delivery Note rows.
- The carrier is ERPNext's native `transporter` on the Delivery Note (Supplier "Noon" for Noon), not a custom Carrier field. On the form it is labelled "Carrier", and `lr_no` "AWB / Tracking No" (Property Setters in `fixtures/property_setter.json`); both, with `lr_date`, sit under the customer on the Details tab so the AWB can be typed before the first save. It is locked, with the item rows and `lr_no`, once the adapter has started the shipment. Defaulting the transporter per Marketplace Account (Shopify to Bosta) is deferred to the Bosta work.
- Mark Out of Stock stays a standalone dialog calling Noon `UpdateOrder` (valid only before a shipment exists). On HTTP 200 the existing sync and reconciliation planner updates the Sales Order. It is not part of the Delivery Note.
- The Delivery Note is created by a person from the Sales Order, not automatically on Sales Order submit, because automatic shipment creation would remove the chance to mark units out of stock first.
- Noon is built first and finished fast. Bosta follows, fully automatic from account defaults, with Needs Attention when the city does not match. The adapter seam stays minimal until Bosta is the second real adapter.
- Rely on ERPNext native features and do not re-invent until really needed. Every custom field, doctype, hook or guard in the plan must name the native option that was checked (native Delivery Note transporter and `lr_no` fields, the native Shipment doctype with `awb_number` and `tracking_url`, native linked-document cancel checks, native delivery status) and why it was rejected.
- No ERPNext or Frappe core files are edited. Everything lives in `marketplace_integrations`: Custom Fields as fixtures, `doc_events` in `hooks.py`, whitelisted methods, client scripts.

## Parked work

Branch `feat/noon-shipped-batch` (#390, #377, #378, #376, unreviewed wip) is parked as reference only and is neither built on nor deleted. It was status-driven (a Noon `SHIPPED` unit drafts and submits the Delivery Note) and depended on the unverified meaning of `INTEGRATION_ITEM_STATUS_SHIPPED`. Pieces that may be re-mined later: #377 (draft Sales Invoice from submitted Delivery Notes), #378 (Noon tab and Delivery Status list field), the #390 per-line planner and single-order webhook refresh. Not carried over: automatic drafting from `SHIPPED`, the expanded `handover_state`, and the #376 cancel guard (replaced by ERPNext's native check).

## Existing Noon code this replaces

`integrations/noon/fulfillment.py` (create, cancel, handover, discard, retry paths), `integrations/noon/handover_state.py`, `services/delivery_note_drafts.py`, `public/js/noon_fulfillment.js`, `api/noon_fulfillment.py`. Kept as needed: the AWB pool, Add AWB, invoice upload, the webhook refresh. The Epic must list exactly what is kept, rewritten and deleted, plus the migration for shipments created under the old flow.
