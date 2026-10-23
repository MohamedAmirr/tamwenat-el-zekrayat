# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.patches.v0_0.reconcile_bosta_handover_permissions import execute

DOCTYPE = "Bosta Shipment"


class IntegrationTestReconcileBostaHandoverPermissions(IntegrationTestCase):
	"""Keep the live site permission rows intact while exercising both patch states."""

	def setUp(self):
		self.snapshot = frappe.get_all(
			"Custom DocPerm", filters={"parent": DOCTYPE}, fields=["*"], order_by="idx asc"
		)
		frappe.db.delete("Custom DocPerm", {"parent": DOCTYPE})
		frappe.clear_cache(doctype=DOCTYPE)
		self.addCleanup(self.restore)

	def insert_row(self, role, **ptypes):
		doc = frappe.get_doc(
			{
				"doctype": "Custom DocPerm",
				"parent": DOCTYPE,
				"parenttype": "DocType",
				"parentfield": "permissions",
				"role": role,
				"permlevel": 0,
				**ptypes,
			}
		)
		doc.flags.ignore_links = True
		return doc.insert(ignore_permissions=True)

	def restore(self):
		frappe.db.delete("Custom DocPerm", {"parent": DOCTYPE})
		for row in self.snapshot:
			data = {key: value for key, value in row.items() if key not in ("name", "doctype")}
			doc = frappe.get_doc({"doctype": "Custom DocPerm", **data})
			doc.flags.ignore_links = True
			doc.insert(ignore_permissions=True)
		frappe.clear_cache(doctype=DOCTYPE)

	def test_standard_site_does_not_create_custom_docperms(self):
		self.assertFalse(frappe.db.exists("Custom DocPerm", {"parent": DOCTYPE}))

		execute()

		self.assertFalse(frappe.db.exists("Custom DocPerm", {"parent": DOCTYPE}))

	def test_existing_custom_docperm_gains_stock_manager_read(self):
		self.insert_row("Stock Manager", read=0)

		execute()

		row = frappe.get_all(
			"Custom DocPerm",
			filters={"parent": DOCTYPE, "role": "Stock Manager"},
			fields=["read"],
			limit=1,
		)[0]
		self.assertEqual(row.read, 1)
