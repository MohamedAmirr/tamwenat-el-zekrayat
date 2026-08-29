# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

import frappe
from frappe.tests import IntegrationTestCase

# Item's own test-record dependency chain pulls in ERPNext's default Fiscal
# Year fixtures, which have a pre-existing overlap on this dev site unrelated
# to this doctype. We reference an already-seeded Item directly instead of
# relying on auto-generated ones, so skip that chain.
IGNORE_TEST_RECORD_DEPENDENCIES = ["Item"]

TEST_ITEM = "CHIPS-CLASSIC-150G"


class IntegrationTestTalabatProductMapping(IntegrationTestCase):
	def make_vendor(self, vendor_id):
		return frappe.get_doc(
			{
				"doctype": "Talabat Vendor",
				"vendor_id": vendor_id,
				"vendor_name": vendor_id,
			}
		).insert()

	def test_fields_persist(self):
		vendor = self.make_vendor("test-mapping-vendor-001")

		mapping = frappe.get_doc(
			{
				"doctype": "Talabat Product Mapping",
				"item": TEST_ITEM,
				"vendor": vendor.name,
				"talabat_sku": "34000000289",
			}
		).insert()

		mapping.reload()
		self.assertEqual(mapping.item, TEST_ITEM)
		self.assertEqual(mapping.vendor, vendor.name)
		self.assertEqual(mapping.talabat_sku, "34000000289")
		self.assertEqual(mapping.sync_status, "Not Synced")

	def test_duplicate_item_vendor_rejected(self):
		vendor = self.make_vendor("test-mapping-vendor-002")

		frappe.get_doc(
			{
				"doctype": "Talabat Product Mapping",
				"item": TEST_ITEM,
				"vendor": vendor.name,
				"talabat_sku": "111",
			}
		).insert()

		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc(
				{
					"doctype": "Talabat Product Mapping",
					"item": TEST_ITEM,
					"vendor": vendor.name,
					"talabat_sku": "222",
				}
			).insert()

	def test_same_item_different_vendor_allowed(self):
		vendor_a = self.make_vendor("test-mapping-vendor-003a")
		vendor_b = self.make_vendor("test-mapping-vendor-003b")

		frappe.get_doc(
			{
				"doctype": "Talabat Product Mapping",
				"item": TEST_ITEM,
				"vendor": vendor_a.name,
				"talabat_sku": "333",
			}
		).insert()

		second = frappe.get_doc(
			{
				"doctype": "Talabat Product Mapping",
				"item": TEST_ITEM,
				"vendor": vendor_b.name,
				"talabat_sku": "444",
			}
		).insert()

		self.assertEqual(second.vendor, vendor_b.name)
