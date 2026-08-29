# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

import frappe

from talabat_integration.talabat_integration.doctype.talabat_product_mapping.talabat_product_mapping import (
	compute_item_vendor_key,
)


def execute():
	"""Backfill item_vendor_key for rows created before issue #19 added it.

	Without this, a pre-existing row's item_vendor_key stays NULL - MariaDB's
	unique index allows multiple NULLs, so the secondary (item, vendor)
	uniqueness check silently doesn't cover any un-backfilled row. Idempotent:
	only touches rows where the key is still unset.
	"""
	if not frappe.db.has_column("Talabat Product Mapping", "item_vendor_key"):
		return

	# SQL's IN/= can't match NULL, so filter in Python instead of relying on
	# a filters= clause here - both NULL and "" need to be caught.
	rows = frappe.get_all(
		"Talabat Product Mapping",
		fields=["name", "item", "vendor", "item_vendor_key"],
	)

	for row in rows:
		if row.item_vendor_key:
			continue
		frappe.db.set_value(
			"Talabat Product Mapping",
			row.name,
			"item_vendor_key",
			compute_item_vendor_key(row.item, row.vendor),
			update_modified=False,
		)
