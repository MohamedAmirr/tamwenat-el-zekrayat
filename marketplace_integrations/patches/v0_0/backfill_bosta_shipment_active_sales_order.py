# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Fill Bosta Shipment.active_sales_order for rows created before the field existed.

``sales_order`` used to be globally unique; the one-active-shipment-per-Sales-Order guard is now
the unique ``active_sales_order`` column (NULL once a shipment is Canceled). Idempotent.
"""

import frappe


def execute():
	if not frappe.db.table_exists("Bosta Shipment"):
		return
	frappe.db.sql(
		"""
		UPDATE `tabBosta Shipment`
		SET active_sales_order = sales_order
		WHERE status != 'Canceled' AND (active_sales_order IS NULL OR active_sales_order = '')
		"""
	)
