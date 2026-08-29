# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from talabat_integration.api.catalog import get_vendor_catalog_page, get_vendor_products, upsert_mapping

TEST_ITEM = "TEST-CATALOG-ITEM"


def _response(body, status_code=200):
	response = MagicMock()
	response.status_code = status_code
	response.ok = status_code < 400
	response.json.return_value = body
	return response


class IntegrationTestCatalog(IntegrationTestCase):
	def setUp(self):
		settings = frappe.get_single("Talabat Settings")
		settings.client_id = "test-client-id"
		settings.client_secret = "test-client-secret"
		settings.chain_id = "test-chain-id"
		settings.environment = "Sandbox"
		settings.save()

		self.vendor = frappe.get_doc(
			{
				"doctype": "Talabat Vendor",
				"vendor_id": "test-vendor-catalog",
				"vendor_name": "Catalog Test Outlet",
			}
		).insert(ignore_if_duplicate=True)

	@patch("talabat_integration.api.catalog.talabat_request")
	def test_returns_sku_title_pairs(self, mock_request):
		mock_request.return_value = _response(
			{
				"page_number": 1,
				"total_pages": 1,
				"products": [
					{"sku": "123", "title": "Chips"},
					{"sku": "456", "title": "Soda"},
				],
			}
		)

		result = get_vendor_products(self.vendor.name)

		self.assertEqual(
			result["products"], [{"sku": "123", "title": "Chips"}, {"sku": "456", "title": "Soda"}]
		)
		self.assertFalse(result["truncated"])

		args, kwargs = mock_request.call_args
		self.assertEqual(args[0], "GET")
		self.assertIn(f"/vendors/{self.vendor.vendor_id}/catalog", args[1])
		self.assertIn("test-chain-id", args[1])
		self.assertEqual(kwargs["params"]["page_size"], 500)

	@patch("talabat_integration.api.catalog.talabat_request")
	def test_empty_catalog_returns_empty_list(self, mock_request):
		mock_request.return_value = _response({"page_number": 1, "total_pages": 1, "products": []})

		result = get_vendor_products(self.vendor.name)

		self.assertEqual(result["products"], [])
		self.assertFalse(result["truncated"])

	@patch("talabat_integration.api.catalog.talabat_request")
	def test_more_than_one_page_marks_truncated(self, mock_request):
		mock_request.return_value = _response(
			{"page_number": 1, "total_pages": 3, "products": [{"sku": "1", "title": "A"}]}
		)

		result = get_vendor_products(self.vendor.name)

		self.assertTrue(result["truncated"])

	@patch("talabat_integration.api.catalog.talabat_request")
	def test_failed_response_raises_clear_error(self, mock_request):
		mock_request.return_value = _response({}, status_code=500)

		with self.assertRaises(frappe.ValidationError):
			get_vendor_products(self.vendor.name)

	def test_unknown_vendor_raises_clear_error(self):
		with self.assertRaises(frappe.DoesNotExistError):
			get_vendor_products("does-not-exist")

	@patch("talabat_integration.api.catalog.talabat_request")
	def test_missing_sku_or_title_does_not_crash(self, mock_request):
		mock_request.return_value = _response(
			{
				"page_number": 1,
				"total_pages": 1,
				"products": [{"sku": "123"}, {"title": "No SKU"}, {}],
			}
		)

		result = get_vendor_products(self.vendor.name)

		self.assertEqual(
			result["products"],
			[
				{"sku": "123", "title": None},
				{"sku": None, "title": "No SKU"},
				{"sku": None, "title": None},
			],
		)

	@patch("talabat_integration.api.catalog.talabat_request")
	def test_catalog_page_marks_mapped_status(self, mock_request):
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

		vendor = frappe.get_doc(
			{"doctype": "Talabat Vendor", "vendor_id": "test-vendor-catalog-page"}
		).insert(ignore_if_duplicate=True)

		frappe.get_doc(
			{
				"doctype": "Talabat Product Mapping",
				"vendor": vendor.name,
				"talabat_sku": "123",
				"item": TEST_ITEM,
			}
		).insert(ignore_if_duplicate=True)

		mock_request.return_value = _response(
			{
				"page_number": 1,
				"total_pages": 1,
				"products": [
					{"sku": "123", "title": "Chips", "price": 10},
					{"sku": "999-unmapped", "title": "Soda", "price": 5},
				],
			}
		)

		result = get_vendor_catalog_page(vendor.name)

		by_sku = {row["sku"]: row for row in result["rows"]}
		self.assertEqual(by_sku["123"]["item"], TEST_ITEM)
		self.assertIsNone(by_sku["999-unmapped"]["item"])
		self.assertFalse(result["has_more"])

	@patch("talabat_integration.api.catalog.talabat_request")
	def test_catalog_page_passes_page_and_page_size(self, mock_request):
		mock_request.return_value = _response({"page_number": 2, "total_pages": 3, "products": []})

		result = get_vendor_catalog_page(self.vendor.name, page=2, page_size=50)

		params = mock_request.call_args.kwargs["params"]
		self.assertEqual(params["page"], 2)
		self.assertEqual(params["page_size"], 50)
		self.assertTrue(result["has_more"])

	@patch("talabat_integration.api.catalog.talabat_request")
	def test_catalog_page_clamps_page_size_to_talabat_limits(self, mock_request):
		mock_request.return_value = _response({"page_number": 1, "total_pages": 1, "products": []})

		get_vendor_catalog_page(self.vendor.name, page_size=1000)
		self.assertEqual(mock_request.call_args.kwargs["params"]["page_size"], 500)

		get_vendor_catalog_page(self.vendor.name, page_size=0)
		self.assertEqual(mock_request.call_args.kwargs["params"]["page_size"], 1)

		# default (no page_size arg) is the grid's default page size
		get_vendor_catalog_page(self.vendor.name)
		self.assertEqual(mock_request.call_args.kwargs["params"]["page_size"], 20)

	@patch("talabat_integration.api.catalog.talabat_request")
	def test_catalog_page_returns_mappings_across_all_skus(self, mock_request):
		item = "TEST-CATALOG-ITEM-2"
		if not frappe.db.exists("Item", item):
			frappe.get_doc(
				{
					"doctype": "Item",
					"item_code": item,
					"item_group": "Products",
					"stock_uom": "Nos",
					"is_stock_item": 0,
				}
			).insert()

		vendor = frappe.get_doc(
			{"doctype": "Talabat Vendor", "vendor_id": "test-vendor-mappings-list"}
		).insert(ignore_if_duplicate=True)

		# mapped to a SKU that is NOT on the requested catalog page
		frappe.get_doc(
			{
				"doctype": "Talabat Product Mapping",
				"vendor": vendor.name,
				"talabat_sku": "off-page-sku",
				"item": item,
			}
		).insert(ignore_if_duplicate=True)

		mock_request.return_value = _response(
			{
				"page_number": 1,
				"total_pages": 1,
				"products": [{"sku": "on-page-sku", "title": "Chips", "price": 10}],
			}
		)

		result = get_vendor_catalog_page(vendor.name)

		# the mapping page uses this list to keep mapped Items out of the picker
		self.assertEqual(result["mappings"], [{"talabat_sku": "off-page-sku", "item": item}])
		# on-page rows without a mapping still come back unmapped
		self.assertIsNone(result["rows"][0]["item"])

	def test_upsert_mapping_creates_then_updates(self):
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
		other_item = "TEST-CATALOG-ITEM-2"
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

		result = upsert_mapping(self.vendor.name, "upsert-test-sku", TEST_ITEM)
		mapping = frappe.get_doc("Talabat Product Mapping", result["name"])
		self.assertEqual(mapping.item, TEST_ITEM)

		result2 = upsert_mapping(self.vendor.name, "upsert-test-sku", other_item)
		self.assertEqual(result2["name"], result["name"])
		mapping.reload()
		self.assertEqual(mapping.item, other_item)

	def test_upsert_mapping_rejects_item_already_mapped_to_another_sku(self):
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

		sku_suffix = frappe.generate_hash(length=10)
		first_sku = f"duplicate-item-first-{sku_suffix}"
		second_sku = f"duplicate-item-second-{sku_suffix}"
		upsert_mapping(self.vendor.name, first_sku, TEST_ITEM)

		with self.assertRaises(frappe.ValidationError):
			upsert_mapping(self.vendor.name, second_sku, TEST_ITEM)

		self.assertTrue(
			frappe.db.exists(
				"Talabat Product Mapping",
				{"vendor": self.vendor.name, "talabat_sku": first_sku, "item": TEST_ITEM},
			)
		)
		self.assertFalse(
			frappe.db.exists(
				"Talabat Product Mapping", {"vendor": self.vendor.name, "talabat_sku": second_sku}
			)
		)

	def test_non_system_manager_cannot_fetch(self):
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": "talabat-catalog-test-user@example.com",
				"first_name": "Catalog Test User",
				"send_welcome_email": 0,
				"roles": [{"role": "All"}],
			}
		).insert(ignore_if_duplicate=True)

		with self.set_user(user.name):
			with self.assertRaises(frappe.PermissionError):
				get_vendor_products(self.vendor.name)
