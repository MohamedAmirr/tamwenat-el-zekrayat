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

	def test_talabat_sku_with_angle_brackets_rejected_by_name_validation(self):
		# talabat_sku is now part of autoname (format:{vendor}-{talabat_sku},
		# issue #19), so it's subject to Frappe's own document-name validation,
		# which unconditionally rejects "<"/">" - the same restriction PR #17
		# confirmed for vendor_id, and for the same reason: name validation
		# runs before the XSS sanitizer ever would, so ignore_xss_filter (set
		# on this field prior to #19) no longer has anything to protect
		# against here and was removed. A real Talabat SKU containing "<"/">"
		# would be rejected outright now - an accepted, documented limitation,
		# not a silent gap.
		vendor = self.make_vendor("test-mapping-vendor-005")

		with self.assertRaises(frappe.NameError):
			frappe.get_doc(
				{
					"doctype": "Talabat Product Mapping",
					"item": TEST_ITEM,
					"vendor": vendor.name,
					"talabat_sku": "sku-<p<+X>-001",
				}
			).insert()

	def test_duplicate_vendor_sku_rejected(self):
		# autoname is format:{vendor}-{talabat_sku}, so a straight duplicate
		# insert (same vendor+sku, different item) computes the same `name`
		# as the existing record before validate() runs - it's MariaDB's own
		# primary-key constraint that rejects it, atomically, as
		# DuplicateEntryError.
		vendor = self.make_vendor("test-mapping-vendor-002")

		frappe.get_doc(
			{
				"doctype": "Talabat Product Mapping",
				"item": TEST_ITEM,
				"vendor": vendor.name,
				"talabat_sku": "111",
			}
		).insert()

		other_item = "TEST-MAPPING-ITEM-2"
		if not frappe.db.exists("Item", other_item):
			frappe.get_doc(
				{
					"doctype": "Item",
					"item_code": other_item,
					"item_group": "Products",
					"stock_uom": "Nos",
					"is_stock_item": 0,
				}
			).insert()

		with self.assertRaises(frappe.DuplicateEntryError):
			frappe.get_doc(
				{
					"doctype": "Talabat Product Mapping",
					"item": other_item,
					"vendor": vendor.name,
					"talabat_sku": "111",
				}
			).insert()

	def test_same_item_different_sku_same_vendor_rejected(self):
		# Identity is now (vendor, talabat_sku), so this pair no longer
		# collides on `name` - it's item_vendor_key's own DB unique index
		# (and validate_unique_item_vendor()'s friendly pre-check) that must
		# catch it instead.
		vendor = self.make_vendor("test-mapping-vendor-004a")

		frappe.get_doc(
			{
				"doctype": "Talabat Product Mapping",
				"item": TEST_ITEM,
				"vendor": vendor.name,
				"talabat_sku": "555",
			}
		).insert()

		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc(
				{
					"doctype": "Talabat Product Mapping",
					"item": TEST_ITEM,
					"vendor": vendor.name,
					"talabat_sku": "666",
				}
			).insert()

	def test_changing_item_on_existing_mapping_persists(self):
		# The whole point of issue #19: under the old format:{item}-{vendor}
		# autoname, Frappe's _sync_autoname_field silently reverted any
		# attempt to change `item` on an existing record back to match the
		# name. Identity is now (vendor, talabat_sku), so `item` is free to
		# change and must actually persist.
		vendor = self.make_vendor("test-mapping-vendor-006")
		other_item = "TEST-MAPPING-ITEM-3"
		if not frappe.db.exists("Item", other_item):
			frappe.get_doc(
				{
					"doctype": "Item",
					"item_code": other_item,
					"item_group": "Products",
					"stock_uom": "Nos",
					"is_stock_item": 0,
				}
			).insert()

		mapping = frappe.get_doc(
			{
				"doctype": "Talabat Product Mapping",
				"item": TEST_ITEM,
				"vendor": vendor.name,
				"talabat_sku": "777",
			}
		).insert()

		mapping.item = other_item
		mapping.save()
		mapping.reload()

		self.assertEqual(mapping.item, other_item)
		self.assertEqual(mapping.item_vendor_key, f"{other_item}-{vendor.name}")

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
