# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.integrations.talabat.catalog import (
	disable_mapping,
	get_catalog_page,
	get_mapped_items,
	upsert_mapping,
)


def _response(body, status_code=200):
	response = MagicMock()
	response.ok = status_code < 400
	response.status_code = status_code
	response.json.return_value = body
	return response


class IntegrationTestTalabatCatalog(IntegrationTestCase):
	def setUp(self):
		suffix = frappe.generate_hash(length=10)
		self.item = self._make_item(f"TEST-MKT-CATALOG-{suffix}")
		self.other_item = self._make_item(f"TEST-MKT-CATALOG-OTHER-{suffix}")
		self.connection = frappe.get_doc(
			{
				"doctype": "Talabat Connection",
				"connection_name": f"Test Catalog Connection {suffix}",
				"client_id": "test-client-id",
				"client_secret": "test-client-secret",
				"chain_id": "test-chain-id",
				"environment": "Sandbox",
			}
		).insert()
		self.account = frappe.get_doc(
			{
				"doctype": "Marketplace Account",
				"account_name": f"Test Catalog Account {suffix}",
				"platform": "Talabat",
				"external_account_id": f"test-vendor-{suffix}",
				"connection_doctype": "Talabat Connection",
				"connection": self.connection.name,
			}
		).insert()

	def tearDown(self):
		frappe.db.delete("Marketplace Product Mapping", {"marketplace_account": self.account.name})
		frappe.delete_doc("Marketplace Account", self.account.name, ignore_permissions=True, force=True)
		frappe.delete_doc("Talabat Connection", self.connection.name, ignore_permissions=True, force=True)

	def _make_item(self, item_code):
		return frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": item_code,
				"item_group": "Products",
				"stock_uom": "Nos",
				"is_stock_item": 0,
			}
		).insert()

	@patch("marketplace_integrations.integrations.talabat.catalog.TalabatClient.request")
	def test_catalog_page_uses_account_connection_and_marks_current_mapping(self, mock_request):
		upsert_mapping(self.account.name, "mapped-sku", self.item.name)
		mock_request.return_value = _response(
			{
				"page_number": 2,
				"total_pages": 3,
				"products": [
					{"sku": "mapped-sku", "title": "<Unsafe title>", "price": 10},
					{"sku": "unmapped-sku", "title": "Soda", "price": 5},
				],
			}
		)

		result = get_catalog_page(self.account.name, page=2, page_size=50)

		self.assertEqual(result["rows"][0]["item"], self.item.name)
		self.assertIsNone(result["rows"][1]["item"])
		self.assertTrue(result["has_more"])
		args, kwargs = mock_request.call_args
		self.assertEqual(args[0], "GET")
		self.assertIn(
			f"/chains/{self.connection.chain_id}/vendors/{self.account.external_account_id}/catalog", args[1]
		)
		self.assertEqual(kwargs["params"], {"page": 2, "page_size": 50})

	@patch("marketplace_integrations.integrations.talabat.catalog.TalabatClient.request")
	def test_catalog_page_clamps_talabat_pagination_bounds(self, mock_request):
		mock_request.return_value = _response({"products": [], "total_pages": 1})

		get_catalog_page(self.account.name, page="not-a-number", page_size=1000)

		self.assertEqual(mock_request.call_args.kwargs["params"], {"page": 1, "page_size": 500})

	def test_upsert_allows_one_item_to_map_to_multiple_listings(self):
		first = upsert_mapping(self.account.name, "sku-one", self.item.name)
		second = upsert_mapping(self.account.name, "sku-two", self.item.name)

		self.assertNotEqual(first["name"], second["name"])
		self.assertEqual(
			get_mapped_items(self.account.name), {"sku-one": self.item.name, "sku-two": self.item.name}
		)

	def test_upsert_updates_existing_listing_mapping(self):
		first = upsert_mapping(self.account.name, "same-sku", self.item.name)
		second = upsert_mapping(self.account.name, "same-sku", self.other_item.name)

		self.assertEqual(first["name"], second["name"])
		self.assertEqual(
			frappe.get_doc("Marketplace Product Mapping", first["name"]).item, self.other_item.name
		)

	def test_upsert_case_variant_creates_a_new_mapping_and_keeps_the_original(self):
		first = upsert_mapping(self.account.name, "abc-1", self.item.name)
		second = upsert_mapping(self.account.name, "ABC-1", self.other_item.name)

		self.assertNotEqual(first["name"], second["name"])
		self.assertEqual(frappe.get_doc("Marketplace Product Mapping", first["name"]).item, self.item.name)
		self.assertEqual(
			frappe.get_doc("Marketplace Product Mapping", second["name"]).item, self.other_item.name
		)

	def test_disable_mapping_preserves_history_and_upsert_reenables_it(self):
		created = upsert_mapping(self.account.name, "disable-me", self.item.name)

		result = disable_mapping(self.account.name, "disable-me")
		mapping = frappe.get_doc("Marketplace Product Mapping", created["name"])

		self.assertEqual(result, {"name": created["name"], "enabled": 0})
		self.assertEqual(mapping.enabled, 0)
		self.assertEqual(mapping.item, self.item.name)
		self.assertEqual(get_mapped_items(self.account.name), {})

		reenabled = upsert_mapping(self.account.name, "disable-me", self.other_item.name)
		mapping.reload()
		self.assertEqual(reenabled["name"], created["name"])
		self.assertEqual(mapping.enabled, 1)
		self.assertEqual(mapping.item, self.other_item.name)

	def test_disable_mapping_uses_case_sensitive_identity_key(self):
		lower = upsert_mapping(self.account.name, "abc-1", self.item.name)
		upper = upsert_mapping(self.account.name, "ABC-1", self.other_item.name)
		self.assertNotEqual(lower["name"], upper["name"])

		result = disable_mapping(self.account.name, "ABC-1")

		self.assertEqual(result["name"], upper["name"])
		self.assertEqual(frappe.db.get_value("Marketplace Product Mapping", upper["name"], "enabled"), 0)
		self.assertEqual(frappe.db.get_value("Marketplace Product Mapping", lower["name"], "enabled"), 1)

	def test_disable_mapping_is_idempotent_and_rejects_missing_or_disabled_account_or_mapping(self):
		created = upsert_mapping(self.account.name, "disable-me", self.item.name)

		with self.assertRaises(frappe.DoesNotExistError):
			disable_mapping("missing-account", "disable-me")

		with self.assertRaises(frappe.ValidationError):
			disable_mapping(self.account.name, "unknown-sku")

		self.assertEqual(
			disable_mapping(self.account.name, "disable-me"),
			{"name": created["name"], "enabled": 0},
		)
		self.assertEqual(
			disable_mapping(self.account.name, "disable-me"),
			{"name": created["name"], "enabled": 0},
		)

		self.account.enabled = 0
		self.account.save()
		with self.assertRaises(frappe.ValidationError):
			disable_mapping(self.account.name, "disable-me")

	def test_non_system_manager_cannot_browse_or_save(self):
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": f"catalog-{frappe.generate_hash(length=10)}@example.com",
				"first_name": "Catalog Test User",
				"send_welcome_email": 0,
				"roles": [{"role": "All"}],
			}
		).insert()

		with self.set_user(user.name):
			with self.assertRaises(frappe.PermissionError):
				get_catalog_page(self.account.name)
			with self.assertRaises(frappe.PermissionError):
				upsert_mapping(self.account.name, "unauthorized-sku", self.item.name)
