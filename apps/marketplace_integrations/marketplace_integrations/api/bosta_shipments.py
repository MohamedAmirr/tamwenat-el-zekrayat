# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Desk boundary for the Bosta-only parts of a Bosta Delivery Note (epic #413): label, cities, COD hint and
edit. Request, check, refresh and cancel are neutral (``api/carrier_shipments.py``). Each endpoint only
authorizes, validates and delegates to ``integrations/bosta/``."""

import frappe
from frappe import _
from frappe.utils import flt

from marketplace_integrations.integrations.bosta import delivery_note_shipment as shipment
from marketplace_integrations.integrations.bosta import editing, locations


def _bosta_note(delivery_note, ptype):
	"""The Delivery Note, after the caller's own permission check and a check that it is a Bosta note."""
	doc = frappe.get_doc("Delivery Note", delivery_note)
	frappe.has_permission("Delivery Note", ptype, doc=doc, throw=True)
	if not shipment.applies_to(doc):
		frappe.throw(_("This Delivery Note is not a Bosta shipment."))
	return doc


MAX_FORM_ROWS = 200


@frappe.whitelist(methods=["POST"])
def fetch_label(delivery_note: str):
	"""Fetch and attach the Bosta label of a Created note (needs write permission)."""
	doc = _bosta_note(delivery_note, "write")
	if shipment.status_of(doc) != shipment.CREATED:
		frappe.throw(_("The label can be fetched once the Bosta shipment is Created."))
	return shipment.fetch_label(delivery_note, raise_on_error=True)


@frappe.whitelist(methods=["POST"])
def get_cities(delivery_note: str):
	"""Bosta's cities (cached 24h) and the city matching the note's address."""
	_bosta_note(delivery_note, "write")
	return shipment.get_city_options(delivery_note)


@frappe.whitelist(methods=["POST"])
def resolve_city(city_id: str):
	"""The city code Bosta is sent and the name shown, for a chosen city id."""
	frappe.has_permission("Delivery Note", "write", throw=True)
	city = locations.get_city(city_id)
	return {"code": city["code"], "name": city["name"]}


@frappe.whitelist(methods=["POST"])
def get_cod_hint(delivery_note: str):
	"""The computed COD when it differs from the merchant's confirmed amount (a hint, never a write)."""
	_bosta_note(delivery_note, "read")
	return shipment.cod_hint(delivery_note)


@frappe.whitelist(methods=["POST"])
def get_default_cod(doc):
	"""The COD the server rule would set for a new, unsaved Delivery Note form (a live prefill, never a write).
	Only the grand total and the Sales Order name of each row are read from the form (at most
	``MAX_FORM_ROWS`` rows); the rule stays the one in ``default_cod``. The transient document is never
	saved. The Sales Order is read, and so permission-checked, only when exactly one is referenced:
	``default_cod`` ignores the order when there are several."""
	frappe.has_permission("Delivery Note", "read", throw=True)
	invalid = _("The Delivery Note data is not valid.")
	values = frappe.parse_json(doc) if isinstance(doc, str) else doc
	if not isinstance(values, dict):
		frappe.throw(invalid)
	rows = values.get("items") or []
	if not isinstance(rows, list):
		frappe.throw(invalid)
	total = values.get("grand_total")
	if total in (None, ""):
		total = 0
	if isinstance(total, bool) or not isinstance(total, (int, float, str)):
		frappe.throw(invalid)
	try:
		total = float(total)
	except ValueError:
		frappe.throw(invalid)
	if total != total or total in (float("inf"), float("-inf")):
		frappe.throw(invalid)
	orders = []
	for row in rows[:MAX_FORM_ROWS]:
		if not isinstance(row, dict):
			frappe.throw(invalid)
		order = row.get("against_sales_order")
		if order in (None, ""):
			continue
		if not isinstance(order, str):
			frappe.throw(invalid)
		if order not in orders:
			orders.append(order)
	if len(orders) == 1:
		# a missing order answers like a forbidden one: existence is not revealed
		if not frappe.db.exists("Sales Order", orders[0]) or not frappe.has_permission(
			"Sales Order", "read", doc=orders[0]
		):
			frappe.throw(_("Not permitted"), frappe.PermissionError)
	draft = frappe.get_doc(
		{
			"doctype": "Delivery Note",
			"grand_total": flt(total),
			"items": [{"doctype": "Delivery Note Item", "against_sales_order": order} for order in orders],
		}
	)
	return shipment.default_cod(draft)


@frappe.whitelist(methods=["POST"])
def get_edit_values(delivery_note: str):
	"""The phone, city and first address line Bosta holds for the note (the edit dialog's start values)."""
	_bosta_note(delivery_note, "write")
	return editing.get_edit_values(delivery_note)


@frappe.whitelist(methods=["POST"])
def edit_shipment(delivery_note: str, receiver_phone: str, city_id: str, address_first_line: str):
	"""Send a changed receiver phone and drop-off address to Bosta (needs write permission on the note)."""
	_bosta_note(delivery_note, "write")
	return editing.edit_shipment(delivery_note, receiver_phone, city_id, address_first_line)
