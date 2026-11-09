# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Marketplace Order list: fill the numeric ``order_number`` (sort key) from
``order_code`` for orders saved before the field existed. Only pure ``#<digits>``
codes get a number; ``update_modified=False`` so no ``modified`` bump.

Idempotent: only rows whose stored number differs from the computed one are written.
"""

import frappe

from marketplace_integrations.marketplace_integrations.doctype.marketplace_order.marketplace_order import (
	compute_order_number,
)


def execute():
	if not frappe.db.table_exists("Marketplace Order") or not frappe.db.has_column(
		"Marketplace Order", "order_number"
	):
		return

	rows = frappe.get_all(
		"Marketplace Order",
		filters={"order_code": ["is", "set"]},
		fields=["name", "order_code", "order_number"],
		limit_page_length=0,
	)
	for row in rows:
		number = compute_order_number(row.order_code)
		if number is not None and number != row.order_number:
			frappe.db.set_value("Marketplace Order", row.name, "order_number", number, update_modified=False)
	frappe.clear_cache(doctype="Marketplace Order")
