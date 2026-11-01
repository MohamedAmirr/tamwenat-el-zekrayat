# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Desk boundary for the Bosta shipment of a Delivery Note (epic #413). Each endpoint only authorizes,
validates and delegates to ``integrations/bosta/delivery_note_shipment.py``."""

import frappe
from frappe import _

from marketplace_integrations.integrations.bosta import delivery_note_shipment as shipment
from marketplace_integrations.integrations.bosta import editing, locations, statuses


def _bosta_note(delivery_note, ptype):
	"""The Delivery Note, after the caller's own permission check and a check that it is a Bosta note."""
	doc = frappe.get_doc("Delivery Note", delivery_note)
	frappe.has_permission("Delivery Note", ptype, doc=doc, throw=True)
	if not shipment.applies_to(doc):
		frappe.throw(_("This Delivery Note is not a Bosta shipment."))
	return doc


@frappe.whitelist(methods=["POST"])
def request_shipment(delivery_note: str, modified: str | None = None):
	"""Start the Bosta create of a draft Delivery Note (needs submit permission on it)."""
	_bosta_note(delivery_note, "submit")
	return shipment.request_create(delivery_note, modified)


@frappe.whitelist(methods=["POST"])
def check_with_bosta(delivery_note: str):
	"""Look the note's delivery up at Bosta (never creates; needs write permission)."""
	_bosta_note(delivery_note, "write")
	return shipment.check_with_bosta(delivery_note)


@frappe.whitelist(methods=["POST"])
def refresh_from_bosta(delivery_note: str):
	"""Read the note's delivery state from Bosta and apply it (needs write permission)."""
	doc = _bosta_note(delivery_note, "write")
	if shipment.status_of(doc) not in (shipment.CREATED, "Cancellation Pending", "Cancellation Unknown", shipment.CANCELLED):
		frappe.throw(_("Only a note with a created Bosta shipment can be refreshed."))
	try:
		return {"shipping_status": statuses.refresh_note(delivery_note)}
	except statuses.RateLimited:
		frappe.throw(_("Bosta is busy. Try again in a few minutes."))


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
def cancel_at_bosta(delivery_note: str):
	"""Terminate the note's Bosta delivery (needs cancel permission on a submitted note, write on a draft).
	Never cancels or deletes the Delivery Note itself: the native cancel or delete does, once Bosta agreed."""
	doc = frappe.get_doc("Delivery Note", delivery_note)
	frappe.has_permission("Delivery Note", "cancel" if doc.docstatus == 1 else "write", doc=doc, throw=True)
	if not shipment.applies_to(doc):
		frappe.throw(_("This Delivery Note is not a Bosta shipment."))
	return shipment.cancel_at_bosta(delivery_note)


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
