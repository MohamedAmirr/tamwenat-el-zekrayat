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

from marketplace_integrations.integrations.noon import fulfillment


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
def create_shipment(
	marketplace_order: str, mp_item_nrs, awb_source: str, courier: str | None = None, awb_nr: str | None = None
):
	_get_noon_order(marketplace_order)
	if isinstance(mp_item_nrs, str):
		mp_item_nrs = frappe.parse_json(mp_item_nrs)
	return fulfillment.create_shipment(marketplace_order, mp_item_nrs, awb_source, courier, awb_nr)


@frappe.whitelist(methods=["POST"])
def cancel_shipment(marketplace_order: str, integration_shipment_nr: str):
	_get_noon_order(marketplace_order)
	return fulfillment.cancel_shipment(marketplace_order, integration_shipment_nr)


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
def retry(marketplace_order: str, integration_shipment_nr: str):
	_get_noon_order(marketplace_order)
	return fulfillment.retry(marketplace_order, integration_shipment_nr)
