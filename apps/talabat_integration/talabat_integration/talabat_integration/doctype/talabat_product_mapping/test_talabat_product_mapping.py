# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

import frappe
from frappe.tests import IntegrationTestCase

from talabat_integration.talabat_integration.doctype.talabat_product_mapping.talabat_product_mapping import (
	compute_item_vendor_key,
)

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

	def test_talabat_sku_is_not_in_default_list_view(self):
		field = frappe.get_meta("Talabat Product Mapping").get_field("talabat_sku")

		self.assertFalse(field.in_list_view)

	def test_talabat_sku_with_angle_brackets_rejected_at_insert(self):
		# name is built by a custom autoname() as f"{vendor}::{talabat_sku}",
		# and Frappe's own document-name validation (validate_name) runs on
		# whatever autoname() produces, unconditionally rejecting "<"/">" -
		# regardless of ignore_xss_filter, which only governs field-value
		# sanitization, not name validation. So an insert with "<"/">" in
		# talabat_sku is still rejected outright, same as before #19.
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

	def test_talabat_sku_with_angle_brackets_survives_update(self):
		# Unlike insert (name validation always guards this), an UPDATE to
		# talabat_sku doesn't touch `name` at all (autoname() only runs at
		# insert) - it just goes through _sanitize_content() like any other
		# field save. ignore_xss_filter on talabat_sku is what stops that from
		# silently mangling the value, the way it once did for client_secret
		# (#17) - this proves the same protection now holds here.
		vendor = self.make_vendor("test-mapping-vendor-007")

		mapping = frappe.get_doc(
			{
				"doctype": "Talabat Product Mapping",
				"item": TEST_ITEM,
				"vendor": vendor.name,
				"talabat_sku": "clean-sku-001",
			}
		).insert()

		mapping.talabat_sku = "sku-<p<+X>-001"
		mapping.save()
		mapping.reload()

		self.assertEqual(mapping.talabat_sku, "sku-<p<+X>-001")

	def test_separator_rejected_in_vendor_or_sku(self):
		# The naming scheme's whole safety argument depends on "::" never
		# appearing in vendor or talabat_sku - enforce it, don't just assume it.
		vendor = self.make_vendor("test-mapping-vendor-008")

		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc(
				{
					"doctype": "Talabat Product Mapping",
					"item": TEST_ITEM,
					"vendor": vendor.name,
					"talabat_sku": "sku::with-separator",
				}
			).insert()

	def test_duplicate_vendor_sku_rejected(self):
		# autoname() computes name as f"{vendor}::{talabat_sku}", so a straight
		# duplicate insert (same vendor+sku, different item) computes the same
		# `name` as the existing record before validate() runs - it's
		# MariaDB's own primary-key constraint that rejects it, atomically, as
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
		self.assertEqual(mapping.item_vendor_key, compute_item_vendor_key(other_item, vendor.name))

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

	def test_item_vendor_key_does_not_collide_on_hyphen(self):
		# A bare "-"-joined key would make item="AA-BB"+vendor="CC" collide
		# with item="AA"+vendor="BB-CC" (both -> "AA-BB-CC"). Real risk here:
		# item codes in this app's own seed data contain hyphens. JSON
		# encoding (compute_item_vendor_key) must keep these distinct.
		item_ab = "TEST-MAPPING-AA-BB"
		item_a = "TEST-MAPPING-AA"
		for code in (item_ab, item_a):
			if not frappe.db.exists("Item", code):
				frappe.get_doc(
					{
						"doctype": "Item",
						"item_code": code,
						"item_group": "Products",
						"stock_uom": "Nos",
						"is_stock_item": 0,
					}
				).insert()

		vendor_cc = self.make_vendor("test-mapping-vendor-CC")
		vendor_bc = self.make_vendor("test-mapping-vendor-BB-CC")

		frappe.get_doc(
			{
				"doctype": "Talabat Product Mapping",
				"item": item_ab,
				"vendor": vendor_cc.name,
				"talabat_sku": "collision-test-sku-1",
			}
		).insert()

		# Should succeed - item_a + vendor_bc is a genuinely different pair,
		# even though a bare "-" join would make both keys "AA-BB-CC".
		second = frappe.get_doc(
			{
				"doctype": "Talabat Product Mapping",
				"item": item_a,
				"vendor": vendor_bc.name,
				"talabat_sku": "collision-test-sku-2",
			}
		).insert()

		self.assertEqual(second.item, item_a)
		self.assertNotEqual(
			compute_item_vendor_key(item_ab, vendor_cc.name),
			compute_item_vendor_key(item_a, vendor_bc.name),
		)

	def test_name_does_not_collide_on_hyphen(self):
		# Same collision shape, for the primary name this time: vendor="X-Y"
		# + sku="Z" vs vendor="X" + sku="Y-Z" would both be "X-Y-Z" under a
		# bare "-" join. The "::" separator must keep these distinct.
		vendor_xy = self.make_vendor("test-mapping-vendor-X-Y")
		vendor_x = self.make_vendor("test-mapping-vendor-X")

		frappe.get_doc(
			{
				"doctype": "Talabat Product Mapping",
				"item": TEST_ITEM,
				"vendor": vendor_xy.name,
				"talabat_sku": "Z",
			}
		).insert()

		# Should succeed - a bare "-" join would collide on name "X-Y-Z" for
		# both; "::" keeps them apart ("X-Y::Z" vs "X::Y-Z").
		other_item = "TEST-MAPPING-ITEM-4"
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

		second = frappe.get_doc(
			{
				"doctype": "Talabat Product Mapping",
				"item": other_item,
				"vendor": vendor_x.name,
				"talabat_sku": "Y-Z",
			}
		).insert()

		self.assertNotEqual(second.name, f"{vendor_xy.name}::Z")
