# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.patches.v0_0 import create_manual_courier_suppliers as courier_patch


class IntegrationTestCreateManualCourierSuppliers(IntegrationTestCase):
	def setUp(self):
		# Start from a site without the Suppliers (a rollback in tearDown restores whatever was there).
		for name in frappe.get_all("Supplier", filters={"supplier_name": ["in", list(courier_patch.SUPPLIER_NAMES)]}, pluck="name"):
			frappe.delete_doc("Supplier", name, force=True, ignore_permissions=True)

	def tearDown(self):
		frappe.db.rollback()

	def _suppliers(self, name):
		return frappe.get_all("Supplier", filters={"supplier_name": name}, fields=["name", "is_transporter"])

	def test_each_supplier_exists_once_as_a_transporter_after_any_number_of_runs(self):
		courier_patch.execute()
		courier_patch.execute()
		for name in ("inDrive", "Uber", "Own driver"):
			suppliers = self._suppliers(name)
			self.assertEqual(len(suppliers), 1, name)
			self.assertTrue(suppliers[0].is_transporter, name)

	def test_an_existing_non_transporter_supplier_is_left_unchanged_and_reported(self):
		frappe.get_doc(
			{
				"doctype": "Supplier",
				"supplier_name": "Uber",
				"supplier_group": "All Supplier Groups",
				"supplier_type": "Company",
				"is_transporter": 0,
			}
		).insert()
		with patch.object(frappe, "log_error") as log_error:
			courier_patch.execute()
		self.assertEqual(len(self._suppliers("Uber")), 1)
		self.assertFalse(self._suppliers("Uber")[0].is_transporter)
		log_error.assert_called_once()
		self.assertEqual(len(self._suppliers("inDrive")), 1)
