# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Authorized, read-only live mapping data for the Marketplace Order form."""

import frappe
from frappe import _
from frappe.utils import cstr

from marketplace_integrations.services.mappings import resolve_item


@frappe.whitelist()
def get_item_presentation(marketplace_order: str):
	"""Return current exact mapping status for every stored Talabat order line.

	ERPNext reference pricing deliberately remains unavailable until a price
	source is explicitly configured in a later issue; this endpoint never
	guesses an Item Price or falls back to Item.standard_rate.
	"""
	order = frappe.get_doc("Marketplace Order", marketplace_order)
	order.check_permission("read")
	if not frappe.has_permission("Marketplace Product Mapping", ptype="read"):
		frappe.throw(_("Not permitted"), frappe.PermissionError)

	return {
		"price_comparison_available": False,
		"items": [
			{
				"external_item_id": row.external_item_id,
				"external_sku": row.external_sku,
				"mapped_item": resolve_item(order.marketplace_account, row.external_sku),
			}
			for row in order.items or []
		],
	}
