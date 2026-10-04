# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.patches.v0_0.backfill_marketplace_product_mapping_platform import execute

# Same structural stand-in connection used by test_marketplace_product_mapping.py
# (no platform Connection DocType is needed just to exercise this patch).
STAND_IN_CONNECTION_DOCTYPE = "UOM"
STAND_IN_CONNECTION = "Nos"


class IntegrationTestBackfillMarketplaceProductMappingPlatform(IntegrationTestCase):
	def setUp(self):
		self.item_code = f"MPM-PLATFORM-BACKFILL-{frappe.generate_hash(length=8)}"
		frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": self.item_code,
				"item_group": "Products",
				"stock_uom": "Nos",
				"is_stock_item": 0,
			}
		).insert()

	def tearDown(self):
		# Delete mappings before accounts (naming-series revert gotcha)
		frappe.db.delete("Marketplace Product Mapping", {"item": self.item_code})
		# Only delete Marketplace Accounts created by make_account() in tests
		# (the stand-in Connection is shared and not created by tests)
		frappe.db.delete("Marketplace Account", {"external_account_id": ["like", "mpm-platform-backfill-%"]})

	def make_account(self, platform):
		suffix = frappe.generate_hash(length=8)
		return frappe.get_doc(
			{
				"doctype": "Marketplace Account",
				"account_name": f"MPM Platform Backfill {suffix}",
				"platform": platform,
				"external_account_id": f"mpm-platform-backfill-{suffix}",
				"connection_doctype": STAND_IN_CONNECTION_DOCTYPE,
				"connection": STAND_IN_CONNECTION,
			}
		).insert()

	def make_mapping(self, account):
		return frappe.get_doc(
			{
				"doctype": "Marketplace Product Mapping",
				"marketplace_account": account.name,
				"item": self.item_code,
				"external_reference": f"ref-{frappe.generate_hash(length=8)}",
			}
		).insert()

	def blank_platform(self, mapping):
		frappe.db.set_value(
			"Marketplace Product Mapping", mapping.name, "platform", "", update_modified=False
		)
		frappe.clear_document_cache("Marketplace Product Mapping", mapping.name)

	def test_backfills_blank_platform_from_linked_account(self):
		account = self.make_account("Talabat")
		mapping = self.make_mapping(account)
		self.blank_platform(mapping)

		execute()

		self.assertEqual(
			frappe.db.get_value("Marketplace Product Mapping", mapping.name, "platform"), "Talabat"
		)

	def test_backfills_a_noon_linked_mapping(self):
		# A real Noon account needs a Noon Connection; this patch only reads the
		# account's platform, so switch the platform on a stand-in account
		# (same approach as the order-platform backfill test for Shopify).
		account = self.make_account("Talabat")
		frappe.db.set_value("Marketplace Account", account.name, "platform", "Noon", update_modified=False)
		frappe.clear_document_cache("Marketplace Account", account.name)
		mapping = self.make_mapping(account)
		self.blank_platform(mapping)

		execute()

		self.assertEqual(frappe.db.get_value("Marketplace Product Mapping", mapping.name, "platform"), "Noon")

	def test_does_not_overwrite_an_already_set_platform(self):
		account = self.make_account("Talabat")
		mapping = self.make_mapping(account)
		frappe.db.set_value(
			"Marketplace Product Mapping", mapping.name, "platform", "Shopify", update_modified=False
		)
		frappe.clear_document_cache("Marketplace Product Mapping", mapping.name)

		execute()

		self.assertEqual(
			frappe.db.get_value("Marketplace Product Mapping", mapping.name, "platform"), "Shopify"
		)

	def test_backfill_is_idempotent(self):
		account = self.make_account("Talabat")
		mapping = self.make_mapping(account)
		self.blank_platform(mapping)

		execute()
		self.assertEqual(
			frappe.db.get_value("Marketplace Product Mapping", mapping.name, "platform"), "Talabat"
		)

		# Change the account's platform underneath (bypassing the controller). A
		# correctly guarded second run must not propagate it onto the filled row.
		frappe.db.set_value("Marketplace Account", account.name, "platform", "Shopify", update_modified=False)
		frappe.clear_document_cache("Marketplace Account", account.name)

		execute()

		self.assertEqual(
			frappe.db.get_value("Marketplace Product Mapping", mapping.name, "platform"), "Talabat"
		)

	def test_never_touches_identity_key(self):
		account = self.make_account("Talabat")
		mapping = self.make_mapping(account)
		self.blank_platform(mapping)
		key_before = frappe.db.get_value("Marketplace Product Mapping", mapping.name, "mapping_identity_key")

		execute()

		self.assertEqual(
			frappe.db.get_value("Marketplace Product Mapping", mapping.name, "mapping_identity_key"),
			key_before,
		)
