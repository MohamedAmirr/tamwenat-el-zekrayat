# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

import json
from unittest.mock import patch

import frappe
import frappe.share

from marketplace_integrations.api.talabat_catalog import (
	queue_catalog_sync,
	queue_mapping_catalog_sync,
	queue_mappings_catalog_sync,
)
from marketplace_integrations.testing_utils import CleanupTestCase


class IntegrationTestTalabatCatalogApi(CleanupTestCase):
	def setUp(self):
		suffix = frappe.generate_hash(length=8)
		warehouse = frappe.get_all(
			"Warehouse", filters={"disabled": 0, "is_group": 0}, pluck="name", limit=1
		)[0]
		price_list = frappe.get_all(
			"Price List", filters={"selling": 1, "enabled": 1}, pluck="name", limit=1
		)[0]
		connection = frappe.get_doc(
			{
				"doctype": "Talabat Connection",
				"connection_name": f"Catalog API Connection {suffix}",
				"client_id": "test-client-id",
				"client_secret": "test-client-secret",
				"chain_id": f"test-chain-{suffix}",
			}
		).insert()
		self.account = frappe.get_doc(
			{
				"doctype": "Marketplace Account",
				"account_name": f"Catalog API Account {suffix}",
				"platform": "Talabat",
				"external_account_id": f"test-vendor-{suffix}",
				"connection_doctype": "Talabat Connection",
				"connection": connection.name,
				"warehouse": warehouse,
				"erpnext_selling_price_list": price_list,
			}
		).insert()
		self.item = frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": f"Catalog API Item {suffix}",
				"item_name": f"Catalog API Item {suffix}",
				"item_group": "Products",
				"stock_uom": "Nos",
			}
		).insert()
		self.mapping = frappe.get_doc(
			{
				"doctype": "Marketplace Product Mapping",
				"marketplace_account": self.account.name,
				"item": self.item.name,
				"external_reference": f"catalog-api-sku-{suffix}",
			}
		).insert()

	@patch("marketplace_integrations.api.talabat_catalog.frappe.enqueue")
	def test_queue_validates_then_enqueues_without_calling_talabat(self, enqueue):
		result = queue_catalog_sync(self.account.name)

		self.assertEqual(result, {"queued": True})
		enqueue.assert_called_once()
		self.assertEqual(enqueue.call_args.args[0].__name__, "sync_catalog")
		self.assertEqual(enqueue.call_args.kwargs["account"], self.account.name)
		self.assertTrue(enqueue.call_args.kwargs["enqueue_after_commit"])

	@patch("marketplace_integrations.api.talabat_catalog.frappe.enqueue")
	def test_queue_requires_account_write_permission(self, enqueue):
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": f"catalog-sync-{frappe.generate_hash(length=10)}@example.com",
				"first_name": "Catalog Sync Test User",
				"send_welcome_email": 0,
				"roles": [{"role": "All"}],
			}
		).insert()
		enqueue.reset_mock()

		with self.set_user(user.name):
			with self.assertRaises(frappe.PermissionError):
				queue_catalog_sync(self.account.name)
		enqueue.assert_not_called()

	@patch("marketplace_integrations.api.talabat_catalog.frappe.enqueue")
	def test_queue_mapping_enqueues_only_the_selected_mapping(self, enqueue):
		result = queue_mapping_catalog_sync(self.mapping.name)

		self.assertEqual(result, {"queued": True})
		enqueue.assert_called_once()
		self.assertEqual(enqueue.call_args.args[0].__name__, "sync_catalog")
		self.assertEqual(enqueue.call_args.kwargs["account"], self.account.name)
		self.assertEqual(enqueue.call_args.kwargs["mapping_names"], [self.mapping.name])
		self.assertTrue(enqueue.call_args.kwargs["enqueue_after_commit"])

	@patch("marketplace_integrations.api.talabat_catalog.frappe.enqueue")
	def test_queue_mappings_groups_selected_mappings_by_account(self, enqueue):
		second_account = frappe.get_doc(
			{
				"doctype": "Marketplace Account",
				"account_name": f"Catalog API Second Account {frappe.generate_hash(length=8)}",
				"platform": "Talabat",
				"external_account_id": f"test-vendor-{frappe.generate_hash(length=8)}",
				"connection_doctype": "Talabat Connection",
				"connection": self.account.connection,
				"warehouse": self.account.warehouse,
				"erpnext_selling_price_list": self.account.erpnext_selling_price_list,
			}
		).insert()
		second_mapping = frappe.get_doc(
			{
				"doctype": "Marketplace Product Mapping",
				"marketplace_account": second_account.name,
				"item": self.item.name,
				"external_reference": f"catalog-api-second-sku-{frappe.generate_hash(length=8)}",
			}
		).insert()
		extra_mapping = frappe.get_doc(
			{
				"doctype": "Marketplace Product Mapping",
				"marketplace_account": self.account.name,
				"item": self.item.name,
				"external_reference": f"catalog-api-extra-sku-{frappe.generate_hash(length=8)}",
			}
		).insert()

		result = queue_mappings_catalog_sync([self.mapping.name, second_mapping.name])

		self.assertEqual(result, {"queued": 2, "jobs": 2})
		self.assertEqual(enqueue.call_count, 2)
		queued = {call.kwargs["account"]: call.kwargs["mapping_names"] for call in enqueue.call_args_list}
		self.assertEqual(queued[self.account.name], [self.mapping.name])
		self.assertEqual(queued[second_account.name], [second_mapping.name])
		self.assertNotIn(extra_mapping.name, [name for names in queued.values() for name in names])

	@patch("marketplace_integrations.api.talabat_catalog.frappe.enqueue")
	def test_queue_mappings_accepts_a_json_encoded_string_of_names(self, enqueue):
		# frappe.call() always JSON-stringifies array arguments before sending, so
		# in real usage mapping_names arrives as a JSON string, never a native list.
		mapping_names_json = json.dumps([self.mapping.name])

		result = queue_mappings_catalog_sync(mapping_names_json)

		self.assertEqual(result, {"queued": 1, "jobs": 1})
		enqueue.assert_called_once()
		self.assertEqual(enqueue.call_args.kwargs["account"], self.account.name)
		self.assertEqual(enqueue.call_args.kwargs["mapping_names"], [self.mapping.name])

	@patch("marketplace_integrations.api.talabat_catalog.frappe.enqueue")
	def test_queue_mappings_rejects_malformed_json_string(self, enqueue):
		with self.assertRaisesRegex(frappe.ValidationError, "valid JSON"):
			queue_mappings_catalog_sync("[not valid json")
		enqueue.assert_not_called()

	@patch("marketplace_integrations.api.talabat_catalog.frappe.enqueue")
	def test_queue_mappings_rejects_json_string_that_is_not_a_list(self, enqueue):
		# Valid JSON, but parses to a bare string rather than a list.
		not_a_list_json = json.dumps(self.mapping.name)

		with self.assertRaisesRegex(frappe.ValidationError, "Select at least one"):
			queue_mappings_catalog_sync(not_a_list_json)
		enqueue.assert_not_called()

	@patch("marketplace_integrations.api.talabat_catalog.frappe.enqueue")
	def test_queue_mappings_rejects_an_empty_list(self, enqueue):
		with self.assertRaisesRegex(frappe.ValidationError, "Select at least one"):
			queue_mappings_catalog_sync([])
		enqueue.assert_not_called()

	@patch("marketplace_integrations.api.talabat_catalog.frappe.enqueue")
	def test_queue_mappings_rejects_a_blank_element(self, enqueue):
		with self.assertRaisesRegex(frappe.ValidationError, "valid names"):
			queue_mappings_catalog_sync([self.mapping.name, "  "])
		enqueue.assert_not_called()

	@patch("marketplace_integrations.api.talabat_catalog.frappe.enqueue")
	def test_queue_mappings_deduplicates_repeated_names(self, enqueue):
		result = queue_mappings_catalog_sync([self.mapping.name, self.mapping.name])

		self.assertEqual(result, {"queued": 1, "jobs": 1})
		enqueue.assert_called_once()
		self.assertEqual(enqueue.call_args.kwargs["mapping_names"], [self.mapping.name])

	@patch("marketplace_integrations.api.talabat_catalog.frappe.enqueue")
	def test_queue_mappings_checks_all_permissions_before_enqueueing(self, enqueue):
		second_mapping = frappe.get_doc(
			{
				"doctype": "Marketplace Product Mapping",
				"marketplace_account": self.account.name,
				"item": self.item.name,
				"external_reference": f"catalog-api-permission-sku-{frappe.generate_hash(length=8)}",
			}
		).insert()
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": f"bulk-mapping-sync-{frappe.generate_hash(length=10)}@example.com",
				"first_name": "Bulk Mapping Sync Test User",
				"send_welcome_email": 0,
				"roles": [{"role": "All"}],
			}
		).insert()
		# Grant the test user real write permission on the *first* mapping (and its
		# account) via an explicit share, but not on the second mapping - neither
		# role has any permission on this doctype otherwise. This proves the
		# implementation checks every selected mapping's permission before
		# enqueueing anything, rather than checking-and-enqueueing one at a time
		# (which would have already enqueued a job for the permitted mapping by
		# the time the second mapping's permission check fails).
		frappe.share.add("Marketplace Product Mapping", self.mapping.name, user.name, write=1)
		frappe.share.add("Marketplace Account", self.account.name, user.name, write=1)
		enqueue.reset_mock()

		with self.set_user(user.name):
			with self.assertRaises(frappe.PermissionError):
				queue_mappings_catalog_sync([self.mapping.name, second_mapping.name])
		enqueue.assert_not_called()

	@patch("marketplace_integrations.api.talabat_catalog.frappe.enqueue")
	def test_queue_mappings_pins_original_per_mapping_error_order_and_text(self, enqueue):
		"""Pins Talabat's pre-#106 behavior (#106 requires no behavior change):
		checks run per mapping in order, so a permission failure on the first
		mapping wins over a later missing one, with Frappe's own message."""
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": f"order-pin-{frappe.generate_hash(length=10)}@example.com",
				"first_name": "Order Pin Test User",
				"send_welcome_email": 0,
				"roles": [{"role": "All"}],
			}
		).insert()
		enqueue.reset_mock()

		with self.set_user(user.name):
			frappe.clear_messages()
			with self.assertRaises(frappe.PermissionError):
				queue_mappings_catalog_sync([self.mapping.name, "MPM-DOES-NOT-EXIST"])
			message = frappe.message_log[-1]["message"] if frappe.message_log else ""
		self.assertIn("permission", message.lower())
		self.assertIn("Marketplace Product Mapping", message)
		enqueue.assert_not_called()

	@patch("marketplace_integrations.api.talabat_catalog.frappe.enqueue")
	def test_queue_mapping_requires_mapping_write_permission(self, enqueue):
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": f"mapping-sync-{frappe.generate_hash(length=10)}@example.com",
				"first_name": "Mapping Sync Test User",
				"send_welcome_email": 0,
				"roles": [{"role": "All"}],
			}
		).insert()
		enqueue.reset_mock()

		with self.set_user(user.name):
			with self.assertRaises(frappe.PermissionError):
				queue_mapping_catalog_sync(self.mapping.name)
		enqueue.assert_not_called()

	@patch("marketplace_integrations.api.talabat_catalog.frappe.enqueue")
	def test_queue_mapping_rejects_an_existing_group_warehouse(self, enqueue):
		company = frappe.defaults.get_global_default("company")
		group_warehouse = frappe.get_doc(
			{
				"doctype": "Warehouse",
				"warehouse_name": f"Catalog API Group {frappe.generate_hash(length=8)}",
				"company": company,
				"is_group": 1,
			}
		).insert()
		frappe.db.set_value("Marketplace Account", self.account.name, "warehouse", group_warehouse.name)

		with self.assertRaisesRegex(frappe.ValidationError, "leaf Warehouse"):
			queue_mapping_catalog_sync(self.mapping.name)
		enqueue.assert_not_called()

	def test_no_catalog_document_events_are_registered(self):
		doc_events = frappe.get_hooks("doc_events")
		for doctype in (
			"Stock Ledger Entry",
			"Item Price",
			"Item",
			"Marketplace Product Mapping",
			"Marketplace Account",
		):
			self.assertNotIn(doctype, doc_events)
