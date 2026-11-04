# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Create the Suppliers "inDrive", "Uber" and "Own driver" used as the Delivery Note ``transporter`` of a
manual courier. Idempotent.

A manual courier has no adapter in ``services/carrier_shipments.CARRIERS``: a Delivery Note with one of
these transporters is a plain ERPNext Delivery Note and no carrier API is called.

Each Supplier is found by ``supplier_name`` (its document name depends on Buying Settings
``supp_master_name``). An existing one is left untouched: if it is not flagged ``is_transporter``
that is only reported, never changed silently.
"""

import frappe

SUPPLIER_NAMES = ("inDrive", "Uber", "Own driver")
SUPPLIER_GROUP = "All Supplier Groups"


def _supplier_group():
	"""``All Supplier Groups`` when the site has it (the setup wizard creates it, translated), else the Buying
	Settings default group, else none: the field is not mandatory and a missing group must not stop migrate."""
	if frappe.db.exists("Supplier Group", SUPPLIER_GROUP):
		return SUPPLIER_GROUP
	return frappe.db.get_single_value("Buying Settings", "supplier_group") or None


def execute():
	supplier_group = _supplier_group()
	for supplier_name in SUPPLIER_NAMES:
		existing = frappe.db.get_value(
			"Supplier", {"supplier_name": supplier_name}, ["name", "is_transporter"], as_dict=True
		)
		if existing:
			if not existing.is_transporter:
				message = (
					f"Supplier {existing.name} ({supplier_name}) already exists and is not marked as a "
					"transporter. It was left unchanged; mark it Is Transporter so it shows in the "
					"Delivery Note transporter field."
				)
				print(f"create_manual_courier_suppliers: {message}")
				frappe.log_error(title=f"{supplier_name} transporter Supplier is not a transporter", message=message)
			continue
		frappe.get_doc(
			{
				"doctype": "Supplier",
				"supplier_name": supplier_name,
				"supplier_group": supplier_group,
				"supplier_type": "Company",
				"is_transporter": 1,
			}
		).insert(ignore_permissions=True)
