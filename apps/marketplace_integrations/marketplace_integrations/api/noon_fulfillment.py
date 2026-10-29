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
from frappe.utils import cstr

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
	return {
		**fulfillment.get_available_actions(doc),
		"shipments": _shipment_summaries(doc),
		"sales_invoice": _readable_document("Sales Invoice", doc.get("noon_sales_invoice")),
	}


_DOCSTATUS_STATES = {0: "Draft", 1: "Submitted", 2: "Cancelled"}


def _readable_document(doctype, name):
	"""``{"name", "state"}`` of an existing document the session user may read, else ``None``.  A
	document name is returned only to someone who can read it (#378); a deleted document is gone."""
	name = cstr(name).strip()
	if not name:
		return None
	docstatus = frappe.db.get_value(doctype, name, "docstatus")
	if docstatus is None or not frappe.has_permission(doctype, "read", doc=name):
		return None
	return {"name": name, "state": _DOCSTATUS_STATES.get(docstatus, "Draft")}


def _unit_summaries(doc, shipment_rows, payload, names):
	"""Units of one shipment grouped by (SKU, Noon status, on a live Delivery Note): ``{"item_name",
	"sku", "qty", "status", "on_note"}``.  ``status`` is Noon's ``integration_status`` from the
	latest payload (``""`` when the unit is not in it)."""
	grouped = {}
	for row in shipment_rows:
		sku = cstr(row.partner_sku).strip()
		status = cstr((payload.get(cstr(row.mp_item_nr).strip()) or {}).get("integration_status"))
		on_note = handover_state.is_covered(row)
		entry = grouped.setdefault(
			(sku, status, on_note),
			{"item_name": names.get(sku, ""), "sku": sku, "qty": 0, "status": status, "on_note": on_note},
		)
		entry["qty"] += 1
	return list(grouped.values())


def _shipment_summaries(doc):
	"""Display values of each shipment: status, units grouped by SKU (``Item name (SKU)`` x qty), AWBs,
	the Delivery Notes recorded on its unit rows (only those the user may read, each with its
	draft/submitted state), how many units Noon reports SHIPPED, the per-unit table of the Noon
	Fulfillment tab (#378) and the units that could not be put on a Delivery Note (as text)."""
	awbs = {}
	for awb in doc.get("noon_shipment_awbs") or []:
		awbs.setdefault(awb.integration_shipment_nr, []).append({"courier": awb.courier, "awb_nr": awb.awb_nr})
	units = doc.get("noon_shipment_units") or []
	payload = {
		cstr(item.get("mp_item_nr")).strip(): item for item in handover_state.payload_items(doc.get("latest_payload"))
	}
	names = fulfillment.item_names_by_sku(doc, [row.partner_sku for row in units])
	items = fulfillment.shipment_items(doc, names=names)
	summaries = []
	for row in doc.get("noon_shipments") or []:
		nr = row.integration_shipment_nr
		rows = handover_state.unit_rows(units, nr)
		notes = [
			note
			for note in (_readable_document("Delivery Note", name) for name in handover_state.shipment_notes(units, nr))
			if note
		]
		summaries.append(
			{
				"integration_shipment_nr": nr,
				"status": row.status,
				"items": items.get(nr, []),
				"units": _unit_summaries(doc, rows, payload, names),
				"unit_count": len(rows),
				"shipped_count": sum(
					1
					for unit in rows
					if (payload.get(cstr(unit.mp_item_nr).strip()) or {}).get("integration_status")
					== handover_state.STATUS_SHIPPED
				),
				"awbs": awbs.get(nr, []),
				"delivery_notes": notes,
				"delivery_note_error": row.get("delivery_note_error") or "",
				"handed_over": fulfillment._handed_over(doc, row),
				"can_mark_handed_over": fulfillment._can_mark_handed_over(doc, row),
				"unresolved": [handover_state.entry_text(entry) for entry in handover_state.load_entries(row)],
			}
		)
	return summaries


@frappe.whitelist(methods=["POST"])
def create_shipment(marketplace_order: str, mp_item_nrs, awb_source: str = "Noon", awb_nr: str | None = None):
	_get_noon_order(marketplace_order)
	if isinstance(mp_item_nrs, str):
		mp_item_nrs = frappe.parse_json(mp_item_nrs)
	return fulfillment.create_shipment(marketplace_order, mp_item_nrs, awb_source, awb_nr)


@frappe.whitelist(methods=["POST"])
def cancel_shipment(marketplace_order: str, integration_shipment_nr: str):
	_get_noon_order(marketplace_order)
	return fulfillment.cancel_shipment(marketplace_order, integration_shipment_nr)


@frappe.whitelist(methods=["POST"])
def get_cancel_on_noon_preview(marketplace_order: str):
	"""Read-only text source of the Cancel on Noon dialog (#376): the Created shipments that would be
	cancelled, those skipped for a live Delivery Note and why the Sales Order cancel is refused. No Noon call."""
	doc = frappe.get_doc("Marketplace Order", marketplace_order)
	doc.check_permission("write")
	if doc.platform != "Noon":
		frappe.throw(frappe._("Marketplace Order {0} is not a Noon order.").format(doc.name))
	return fulfillment.get_cancel_on_noon_preview(doc)


@frappe.whitelist(methods=["POST"])
def cancel_created_shipments(marketplace_order: str):
	"""Queue the Noon cancel of every Created shipment (#376). Units outside a shipment are untouched;
	the Sales Order is cancelled by the user afterwards, once the shipments show Cancelled."""
	_get_noon_order(marketplace_order)
	return fulfillment.cancel_created_shipments(marketplace_order)


@frappe.whitelist(methods=["POST"])
def mark_handed_over(marketplace_order: str, integration_shipment_nr: str):
	"""Manual "all shipped" override of a Created shipment: puts every uncovered unit on its Delivery
	Note and submits it (#368, #390).

	No Noon call. The user needs Delivery Note submit permission; the function checks it.
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
