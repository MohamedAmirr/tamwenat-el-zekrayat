# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from talabat_integration.api.catalog import get_vendor_products


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
