# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""#324: Marketplace Order now uses ``platform_order_reference`` as its title
(Links and the list show it). The controller fills it from ``external_order_id``
when a mapper leaves it empty; this patch does the same for orders saved before
that change, in one bulk UPDATE (no ``doc.save()``, no ``modified`` bump).

Idempotent: only rows with an empty reference are touched, and they are no
longer empty afterwards.
"""

import frappe


def execute():
	if not frappe.db.table_exists("Marketplace Order"):
		return

	frappe.db.sql(
		"""
		UPDATE `tabMarketplace Order`
		SET platform_order_reference = external_order_id
		WHERE (platform_order_reference IS NULL OR platform_order_reference = '')
			AND external_order_id IS NOT NULL
			AND external_order_id != ''
		"""
	)
	# Raw SQL bypasses Document and its cache (docs/frappe-conventions.md).
	frappe.clear_cache(doctype="Marketplace Order")
