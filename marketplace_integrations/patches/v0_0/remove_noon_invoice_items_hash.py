# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Remove the Delivery Note field ``custom_noon_invoice_items_hash`` (#416).

It stored a fingerprint of the note's rows at the shipment-invoice upload so Needs Attention could say
"the invoice may need re-upload" when the items changed later. That row was dropped as noise, so the field
and its fixture entry are gone; this patch removes what an upgraded site still holds: the Custom Field row,
any Property Setter for it and the database column. Idempotent: every step checks first.
"""

import frappe

DOCTYPE = "Delivery Note"
FIELDNAME = "custom_noon_invoice_items_hash"


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
