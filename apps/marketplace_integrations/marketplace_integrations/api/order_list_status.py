# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Endpoint of the Sales Order list Status and Payment columns: one call per list page."""

import frappe

from marketplace_integrations.services import order_list_status


@frappe.whitelist()
def get_order_states(names):
	"""Delivery stage and payment of the given Sales Orders (``names``: a JSON list or a list). Only orders the
	caller may read are answered; the work is in ``services/order_list_status.py``."""
	frappe.has_permission("Sales Order", "read", throw=True)
	names = frappe.parse_json(names) if isinstance(names, str) else names
	if not isinstance(names, list):
		frappe.throw(frappe._("Send a list of Sales Order names."))
	return order_list_status.get_states(names)
