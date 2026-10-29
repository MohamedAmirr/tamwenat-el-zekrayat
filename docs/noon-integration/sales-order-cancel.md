# Cancelling a Noon Sales Order: wait for Noon (#376)

Design note. Scope was reduced by the user: only **Created** shipments are cancelled on Noon
(`CancelShipment`). Units are not marked out of stock and nothing is rejected toward the customer.
Units that are in no shipment are left as they are on Noon.

## Why this differs from Shopify and Talabat

Shopify and Talabat cancel in ERPNext first and then offer to cancel on the platform
(`services/marketplace_outbound.py`, `sales_order.js`). Noon works the other way round, "wait for
Noon": the Noon calls go first, and the Sales Order cancel is refused until they have succeeded. The
Shopify and Talabat flow, the echo-guard marker and `request_platform_cancel` are not touched.

## Order of events

1. The user clicks **Cancel on Noon** (Noon Fulfillment group, Sales Order form). A dialog lists the
   Created shipments that will be cancelled on Noon, says that Noon also cancels their units, that
   units outside a shipment are not changed, and that the Sales Order cancel completes only after
   Noon has confirmed. Closing the dialog sends nothing.
2. On confirm, `fulfillment.cancel_created_shipments` calls the existing `cancel_shipment` for each
   Created shipment (status becomes Cancel Pending, committed, `run_cancel_shipment` queued on the
   long queue with its existing retry rules). No Noon call runs in the request.
3. The worker sets each shipment to Cancelled on a 200 answer; a failure keeps it in Cancel Pending
   with `last_error` and a `Marketplace Sync Log` Error row (existing Needs Attention behaviour). The
   user retries or discards through the existing Retry / Discard Shipment buttons.
4. The user cancels the Sales Order. A server guard (`before_cancel` on Sales Order,
   `services/noon_cancel_guard.py`) refuses the cancel while any shipment is Pending, Created, Cancel
   Pending or Error. Cancelled and Discarded shipments never block. The refusal names each shipment
   and its status. Pressing the native Cancel early is therefore safe: nothing is sent to Noon and
   nothing is cancelled.
5. Close is not guarded (ERPNext fires no event the app can refuse on, and overriding the whitelisted
   method is a last resort).

## Handed-over shipments

A shipment that is not Cancelled or Discarded and was handed over (`handed_over_on` set, or a live
draft or submitted Delivery Note per `handover_state.live_notes`) refuses the Sales Order cancel with
a message to cancel on Noon first and then cancel the linked Delivery Note. This is enforced by the
same server guard. `cancel_created_shipments` skips a shipment with a live Delivery Note and reports
it (the existing `cancel_shipment` also refuses it).

## Partial failure

- Some shipments queue, others fail to queue (for example one is no longer Created): the response has
  `queued`, `skipped` and `failed` lists, the dialog reports them, and the shipments that did queue
  stay queued. Calling it again only touches shipments that are still Created.
- A queued call that Noon rejects or that times out leaves the shipment in Cancel Pending with the
  error; the Sales Order cancel stays refused (Cancel Pending blocks). No silent disagreement.
- The Sales Order is never cancelled by the Noon worker. The user cancels it after the shipments show
  Cancelled.

## Re-cancel after an earlier attempt

- Shipments already Cancel Pending or running are not Created, so they are not queued twice; the job
  id is deduplicated per shipment as before.
- `CancelShipment` is resent by the existing Retry for Cancel Pending (Noon's behaviour on a double
  cancel is verification item 5 in `fulfillment-release.md`).
- Pressing Cancel on the Sales Order again just re-runs the guard.

## Customer cancels on Noon

The reconciliation planner cancels the Sales Order when every unit is cancelled on Noon, possibly
while local shipments are still Created. It runs inside `sales_order_sync`, which registers the
Marketplace Order in `frappe.flags.marketplace_syncing_orders`; the guard passes while the order is
in that set (the same marker `noon_item_edit_guard` uses). So that path is unchanged.

## Not covered

- Units outside a shipment are not marked out of stock; nothing is rejected toward the customer.
- No order-level cancel on Noon exists.
- Real Noon behaviour of `CancelShipment` on a Created shipment is still to be checked
  (`fulfillment-release.md`, verification list).
