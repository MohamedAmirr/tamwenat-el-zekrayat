# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Desk boundary of the Sales Order form's "Create Shipment" button. It only authorizes and delegates to
``services/order_shipment.py`` (the same per-order service as the bulk list action); nothing is taken from the client
but the Sales Order name."""

import frappe

from marketplace_integrations.services import bulk_delivery_notes, order_shipment


@frappe.whitelist(methods=["POST"])
def create_shipment(sales_order: str):
	"""Make the draft Delivery Note of the whole order (or reuse its unsent draft) and start its Bosta shipment, in this
	request. Needs create and submit permission on Delivery Note and read on the Sales Order. Returns the result
	row of ``order_shipment`` (``status`` sent / skipped / failed, ``message``, ``delivery_note``): a refusal
	("skipped") created nothing; a "failed" row names the reason and either has no note (the draft this action made was
	deleted) or keeps the draft it reused. The form then watches the note with ``shipment_state``."""
	order_shipment.check_permissions()
	row = order_shipment.process_order(sales_order)
	return bulk_delivery_notes.hide_unreadable_note(row) if row.get("status") == order_shipment.SKIPPED else row


@frappe.whitelist()
def shipment_state(delivery_note: str):
	"""What the form's wait asks every few seconds: ``{"state", "status", "reason"}`` (``creating`` / ``accepted`` /
	``failed`` (the note is kept) / ``discarded`` (the draft note was deleted after Bosta refused it) / ``missing``).
	A note that exists needs read permission on it; a deleted one is answered only to the user who asked for it, and to
	anyone else as ``missing``, so a discarded name cannot be told from one that never existed."""
	if frappe.db.exists("Delivery Note", delivery_note):
		frappe.has_permission("Delivery Note", "read", doc=delivery_note, throw=True)
	outcome = order_shipment.shipment_outcome(delivery_note)
	if outcome["state"] == order_shipment.OUTCOME_DISCARDED and outcome["user"] != frappe.session.user:
		return {"state": order_shipment.OUTCOME_MISSING, "status": "", "reason": ""}
	return {key: outcome[key] for key in ("state", "status", "reason")}
