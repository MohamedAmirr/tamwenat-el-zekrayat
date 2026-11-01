# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Retire the old Noon Delivery Note fields ``custom_carrier_status`` and ``custom_carrier_error`` (#415).

The neutral carrier contract keeps the shipment state in ``custom_integration_status`` /
``custom_shipping_status`` / ``custom_last_integration_error``; the two old fields (Pending / Created /
Error / Cancelled and its error text) belonged to the removed Marketplace Order shipment flow and carry
nothing to keep. Their Custom Field rows and Property Setters are no longer in the fixtures, so nothing
re-creates them; this patch removes what an upgraded site still holds: the Custom Field rows, any Property
Setter for them (the field order is rewritten by the fixture sync that follows) and the database columns.

It does NOT drop ``tabNoon AWB`` or the ``Marketplace Order Shipment`` child tables: that is a manual step
after verification (``docs/user-actions.md``). Idempotent: every step checks first.
"""

import frappe

DOCTYPE = "Delivery Note"
FIELDNAMES = ("custom_carrier_status", "custom_carrier_error")


def execute():
	changed = False
	for fieldname in FIELDNAMES:
		name = f"{DOCTYPE}-{fieldname}"
		if frappe.db.exists("Custom Field", name):
			frappe.db.delete("Custom Field", {"name": name})
			changed = True
		if frappe.db.exists("Property Setter", {"doc_type": DOCTYPE, "field_name": fieldname}):
			frappe.db.delete("Property Setter", {"doc_type": DOCTYPE, "field_name": fieldname})
			changed = True
		if frappe.db.has_column(DOCTYPE, fieldname):
			# The names are the constants above, never user input, so the identifier is safe to interpolate.
			frappe.db.sql_ddl(f"ALTER TABLE `tab{DOCTYPE}` DROP COLUMN `{fieldname}`")
			changed = True
	if changed:
		frappe.clear_cache(doctype=DOCTYPE)
