# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.integrations.noon.sync import persist_outcomes, selected_mappings


class IntegrationTestNoonSyncState(IntegrationTestCase):
	def setUp(self):
		self.account = self.make_account()
		item_code = f"TEST-NOON-{frappe.generate_hash(length=8)}"
		self.item = frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": item_code,
				"item_group": "Products",
				"stock_uom": "Nos",
				"is_stock_item": 1,
			}
		).insert()
		self.mapping = frappe.get_doc(
			{
				"doctype": "Marketplace Product Mapping",
				"marketplace_account": self.account.name,
				"item": self.item.name,
				"external_reference": f"SKU-{frappe.generate_hash(length=8)}",
			}
		).insert()

	def make_account(self):
		project = f"test-noon-{frappe.generate_hash(length=10)}"
		connection = frappe.get_doc(
			{
				"doctype": "Noon Connection",
				"connection_name": project,
				"project_code": project,
				"key_id": "test-key-id",
				"private_key": "-----BEGIN PRIVATE KEY-----\ntest-only\n-----END PRIVATE KEY-----\n",
				"environment": "Static Sandbox",
			}
		).insert()
		return frappe.get_doc(
			{
				"doctype": "Marketplace Account",
				"account_name": project,
				"platform": "Noon",
				"external_account_id": project,
				"connection": connection.name,
				"noon_country_code": "ae",
			}
		).insert()

	def test_only_exact_enabled_mapping_and_account_resolve(self):
		self.assertEqual(
			[entry[0].name for entry in selected_mappings(self.account.name)], [self.mapping.name]
		)
		other = self.make_account()
		self.assertEqual(selected_mappings(other.name, [self.mapping.name]), [])
		self.assertEqual(selected_mappings(other.name), [])
		self.mapping.enabled = 0
		self.mapping.save()
		self.assertEqual(selected_mappings(self.account.name), [])
		self.mapping.enabled = 1
		self.mapping.save()
		self.account.enabled = 0
		self.account.save()
		self.assertEqual(selected_mappings(self.account.name), [])

	def test_error_and_retry_status_persist_separately_for_price_and_stock(self):
		persist_outcomes({self.mapping.name: "price failed"})
		persist_outcomes({self.mapping.name: "stock failed"}, inventory=True)
		self.mapping.reload()
		self.assertEqual(self.mapping.sync_status, "Error")
		self.assertEqual(self.mapping.last_error, "price failed")
		self.assertEqual(self.mapping.inventory_sync_status, "Error")
		self.assertEqual(self.mapping.inventory_last_error, "stock failed")
		persist_outcomes({self.mapping.name: None})
		persist_outcomes({self.mapping.name: None}, inventory=True)
		self.mapping.reload()
		self.assertEqual(self.mapping.sync_status, "Synced")
		self.assertFalse(self.mapping.last_error)
		self.assertEqual(self.mapping.inventory_sync_status, "Synced")
		self.assertFalse(self.mapping.inventory_last_error)
