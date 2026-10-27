# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Permission-checked Desk boundary for Noon FBPI shipment fulfillment
(#146, decision 20).

Thin by design, matching ``api/noon_orders.py``: every endpoint here checks
write permission on the Marketplace Order and that it is actually a Noon
order, then delegates to ``integrations/noon/fulfillment.py`` - which does
its own row-locking and enqueues the real Noon HTTP call on the ``long``
queue. No Noon HTTP call ever happens inside a Desk request.
"""

import frappe

from marketplace_integrations.integrations.noon import fulfillment, handover_state


def _get_noon_order(marketplace_order):
	"""#146 round-3 review finding #1: lock FIRST, then load - this used to
	call ``frappe.get_doc()`` (a plain consistent read) before the actual
	fulfillment call below took its own ``FOR UPDATE`` lock on the same row.
	Under MariaDB's ``innodb_snapshot_isolation=ON``, that earlier plain read
	could establish this request's transaction read view ahead of the
	locking read, raising error 1020 (``frappe.QueryDeadlockError``) if the
	row had since changed - exactly the same ordering bug fixed in
	``integrations/noon/orders.py::import_order()``. Locking here first (via
	the identical ``FOR UPDATE`` ``_lock_and_load_order()`` helper uses)
	means every later read in this request - including the one inside
	whichever ``fulfillment.py`` function actually re-locks this row - sees
	a transaction whose read view was never stale to begin with."""
	frappe.db.get_value("Marketplace Order", marketplace_order, "name", for_update=True)
	doc = frappe.get_doc("Marketplace Order", marketplace_order)
	doc.check_permission("write")
	if doc.platform != "Noon":
		frappe.throw(frappe._("Marketplace Order {0} is not a Noon order.").format(doc.name))
	return doc


@frappe.whitelist(methods=["POST"])
def get_shippable_units(marketplace_order: str):
	order = _get_noon_order(marketplace_order)
	return fulfillment.get_shippable_units(order)


@frappe.whitelist(methods=["POST"])
def get_available_actions(marketplace_order: str):
	"""Read-only convenience for the Sales Order form: which Noon Fulfillment buttons to show,
	plus a compact copy of the shipments for its dialogs and read-only summary. No lock needed
	(nothing is written), so this loads directly and only needs read permission; every action
	still validates server side."""
	doc = frappe.get_doc("Marketplace Order", marketplace_order)
	doc.check_permission("read")
	if doc.platform != "Noon":
		frappe.throw(frappe._("Marketplace Order {0} is not a Noon order.").format(doc.name))
	return {**fulfillment.get_available_actions(doc), "shipments": _shipment_summaries(doc)}


def _shipment_summaries(doc):
	"""Display values of each shipment: status, units grouped by SKU (``Item name (SKU)`` x qty), AWBs, Delivery Note and the units that could not
	be put on a Delivery Note (as text)."""
	awbs = {}
	for awb in doc.noon_shipment_awbs or []:
		awbs.setdefault(awb.integration_shipment_nr, []).append({"courier": awb.courier, "awb_nr": awb.awb_nr})
	items = fulfillment.shipment_items(doc)
	return [
		{
			"integration_shipment_nr": row.integration_shipment_nr,
			"status": row.status,
			"items": items.get(row.integration_shipment_nr, []),
			"awbs": awbs.get(row.integration_shipment_nr, []),
			"delivery_note": row.delivery_note or "",
			"delivery_notes": handover_state.shipment_notes(row),
			"handed_over": fulfillment._handed_over(row),
			"unresolved": [handover_state.entry_text(entry) for entry in handover_state.load_entries(row)],
		}
		for row in doc.noon_shipments or []
	]


@frappe.whitelist(methods=["POST"])
def create_shipment(marketplace_order: str, mp_item_nrs, awb_source: str = "Noon"):
	_get_noon_order(marketplace_order)
	if isinstance(mp_item_nrs, str):
		mp_item_nrs = frappe.parse_json(mp_item_nrs)
	return fulfillment.create_shipment(marketplace_order, mp_item_nrs, awb_source)


@frappe.whitelist(methods=["POST"])
def cancel_shipment(marketplace_order: str, integration_shipment_nr: str):
	_get_noon_order(marketplace_order)
	return fulfillment.cancel_shipment(marketplace_order, integration_shipment_nr)


@frappe.whitelist(methods=["POST"])
def mark_handed_over(marketplace_order: str, integration_shipment_nr: str):
	"""Manual handover of a Created shipment: drafts one Delivery Note (#368).

	No Noon call. The shared service also needs create permission on Delivery Note and checks it.
	"""
	_get_noon_order(marketplace_order)
	return fulfillment.mark_handed_over(marketplace_order, integration_shipment_nr)


@frappe.whitelist(methods=["POST"])
def discard_shipment(marketplace_order: str, integration_shipment_nr: str):
	_get_noon_order(marketplace_order)
	return fulfillment.discard_shipment(marketplace_order, integration_shipment_nr)


@frappe.whitelist(methods=["POST"])
def release_stuck_oos_reservation(marketplace_order: str):
	_get_noon_order(marketplace_order)
	return fulfillment.release_stuck_oos_reservation(marketplace_order)


@frappe.whitelist(methods=["POST"])
def add_awb(marketplace_order: str, integration_shipment_nr: str, courier: str, awb_nr: str):
	_get_noon_order(marketplace_order)
	return fulfillment.add_awb(marketplace_order, integration_shipment_nr, courier, awb_nr)


@frappe.whitelist(methods=["POST"])
def mark_out_of_stock(marketplace_order: str, mp_item_nrs):
	_get_noon_order(marketplace_order)
	if isinstance(mp_item_nrs, str):
		mp_item_nrs = frappe.parse_json(mp_item_nrs)
	return fulfillment.mark_out_of_stock(marketplace_order, mp_item_nrs)


@frappe.whitelist(methods=["POST"])
def upload_invoice(
	marketplace_order: str, integration_shipment_nr: str, file_url: str, invoice_nr: str, issued_at: str
):
	_get_noon_order(marketplace_order)
	return fulfillment.upload_invoice(marketplace_order, integration_shipment_nr, file_url, invoice_nr, issued_at)


@frappe.whitelist(methods=["POST"])
def upload_order_invoice(marketplace_order: str, file_url: str, invoice_nr: str, issued_at: str):
	_get_noon_order(marketplace_order)
	return fulfillment.upload_order_invoice(marketplace_order, file_url, invoice_nr, issued_at)


@frappe.whitelist(methods=["POST"])
def retry_order_invoice(marketplace_order: str):
	_get_noon_order(marketplace_order)
	return fulfillment.retry_order_invoice(marketplace_order)


@frappe.whitelist(methods=["POST"])
def retry(marketplace_order: str, integration_shipment_nr: str):
	_get_noon_order(marketplace_order)
	return fulfillment.retry(marketplace_order, integration_shipment_nr)
