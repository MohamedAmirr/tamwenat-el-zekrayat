# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.patches.v0_0.migrate_talabat_accounts_and_mappings import (
	CONNECTION_DOCTYPE,
	MIGRATED_CONNECTION_SOURCE,
	execute,
	get_legacy_mappings,
	get_legacy_vendors,
	get_migrated_connection,
	validate_legacy_mappings,
	validate_legacy_vendors,
)


class IntegrationTestMigrateTalabatAccountsAndMappings(IntegrationTestCase):
	def setUp(self):
		# The dev bench is shared between branches. Keep every fixture and every
		# target row produced by execute() inside this test's transaction, then
		# roll it all back in tearDown instead of deleting any shared records.
		self.savepoint = "issue_38_" + frappe.generate_hash(length=10)
		frappe.db.savepoint(self.savepoint)
		self.fixture_suffix = frappe.generate_hash(length=10)
		self.item_name = self.make_item()
		self.connection = self.get_or_make_migrated_connection()
		self.vendor = self.make_vendor("Vendor One", is_active=0)
		self.legacy_mapping = self.make_legacy_mapping(
			self.vendor.name,
			"sku-one-" + self.fixture_suffix,
			sync_status="Synced",
			last_error="legacy error",
		)
		self.legacy_vendor_snapshot = self.source_values(
			"Talabat Vendor", self.vendor.name, ["vendor_id", "vendor_name", "is_active"]
		)
		self.legacy_mapping_snapshot = self.source_values(
			"Talabat Product Mapping",
			self.legacy_mapping.name,
			["vendor", "item", "talabat_sku", "sync_status", "last_synced_on", "last_error"],
		)

	def tearDown(self):
		frappe.db.rollback(save_point=self.savepoint)

	def get_or_make_migrated_connection(self):
		connections = frappe.get_all(
			CONNECTION_DOCTYPE,
			filters={"legacy_migration_source": MIGRATED_CONNECTION_SOURCE},
			pluck="name",
		)
		if len(connections) > 1:
			self.skipTest("The shared test site has more than one marker-tagged Talabat Connection.")
		if connections:
			return connections[0]

		connection = frappe.get_doc(
			{
				"doctype": CONNECTION_DOCTYPE,
				"connection_name": "#38 test connection " + self.fixture_suffix,
				"client_id": "test-client-" + self.fixture_suffix,
				"client_secret": "test-secret-" + self.fixture_suffix,
				"chain_id": "test-chain-" + self.fixture_suffix,
				"environment": "Sandbox",
				"legacy_migration_source": MIGRATED_CONNECTION_SOURCE,
			}
		).insert(ignore_permissions=True)
		return connection.name

	def make_item(self):
		item = frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": "#38-test-item-" + self.fixture_suffix,
				"item_name": "#38 test item " + self.fixture_suffix,
				"item_group": "All Item Groups",
				"stock_uom": "Nos",
				"is_stock_item": 0,
			}
		).insert(ignore_permissions=True)
		return item.name

	def source_values(self, doctype, name, fields):
		return frappe.db.get_value(doctype, name, fields, as_dict=True)

	def make_vendor(self, vendor_name, is_active):
		vendor = frappe.get_doc(
			{
				"doctype": "Talabat Vendor",
				"vendor_id": "#38-vendor-" + self.fixture_suffix,
				"vendor_name": vendor_name,
				"is_active": is_active,
			}
		).insert(ignore_permissions=True)
		return vendor

	def make_legacy_mapping(self, vendor, sku, **kwargs):
		mapping = frappe.get_doc(
			{
				"doctype": "Talabat Product Mapping",
				"vendor": vendor,
				"item": self.item_name,
				"talabat_sku": sku,
				**kwargs,
			}
		).insert(ignore_permissions=True)
		return mapping

	def test_migrates_source_records_without_rewriting_them_and_is_idempotent(self):
		execute()
		execute()

		accounts = frappe.get_all(
			"Marketplace Account",
			filters={"platform": "Talabat", "external_account_id": self.vendor.vendor_id},
			fields=["name", "account_name", "connection_doctype", "connection", "warehouse", "enabled"],
		)
		self.assertEqual(len(accounts), 1)
		account = accounts[0]
		self.assertEqual(account.account_name, "Vendor One")
		self.assertEqual(account.connection_doctype, CONNECTION_DOCTYPE)
		self.assertEqual(account.connection, self.connection)
		self.assertFalse(account.warehouse)
		self.assertEqual(account.enabled, 0)

		mappings = frappe.get_all(
			"Marketplace Product Mapping",
			filters={
				"marketplace_account": account.name,
				"external_reference": self.legacy_mapping.talabat_sku,
			},
			fields=["name", "item", "external_sku", "enabled", "sync_status", "last_synced_on", "last_error"],
		)
		self.assertEqual(len(mappings), 1)
		mapping = mappings[0]
		self.assertEqual(mapping.item, self.item_name)
		self.assertEqual(mapping.external_sku, self.legacy_mapping.talabat_sku)
		self.assertEqual(mapping.enabled, 1)
		self.assertEqual(mapping.sync_status, "Synced")
		self.assertEqual(mapping.last_synced_on, self.legacy_mapping.last_synced_on)
		self.assertEqual(mapping.last_error, "legacy error")

		self.assertEqual(
			self.source_values("Talabat Vendor", self.vendor.name, ["vendor_id", "vendor_name", "is_active"]),
			self.legacy_vendor_snapshot,
		)
		self.assertEqual(
			self.source_values(
				"Talabat Product Mapping",
				self.legacy_mapping.name,
				["vendor", "item", "talabat_sku", "sync_status", "last_synced_on", "last_error"],
			),
			self.legacy_mapping_snapshot,
		)

		# The new schema permits a second listing for the same Item/account.
		frappe.get_doc(
			{
				"doctype": "Marketplace Product Mapping",
				"marketplace_account": account.name,
				"item": self.item_name,
				"external_reference": "sku-two-" + self.fixture_suffix,
				"external_sku": "sku-two-" + self.fixture_suffix,
			}
		).insert(ignore_permissions=True)
		self.assertEqual(
			frappe.db.count("Marketplace Product Mapping", {"marketplace_account": account.name}), 2
		)

	def test_fails_before_writing_targets_when_a_legacy_mapping_has_no_sku(self):
		second_vendor = frappe.get_doc(
			{
				"doctype": "Talabat Vendor",
				"vendor_id": "#38-empty-sku-vendor-" + self.fixture_suffix,
				"vendor_name": "Empty SKU Vendor",
			}
		).insert(ignore_permissions=True)
		empty_sku_mapping = self.make_legacy_mapping(second_vendor.name, "")

		with self.assertRaisesRegex(frappe.ValidationError, "has no Talabat SKU"):
			execute()

		self.assertFalse(
			frappe.db.exists("Marketplace Account", {"external_account_id": second_vendor.vendor_id})
		)
		self.assertFalse(
			frappe.db.exists("Marketplace Account", {"external_account_id": self.vendor.vendor_id})
		)
		self.assertTrue(frappe.db.exists("Talabat Product Mapping", empty_sku_mapping.name))

	def test_fails_clearly_when_the_marker_tagged_connection_is_missing(self):
		with patch(
			"marketplace_integrations.patches.v0_0.migrate_talabat_accounts_and_mappings.frappe.get_all",
			return_value=[],
		):
			with self.assertRaisesRegex(frappe.ValidationError, "exactly one Talabat Connection"):
				get_migrated_connection()

	def test_fails_clearly_when_multiple_marker_tagged_connections_exist(self):
		with patch(
			"marketplace_integrations.patches.v0_0.migrate_talabat_accounts_and_mappings.frappe.get_all",
			return_value=["TALCONN-ONE", "TALCONN-TWO"],
		):
			with self.assertRaisesRegex(frappe.ValidationError, "exactly one Talabat Connection"):
				get_migrated_connection()

	def test_rejects_blank_vendor_id_during_preflight(self):
		with self.assertRaisesRegex(frappe.ValidationError, "has no Vendor ID"):
			validate_legacy_vendors([frappe._dict(name="blank-vendor", vendor_id=" \t")])

	def test_rejects_missing_vendor_reference_during_preflight(self):
		with self.assertRaisesRegex(frappe.ValidationError, "references missing Talabat Vendor"):
			validate_legacy_mappings(
				[frappe._dict(name="missing-vendor", vendor="missing", talabat_sku="sku-1")], set()
			)

	def test_rejects_duplicate_vendor_sku_during_preflight(self):
		with self.assertRaisesRegex(frappe.ValidationError, "duplicates Talabat SKU"):
			validate_legacy_mappings(
				[
					frappe._dict(
						name="mapping-one", vendor="vendor-1", item=self.item_name, talabat_sku=" sku-1 "
					),
					frappe._dict(
						name="mapping-two", vendor="vendor-1", item=self.item_name, talabat_sku="sku-1"
					),
				],
				{"vendor-1"},
			)

	def test_missing_mapping_item_preflight_writes_no_targets(self):
		invalid_mapping = frappe._dict(
			name="missing-item-mapping",
			vendor=self.vendor.name,
			item="missing-item-" + self.fixture_suffix,
			talabat_sku="missing-item-sku-" + self.fixture_suffix,
		)
		with (
			patch(
				"marketplace_integrations.patches.v0_0.migrate_talabat_accounts_and_mappings.get_legacy_vendors",
				return_value=[self.vendor],
			),
			patch(
				"marketplace_integrations.patches.v0_0.migrate_talabat_accounts_and_mappings.get_legacy_mappings",
				return_value=[invalid_mapping],
			),
		):
			with self.assertRaisesRegex(frappe.ValidationError, "references missing Item"):
				execute()

		self.assertFalse(
			frappe.db.exists("Marketplace Account", {"external_account_id": self.vendor.vendor_id})
		)
		self.assertFalse(
			frappe.db.exists(
				"Marketplace Product Mapping", {"external_reference": invalid_mapping.talabat_sku}
			)
		)

	def test_missing_source_doctype_is_an_empty_source_list(self):
		with patch(
			"marketplace_integrations.patches.v0_0.migrate_talabat_accounts_and_mappings.frappe.db.table_exists",
			return_value=False,
		):
			self.assertEqual(get_legacy_vendors(), [])
			self.assertEqual(get_legacy_mappings(), [])

	def test_migrates_vendor_only_source(self):
		with (
			patch(
				"marketplace_integrations.patches.v0_0.migrate_talabat_accounts_and_mappings.get_legacy_vendors",
				return_value=[self.vendor],
			),
			patch(
				"marketplace_integrations.patches.v0_0.migrate_talabat_accounts_and_mappings.get_legacy_mappings",
				return_value=[],
			),
		):
			execute()

		self.assertTrue(
			frappe.db.exists("Marketplace Account", {"external_account_id": self.vendor.vendor_id})
		)

	def test_mapping_only_source_does_not_silently_noop(self):
		with (
			patch(
				"marketplace_integrations.patches.v0_0.migrate_talabat_accounts_and_mappings.get_legacy_vendors",
				return_value=[],
			),
			patch(
				"marketplace_integrations.patches.v0_0.migrate_talabat_accounts_and_mappings.get_legacy_mappings",
				return_value=[self.legacy_mapping],
			),
		):
			with self.assertRaisesRegex(frappe.ValidationError, "references missing Talabat Vendor"):
				execute()
