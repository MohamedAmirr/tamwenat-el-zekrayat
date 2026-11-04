# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Move Marketplace Account ``shopify_delivery_lead_days`` into the generic ``delivery_lead_days``.

The field left the doctype JSON, so the migrate's schema sync no longer knows it but leaves its column in
place. This post-model-sync patch reads that column with SQL, copies the value into ``delivery_lead_days``
for every account that has none yet, then drops the old column and any orphaned Custom Field / Property
Setter row of the old field. It does nothing when the new column is missing (the old column is never dropped
uncopied). Idempotent: every step checks first, and it never overwrites a ``delivery_lead_days`` already set.
"""

import frappe

DOCTYPE = "Marketplace Account"
OLD = "shopify_delivery_lead_days"
NEW = "delivery_lead_days"


def execute():
	if not frappe.db.has_column(DOCTYPE, NEW):
		# The new column is not there (schema sync did not run): never drop the old one uncopied.
		return
	if frappe.db.has_column(DOCTYPE, OLD):
		# The column names are the constants above, never user input.
		rows = frappe.db.sql(f"SELECT `name`, `{OLD}` FROM `tab{DOCTYPE}` WHERE `{OLD}` > 0", as_dict=True)
		for row in rows:
			if not frappe.db.get_value(DOCTYPE, row.name, NEW):
				frappe.db.set_value(DOCTYPE, row.name, NEW, row[OLD], update_modified=False)
		frappe.db.sql_ddl(f"ALTER TABLE `tab{DOCTYPE}` DROP COLUMN `{OLD}`")
	# Orphaned customization rows for the old field (idempotent: each step checks first).
	name = f"{DOCTYPE}-{OLD}"
	if frappe.db.exists("Custom Field", name):
		frappe.db.delete("Custom Field", {"name": name})
	if frappe.db.exists("Property Setter", {"doc_type": DOCTYPE, "field_name": OLD}):
		frappe.db.delete("Property Setter", {"doc_type": DOCTYPE, "field_name": OLD})
	frappe.clear_cache(doctype=DOCTYPE)
