# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Drop the Marketplace Account column ``noon_fbpi_code`` (#416).

The user typed the FBPI code on each Noon account. The code is now the constant
``catalog.FBPI_SYSTEM_CODE``, so the field is gone from the doctype; this patch removes what an upgraded
site still holds: any Custom Field / Property Setter row and the database column. Nothing else reads the
value. Idempotent: every step checks first.
"""

import frappe

DOCTYPE = "Marketplace Account"
FIELDNAME = "noon_fbpi_code"


def execute():
	changed = False
	name = f"{DOCTYPE}-{FIELDNAME}"
	if frappe.db.exists("Custom Field", name):
		frappe.db.delete("Custom Field", {"name": name})
		changed = True
	if frappe.db.exists("Property Setter", {"doc_type": DOCTYPE, "field_name": FIELDNAME}):
		frappe.db.delete("Property Setter", {"doc_type": DOCTYPE, "field_name": FIELDNAME})
		changed = True
	if frappe.db.has_column(DOCTYPE, FIELDNAME):
		# The names are the constants above, never user input, so the identifier is safe to interpolate.
		frappe.db.sql_ddl(f"ALTER TABLE `tab{DOCTYPE}` DROP COLUMN `{FIELDNAME}`")
		changed = True
	if changed:
		frappe.clear_cache(doctype=DOCTYPE)
