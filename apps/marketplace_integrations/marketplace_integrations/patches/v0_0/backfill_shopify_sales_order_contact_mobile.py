# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Backfill Sales Order.contact_mobile from the Shopify buyer phone (#277).

Only Shopify-linked Sales Orders whose contact_mobile is empty are filled; an order that
already has a phone is left alone.  Written with ``db.set_value`` and ``update_modified=False``
so submitted orders are filled without a validation pass or a changed ``modified``.  The
snapshot hash is untouched, so no order becomes "Modified After Sync".  Idempotent: a filled
order no longer matches.
"""

import frappe
from frappe.utils import cstr


def execute():
	if not frappe.db.table_exists("Marketplace Order"):
		return
	orders = frappe.get_all(
		"Marketplace Order",
		filters={
			"platform": "Shopify",
			"erpnext_sales_order": ["is", "set"],
			"customer_phone_number": ["is", "set"],
		},
		fields=["erpnext_sales_order", "customer_phone_number"],
	)
	for order in orders:
		phone = cstr(order.customer_phone_number).strip()
		if not phone:
			continue
		current = frappe.db.get_value("Sales Order", order.erpnext_sales_order, "contact_mobile")
		if current is None and not frappe.db.exists("Sales Order", order.erpnext_sales_order):
			continue
		if cstr(current).strip():
			continue
		frappe.db.set_value(
			"Sales Order", order.erpnext_sales_order, "contact_mobile", phone, update_modified=False
		)
