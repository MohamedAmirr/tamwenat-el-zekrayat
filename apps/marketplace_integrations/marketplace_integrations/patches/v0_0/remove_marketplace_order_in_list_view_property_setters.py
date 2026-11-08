# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""The Marketplace Order list columns are defined in the doctype JSON (#324).
Customize Form Property Setters for ``in_list_view`` overrode that layout on some
sites. Delete them (only those) so the JSON layout applies.

Idempotent: a second run finds nothing to delete.
"""

import frappe


def execute():
	filters = {"doc_type": "Marketplace Order", "property": "in_list_view"}
	for setter in frappe.get_all("Property Setter", filters=filters, fields=["name", "field_name", "value"]):
		print(
			f"Removing Property Setter {setter.name}: Marketplace Order.{setter.field_name} "
			f"in_list_view={setter.value}"
		)
	frappe.db.delete("Property Setter", filters)
	frappe.clear_cache(doctype="Marketplace Order")
