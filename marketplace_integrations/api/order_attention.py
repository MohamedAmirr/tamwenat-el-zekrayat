# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Desk boundary of the Sales Order form's "Mark as Resolved" button (#533). It only authorizes and delegates to
``services/order_attention.py``."""

import frappe
from frappe import _

from marketplace_integrations.services import order_attention


@frappe.whitelist(methods=["POST"])
def mark_resolved(sales_order: str):
	"""Clear the Needs Attention flag of a submitted or cancelled Sales Order and log the old reason on its timeline.
	Needs write permission on that order. Returns ``{"cleared": bool}``: False when the order was no longer flagged."""
	frappe.has_permission("Sales Order", "write", doc=sales_order, throw=True)
	if frappe.db.get_value("Sales Order", sales_order, "docstatus") not in (1, 2):
		frappe.throw(_("Only a submitted or cancelled Sales Order can be marked as resolved."))
	return {"cleared": order_attention.clear_attention(sales_order) is not None}
