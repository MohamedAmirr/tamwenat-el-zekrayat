# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Desk boundary for the carrier shipment of a Delivery Note (epic #413, #415). Each endpoint only
authorizes, validates and delegates to the neutral service ``services/carrier_shipments.py``, which calls the
adapter of the note's carrier."""

import frappe
from frappe import _

from marketplace_integrations.services import carrier_log, carrier_shipments


def _carrier_note(delivery_note, ptype):
	"""The Delivery Note and its adapter, after the caller's own permission check and a check that a carrier
	owns the note."""
	doc = frappe.get_doc("Delivery Note", delivery_note)
	frappe.has_permission("Delivery Note", ptype, doc=doc, throw=True)
	adapter = carrier_shipments.adapter_for(doc)
	if not adapter:
		frappe.throw(_("This Delivery Note has no carrier shipment."))
	return doc, adapter


@frappe.whitelist(methods=["POST"])
def request_shipment(delivery_note: str, modified: str | None = None):
	"""Start the carrier create of a draft Delivery Note (needs submit permission on it)."""
	_carrier_note(delivery_note, "submit")
	return carrier_shipments.request_create(delivery_note, modified)


@frappe.whitelist(methods=["POST"])
def check_shipment(delivery_note: str):
	"""Look the note's shipment up at the carrier (never creates; needs write permission)."""
	_carrier_note(delivery_note, "write")
	return carrier_shipments.check(delivery_note)


@frappe.whitelist(methods=["POST"])
def refresh_shipment(delivery_note: str):
	"""Read the note's shipment state from the carrier and apply it (needs write permission)."""
	doc, adapter = _carrier_note(delivery_note, "write")
	if carrier_shipments.status_of(doc) not in (
		carrier_log.CREATED,
		carrier_log.CANCELLATION_PENDING,
		carrier_log.CANCELLATION_UNKNOWN,
		carrier_log.CANCELLED,
	):
		frappe.throw(_("Only a note with a created {0} shipment can be refreshed.").format(adapter.SERVICE_NAME))
	try:
		return {"shipping_status": adapter.refresh(delivery_note)}
	except carrier_log.RateLimited:
		frappe.throw(_("{0} is busy. Try again in a few minutes.").format(adapter.SERVICE_NAME))


@frappe.whitelist(methods=["POST"])
def cancel_shipment(delivery_note: str):
	"""Terminate the note's shipment at the carrier (needs cancel permission on a submitted note, write on a
	draft). Never cancels or deletes the Delivery Note itself: the native cancel or delete does, once the
	carrier agreed."""
	doc = frappe.get_doc("Delivery Note", delivery_note)
	frappe.has_permission("Delivery Note", "cancel" if doc.docstatus == 1 else "write", doc=doc, throw=True)
	if not carrier_shipments.adapter_for(doc):
		frappe.throw(_("This Delivery Note has no carrier shipment."))
	return carrier_shipments.cancel(delivery_note)
