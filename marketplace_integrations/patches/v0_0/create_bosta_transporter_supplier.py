# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Create the Supplier "Bosta" used as the Delivery Note ``transporter`` (epic #413). Idempotent.

The Supplier is found by ``supplier_name`` (its document name depends on Buying Settings
``supp_master_name``). An existing one is left untouched: if it is not flagged ``is_transporter``
that is only reported, never changed silently.
"""

import frappe

SUPPLIER_NAME = "Bosta"
SUPPLIER_GROUP = "All Supplier Groups"


def execute():
	existing = frappe.db.get_value(
		"Supplier", {"supplier_name": SUPPLIER_NAME}, ["name", "is_transporter"], as_dict=True
	)
	if existing:
		if not existing.is_transporter:
			message = (
				f"Supplier {existing.name} ({SUPPLIER_NAME}) already exists and is not marked as a "
				"transporter. It was left unchanged; mark it Is Transporter so it shows in the "
				"Delivery Note transporter field."
			)
			print(f"create_bosta_transporter_supplier: {message}")
			frappe.log_error(title="Bosta transporter Supplier is not a transporter", message=message)
		return
	frappe.get_doc(
		{
			"doctype": "Supplier",
			"supplier_name": SUPPLIER_NAME,
			"supplier_group": SUPPLIER_GROUP,
			"supplier_type": "Company",
			"is_transporter": 1,
		}
	).insert(ignore_permissions=True)
