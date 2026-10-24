# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""#324: backfill ``Marketplace Sync Log.platform`` (read-only
``fetch_from: marketplace_account.platform``) for logs written before the field
existed. One bulk ``UPDATE ... JOIN`` (no ``doc.save()``, no ``modified`` bump).

Only rows that have an account and an empty ``platform`` are filled; rows
without an account stay empty. Idempotent: filled rows are excluded by the WHERE.
"""

import frappe


def execute():
	if not frappe.db.table_exists("Marketplace Sync Log"):
		return
	if not frappe.db.has_column("Marketplace Sync Log", "platform"):
		return

	frappe.db.sql(
		"""
		UPDATE `tabMarketplace Sync Log` AS msl
		INNER JOIN `tabMarketplace Account` AS ma ON ma.name = msl.marketplace_account
		SET msl.platform = ma.platform
		WHERE (msl.platform IS NULL OR msl.platform = '')
			AND ma.platform IS NOT NULL
			AND ma.platform != ''
		"""
	)
	frappe.clear_cache(doctype="Marketplace Sync Log")
