# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Historical backfill for the legacy active_sales_order field.

The reservation model no longer uses this column; the following patch removes its
unique index while retaining existing values for audit history. Idempotent.
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
