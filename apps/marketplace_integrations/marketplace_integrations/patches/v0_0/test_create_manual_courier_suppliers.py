# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.patches.v0_0 import create_manual_courier_suppliers as courier_patch


class IntegrationTestCreateManualCourierSuppliers(IntegrationTestCase):
	def setUp(self):
		# Start from a site without the Suppliers (a rollback in tearDown restores whatever was there).
		for name in frappe.get_all(
			"Supplier", filters={"supplier_name": ["in", list(courier_patch.SUPPLIER_NAMES)]}, pluck="name"
		):
			frappe.delete_doc("Supplier", name, force=True, ignore_permissions=True)

	def tearDown(self):
		frappe.db.rollback()

	def _suppliers(self, name):
		return frappe.get_all("Supplier", filters={"supplier_name": name}, fields=["name", "is_transporter"])

	def test_each_supplier_exists_once_as_a_transporter_after_any_number_of_runs(self):
		courier_patch.execute()
		courier_patch.execute()
		self.assertEqual(courier_patch.SUPPLIER_NAMES, ("Manual delivery",))
		for name in courier_patch.SUPPLIER_NAMES:
			suppliers = self._suppliers(name)
			self.assertEqual(len(suppliers), 1, name)
			self.assertTrue(suppliers[0].is_transporter, name)

	def test_the_old_three_suppliers_are_not_created(self):
		old_names = ("inDrive", "Uber", "Own driver")
		before = {name: self._suppliers(name) for name in old_names}
		courier_patch.execute()
		# A site where the old patch ran keeps its old Suppliers; none is added by this patch.
		self.assertEqual({name: self._suppliers(name) for name in old_names}, before)

	def test_an_existing_non_transporter_supplier_is_left_unchanged_and_reported(self):
		frappe.get_doc(
			{
				"doctype": "Supplier",
				"supplier_name": "Manual delivery",
				"supplier_group": "All Supplier Groups",
				"supplier_type": "Company",
				"is_transporter": 0,
			}
		).insert()
		with patch.object(frappe, "log_error") as log_error:
			courier_patch.execute()
		self.assertEqual(len(self._suppliers("Manual delivery")), 1)
		self.assertFalse(self._suppliers("Manual delivery")[0].is_transporter)
		log_error.assert_called_once()

	def _without_all_supplier_groups(self):
		"""Make the ``All Supplier Groups`` lookup report a missing group; every other lookup stays real."""
		real_exists = frappe.db.exists

		def exists(doctype, name=None, *args, **kwargs):
			if doctype == "Supplier Group" and name == courier_patch.SUPPLIER_GROUP:
				return False
			return real_exists(doctype, name, *args, **kwargs)

		return patch.object(frappe.db, "exists", side_effect=exists)

	def _buying_settings_supplier_group(self, value):
		"""Make the Buying Settings ``supplier_group`` read return ``value``; every other read stays real."""
		real_get_single_value = frappe.db.get_single_value

		def get_single_value(doctype, fieldname, *args, **kwargs):
			if (doctype, fieldname) == ("Buying Settings", "supplier_group"):
				return value
			return real_get_single_value(doctype, fieldname, *args, **kwargs)

		return patch.object(frappe.db, "get_single_value", side_effect=get_single_value)

	def test_creates_all_suppliers_without_a_group_when_all_supplier_groups_and_buying_settings_have_none(
		self,
	):
		with self._without_all_supplier_groups(), self._buying_settings_supplier_group(None):
			courier_patch.execute()
		for name in courier_patch.SUPPLIER_NAMES:
			suppliers = frappe.get_all(
				"Supplier",
				filters={"supplier_name": name},
				fields=["name", "is_transporter", "supplier_group"],
			)
			self.assertEqual(len(suppliers), 1, name)
			self.assertTrue(suppliers[0].is_transporter, name)
			self.assertFalse(suppliers[0].supplier_group, name)

	def test_creates_all_suppliers_in_the_buying_settings_group_when_all_supplier_groups_is_missing(self):
		fallback = frappe.get_all(
			"Supplier Group", filters={"name": ["!=", courier_patch.SUPPLIER_GROUP]}, pluck="name", limit=1
		)
		if not fallback:
			self.skipTest("site has no Supplier Group other than All Supplier Groups")
		with self._without_all_supplier_groups(), self._buying_settings_supplier_group(fallback[0]):
			courier_patch.execute()
		for name in courier_patch.SUPPLIER_NAMES:
			suppliers = frappe.get_all(
				"Supplier",
				filters={"supplier_name": name},
				fields=["name", "is_transporter", "supplier_group"],
			)
			self.assertEqual(len(suppliers), 1, name)
			self.assertTrue(suppliers[0].is_transporter, name)
			self.assertEqual(suppliers[0].supplier_group, fallback[0], name)
