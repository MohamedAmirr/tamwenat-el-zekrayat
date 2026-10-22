# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Read-only summary of a Sales Order for the list hover card (#325)."""

import frappe
from frappe.utils import cstr, flt

from marketplace_integrations.integrations.bosta.deliveries import get_sales_order_shipment

MAX_ITEMS = 10


@frappe.whitelist()
def get_sales_order_card(sales_order: str):
	"""Everything the Sales Order list hover card shows, for one order.

	The caller must be able to read this Sales Order. The Marketplace Order, Sync Log and
	Bosta Shipment parts are included only when the caller can read those doctypes; without
	that permission the key is None and nothing from it is returned. All of it is on the
	Sales Order form and the linked documents too: the card never holds the only copy.
	"""
	frappe.has_permission("Sales Order", "read", doc=sales_order, throw=True)

	order = frappe.db.get_value(
		"Sales Order",
		sales_order,
		["customer", "customer_name", "contact_mobile", "status", "currency", "grand_total", "rounded_total"],
		as_dict=True,
	)
	# One extra row tells whether the list was cut, without a second count query.
	rows = frappe.get_list(
		"Sales Order Item",
		parent_doctype="Sales Order",
		filters={"parent": sales_order},
		fields=["item_name", "qty", "uom"],
		order_by="idx asc",
		limit_page_length=MAX_ITEMS + 1,
	)
	return {
		"sales_order": sales_order,
		"customer": order.customer_name or order.customer,
		"phone": order.contact_mobile or "",
		"status": order.status,
		"currency": order.currency,
		"total": flt(order.rounded_total or order.grand_total),
		"items": [
			{"item_name": r.item_name, "qty": flt(r.qty), "uom": r.uom or ""} for r in rows[:MAX_ITEMS]
		],
		"more_items": len(rows) > MAX_ITEMS,
		"marketplace": _marketplace_part(sales_order),
		"bosta": _bosta_part(sales_order),
	}


def _marketplace_part(sales_order):
	"""The linked Marketplace Order's readable summary, or None (no order, or not readable)."""
	if not frappe.has_permission("Marketplace Order", "read"):
		return None
	rows = frappe.get_list(
		"Marketplace Order",
		filters={"erpnext_sales_order": sales_order},
		fields=[
			"name",
			"platform",
			"platform_order_reference",
			"external_order_id",
			"external_financial_status",
			"payment_type",
			"erpnext_sync_status",
			"last_sync_error",
		],
		order_by="creation desc, name desc",
		limit_page_length=1,
	)
	if not rows:
		return None
	row = rows[0]
	return {
		# Never the MKTORD- name: staff know the platform's own number.
		"reference": row.platform_order_reference or row.external_order_id or "",
		"platform": row.platform or "",
		"payment_status": row.external_financial_status or "",
		"payment_type": row.payment_type or "",
		"sync_status": row.erpnext_sync_status or "",
		"last_sync_error": cstr(row.last_sync_error),
		"last_sync": _last_sync(row.name),
	}


def _last_sync(marketplace_order):
	"""Status of the latest Sync Log of the order, only when the caller can read Sync Logs."""
	if not frappe.has_permission("Marketplace Sync Log", "read"):
		return None
	rows = frappe.get_list(
		"Marketplace Sync Log",
		filters={"marketplace_order": marketplace_order},
		fields=["sync_type", "status", "attempted_at"],
		order_by="attempted_at desc, creation desc",
		limit_page_length=1,
	)
	if not rows:
		return None
	return {
		"sync_type": rows[0].sync_type,
		"status": rows[0].status,
		"attempted_at": cstr(rows[0].attempted_at) if rows[0].attempted_at else "",
	}


def _bosta_part(sales_order):
	"""Bosta status and tracking number, or None. Reuses the form's permission-aware summary."""
	shipment = get_sales_order_shipment(sales_order)
	if not shipment:
		return None
	return {
		"status": shipment["bosta_state"] or shipment["status"],
		"tracking_number": shipment["tracking_number"],
		"active": shipment["active"],
	}
