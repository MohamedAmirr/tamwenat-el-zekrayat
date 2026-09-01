# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Authorized API boundary for explicit ERPNext Sales Order synchronization."""

import frappe

from marketplace_integrations.services.sales_order_sync import (
	get_current_sync_status,
	sync_sales_order,
)


@frappe.whitelist(methods=["POST"])
def sync_to_erpnext(marketplace_order: str):
	"""Explicitly create or update this order's linked ERPNext Sales Order."""
	doc = frappe.get_doc("Marketplace Order", marketplace_order)
	doc.check_permission("write")
	return sync_sales_order(doc.name)


@frappe.whitelist()
def get_erpnext_sync_status(marketplace_order: str):
	"""Refresh the server-side snapshot state shown by the Marketplace Order form."""
	doc = frappe.get_doc("Marketplace Order", marketplace_order)
	doc.check_permission("write")
	return {"status": get_current_sync_status(doc.name)}
