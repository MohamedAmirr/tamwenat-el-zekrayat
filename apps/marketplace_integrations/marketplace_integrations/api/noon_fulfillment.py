# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Permission-checked Desk boundary for the Noon FBPI order-level actions (#146, decision 20):
Mark as Out of Stock (a Sales Order item row, #416: right is write on the Sales Order), the order invoice and
the stuck-reservation recovery.

Shipments are created, cancelled and refreshed from the Delivery Note
(``api/carrier_shipments.py``, #415), so no shipment endpoint lives here.

Thin by design, matching ``api/noon_orders.py``: every endpoint here checks
write permission on the Marketplace Order and that it is actually a Noon
order, then delegates to ``integrations/noon/fulfillment.py`` - which does
its own row-locking and enqueues the real Noon HTTP call on the ``long``
queue. No Noon HTTP call ever happens inside a Desk request.
"""

import frappe

from marketplace_integrations.integrations.noon import delivery_note_shipment, fulfillment


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
def get_available_actions(marketplace_order: str):
	"""Read-only convenience for the Sales Order form: which Noon Fulfillment buttons to show. No
	lock needed (nothing is written), so this loads directly and only needs read permission; every
	action still validates server side."""
	doc = frappe.get_doc("Marketplace Order", marketplace_order)
	doc.check_permission("read")
	if doc.platform != "Noon":
		frappe.throw(frappe._("Marketplace Order {0} is not a Noon order.").format(doc.name))
	return fulfillment.get_available_actions(doc)


@frappe.whitelist(methods=["POST"])
def release_stuck_oos_reservation(marketplace_order: str):
	_get_noon_order(marketplace_order)
	return fulfillment.release_stuck_oos_reservation(marketplace_order)


@frappe.whitelist(methods=["POST"])
def get_out_of_stock_rows(sales_order: str):
	"""Names of the Sales Order Item rows that offer Mark as Out of Stock. Right: write on the Sales Order
	(the order row is read as the system: the user needs no Marketplace Order permission)."""
	frappe.has_permission("Sales Order", "write", doc=sales_order, throw=True)
	return fulfillment.out_of_stock_rows(sales_order)


@frappe.whitelist(methods=["POST"])
def mark_item_out_of_stock(sales_order: str, sales_order_item: str):
	"""Mark as Out of Stock for one Sales Order Item row: authorizes, then delegates (the service refuses a
	row in a Delivery Note, a non-FBPI warehouse and anything not eligible). Queues the Noon call."""
	frappe.has_permission("Sales Order", "write", doc=sales_order, throw=True)
	return fulfillment.mark_row_out_of_stock(sales_order, sales_order_item)


@frappe.whitelist(methods=["POST"])
def check_mark_out_of_stock(sales_order: str, request_id: str):
	"""Poll of the wait dialog: ``{"state": pending|done|failed|unknown|local_failed, "message"}``."""
	frappe.has_permission("Sales Order", "write", doc=sales_order, throw=True)
	return fulfillment.out_of_stock_result(sales_order, request_id)


def _noon_order_of_sales_order(sales_order):
	"""The Marketplace Order of a submitted Noon Sales Order, after the right of the invoice actions: write on
	the Sales Order (the order row is then read and written as the system: no Marketplace Order right needed)."""
	frappe.has_permission("Sales Order", "write", doc=sales_order, throw=True)
	values = frappe.db.get_value("Sales Order", sales_order, ["docstatus", "custom_marketplace_order"], as_dict=True)
	if not values or values.docstatus != 1 or not values.custom_marketplace_order:
		frappe.throw(frappe._("Sales Order {0} is not a submitted Noon order.").format(sales_order))
	if frappe.db.get_value("Marketplace Order", values.custom_marketplace_order, "platform") != "Noon":
		frappe.throw(frappe._("Sales Order {0} is not a submitted Noon order.").format(sales_order))
	return values.custom_marketplace_order


@frappe.whitelist(methods=["POST"])
def get_order_invoice_state(sales_order: str):
	"""``{"can_upload", "can_retry", "uploaded"}`` for the order-invoice buttons (write on the Sales Order)."""
	marketplace_order = _noon_order_of_sales_order(sales_order)
	return fulfillment.order_invoice_state(frappe.get_doc("Marketplace Order", marketplace_order))


@frappe.whitelist(methods=["POST"])
def upload_order_invoice(sales_order: str, file_url: str, invoice_nr: str, issued_at: str):
	"""Upload Order Invoice from the Sales Order: authorizes, then delegates (the PDF is attached to the Sales
	Order). Queues the Noon call; returns ``{"queued", "idempotency_key"}``."""
	marketplace_order = _noon_order_of_sales_order(sales_order)
	return fulfillment.upload_order_invoice(
		marketplace_order, file_url, invoice_nr, issued_at, sales_order=sales_order
	)


@frappe.whitelist(methods=["POST"])
def retry_order_invoice(sales_order: str):
	marketplace_order = _noon_order_of_sales_order(sales_order)
	return fulfillment.retry_order_invoice(marketplace_order, sales_order=sales_order)


@frappe.whitelist(methods=["POST"])
def upload_delivery_note_invoice(
	delivery_note: str, file_url: str, invoice_nr: str, issued_at: str, idempotency_key: str | None = None
):
	"""Upload Shipment Invoice from a Delivery Note (right: write on that note). ``idempotency_key`` is given
	only by Retry (the key of the press that failed)."""
	frappe.has_permission("Delivery Note", "write", doc=delivery_note, throw=True)
	return delivery_note_shipment.upload_invoice_for_note(
		delivery_note, file_url, invoice_nr, issued_at, idempotency_key
	)


@frappe.whitelist(methods=["POST"])
def check_invoice_upload(document_type: str, document_name: str, idempotency_key: str):
	"""Poll of the invoice dialog: ``{"state": pending|done|failed, "message"}`` (write on the document the
	button was used on, a Sales Order or a Delivery Note)."""
	if document_type not in ("Sales Order", "Delivery Note"):
		frappe.throw(frappe._("Invalid document type."))
	frappe.has_permission(document_type, "write", doc=document_name, throw=True)
	return fulfillment.invoice_result(idempotency_key)
