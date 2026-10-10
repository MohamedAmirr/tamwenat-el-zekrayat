# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Clear the obsolete Sync Error on Shopify orders cancelled before any Sales Order existed.

Before the terminal skip in ``sales_order_sync.sync_sales_order()``, a Shopify order cancelled
with no linked Sales Order was saved as ``Sync Error`` ("Cancelled Shopify orders cannot be
synchronized.", or an earlier blocker such as an unpaid COD order) and stayed there. Nothing
retries such an order, so it would never clear by itself. This sets those rows back to
``Not Synced`` with the error cleared, in one statement; Sync Log rows and every other field
are untouched. Idempotent: a cleared row no longer matches the WHERE clause.
"""

import frappe


def execute():
	if not frappe.db.table_exists("Marketplace Order"):
		return
	frappe.db.sql(
		"""
		UPDATE `tabMarketplace Order`
		SET erpnext_sync_status = 'Not Synced', last_sync_error = NULL
		WHERE platform = 'Shopify'
			AND erpnext_sync_status = 'Sync Error'
			AND cancelled_at IS NOT NULL
			AND (erpnext_sales_order IS NULL OR erpnext_sales_order = '')
		"""
	)
