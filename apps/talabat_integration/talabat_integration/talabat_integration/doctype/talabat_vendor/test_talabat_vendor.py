# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

import frappe
from frappe.tests import IntegrationTestCase


class IntegrationTestTalabatVendor(IntegrationTestCase):
	def test_fields_persist(self):
		vendor = frappe.get_doc(
			{
				"doctype": "Talabat Vendor",
				"vendor_id": "test-vendor-001",
				"vendor_name": "Test Outlet",
				"is_active": 1,
			}
		).insert()

		vendor.reload()
		self.assertEqual(vendor.vendor_id, "test-vendor-001")
		self.assertEqual(vendor.vendor_name, "Test Outlet")
		self.assertEqual(vendor.is_active, 1)

	def test_duplicate_vendor_id_rejected(self):
		frappe.get_doc(
			{
				"doctype": "Talabat Vendor",
				"vendor_id": "test-vendor-002",
				"vendor_name": "First Outlet",
			}
		).insert()

		with self.assertRaises(frappe.DuplicateEntryError):
			frappe.get_doc(
				{
					"doctype": "Talabat Vendor",
					"vendor_id": "test-vendor-002",
					"vendor_name": "Second Outlet",
				}
			).insert()
