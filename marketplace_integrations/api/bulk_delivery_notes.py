# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Desk boundary of the bulk "Delivery Note" action on the Sales Order list. It only authorizes, validates
the batch and delegates to ``services/bulk_delivery_notes.py``; amounts and eligibility are never taken from
the client."""

import frappe

from marketplace_integrations.services import bulk_delivery_notes


@frappe.whitelist(methods=["POST"])
def create_delivery_notes(sales_orders: str | list, shipping_method: str):
	"""Queue the bulk run for the given Sales Order names (at most ``MAX_ORDERS``). Needs create and submit
	permission on Delivery Note. Progress and the final summary arrive on the
	``bulk_delivery_note_progress`` realtime event (sent at every stage: creating, waiting, done)."""
	return bulk_delivery_notes.queue_bulk(sales_orders, shipping_method)
