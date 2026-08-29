# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

import frappe
from frappe.tests import IntegrationTestCase

# Frappe's IntegrationTestCase auto-generates a generic test record for every
# doctype linked from the one under test (here: Item). ERPNext's generic Item
# test-record generator hits a pre-existing Fiscal Year overlap bug on this
# dev site, unrelated to this doctype. We create our own minimal Item below
# instead of relying on that auto-generator (or on any externally-seeded
# data), so skip just the auto-generation step for Item.
IGNORE_TEST_RECORD_DEPENDENCIES = ["Item"]

TEST_ITEM = "TEST-MAPPING-ITEM"


class IntegrationTestTalabatProductMapping(IntegrationTestCase):
	def setUp(self):
		if not frappe.db.exists("Item", TEST_ITEM):
			frappe.get_doc(
				{
					"doctype": "Item",
					"item_code": TEST_ITEM,
					"item_group": "Products",
					"stock_uom": "Nos",
					"is_stock_item": 0,
				}
			).insert()

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

	def test_talabat_sku_with_angle_brackets_persists_exactly(self):
		# Regression: same class of bug as Talabat Settings' client_secret -
		# Frappe's HTML/XSS sanitizer mutilates values containing "<"/">" unless
		# the field sets "ignore_xss_filter". talabat_sku is an opaque external
		# SKU, not HTML, and (unlike vendor_id) isn't part of autoname, so it can
		# legitimately contain these characters and must survive a save intact.
		vendor = self.make_vendor("test-mapping-vendor-005")
		tricky_sku = "sku-<p<+X>-001"

		mapping = frappe.get_doc(
			{
				"doctype": "Talabat Product Mapping",
				"item": TEST_ITEM,
				"vendor": vendor.name,
				"talabat_sku": tricky_sku,
			}
		).insert()

		mapping.reload()
		self.assertEqual(mapping.talabat_sku, tricky_sku)

	def test_duplicate_item_vendor_rejected(self):
		# autoname is format:{item}-{vendor}, so a straight duplicate insert
		# computes the same `name` as the existing record before validate()
		# runs - the app-level check's `name != self.name` exclusion doesn't
		# see it (same name on both sides), and it's MariaDB's own primary-key
		# constraint that rejects it, atomically, as DuplicateEntryError.
		vendor = self.make_vendor("test-mapping-vendor-002")

		frappe.get_doc(
			{
				"doctype": "Talabat Product Mapping",
				"item": TEST_ITEM,
				"vendor": vendor.name,
				"talabat_sku": "111",
			}
		).insert()

		with self.assertRaises(frappe.DuplicateEntryError):
			frappe.get_doc(
				{
					"doctype": "Talabat Product Mapping",
					"item": TEST_ITEM,
					"vendor": vendor.name,
					"talabat_sku": "222",
				}
			).insert()

	def test_editing_to_create_duplicate_rejected(self):
		# The PK constraint from autoname only fires when both records were
		# created with the same (item, vendor) from the start (same computed
		# name). Editing an *existing* record's vendor to collide with a
		# different existing record (different original name) isn't caught by
		# the PK - this is exactly what validate_unique_item_vendor() is still
		# for, and remains reachable.
		vendor_a = self.make_vendor("test-mapping-vendor-004a")
		vendor_b = self.make_vendor("test-mapping-vendor-004b")

		frappe.get_doc(
			{
				"doctype": "Talabat Product Mapping",
				"item": TEST_ITEM,
				"vendor": vendor_a.name,
				"talabat_sku": "555",
			}
		).insert()

		other = frappe.get_doc(
			{
				"doctype": "Talabat Product Mapping",
				"item": TEST_ITEM,
				"vendor": vendor_b.name,
				"talabat_sku": "666",
			}
		).insert()

		other.vendor = vendor_a.name
		with self.assertRaises(frappe.ValidationError):
			other.save()

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
