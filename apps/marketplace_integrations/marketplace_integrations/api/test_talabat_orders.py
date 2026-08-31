# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.api.talabat_orders import (
	import_vendor_orders,
	refresh_order,
)
from marketplace_integrations.services.marketplace_orders import upsert_marketplace_order


def _response(body, status_code=200):
	"""Helper to mock HTTP responses."""
	response = MagicMock()
	response.ok = status_code < 400
	response.status_code = status_code
	response.json.return_value = body
	return response


def _make_connection(connection_name, **kwargs):
	"""Helper to create a test Talabat Connection."""
	doc = {
		"doctype": "Talabat Connection",
		"connection_name": connection_name,
		"client_id": "test-client-id",
		"client_secret": "test-client-secret",
		"chain_id": "test-chain-id",
		"environment": "Sandbox",
		"enabled": 1,
	}
	doc.update(kwargs)
	return frappe.get_doc(doc).insert()


def _make_account(account_name, connection, **kwargs):
	"""Helper to create a test Marketplace Account."""
	doc = {
		"doctype": "Marketplace Account",
		"account_name": account_name,
		"platform": "Talabat",
		"external_account_id": f"test-vendor-{account_name}",
		"connection_doctype": "Talabat Connection",
		"connection": connection.name,
		"enabled": 1,
	}
	doc.update(kwargs)
	return frappe.get_doc(doc).insert()


def _sample_normalized_order(order_id="order-001", **overrides):
	"""Helper to create a normalized order for upsert."""
	order = {
		"external_order_id": order_id,
		"platform_order_reference": f"MERCH-{order_id}",
		"order_code": f"CODE-{order_id}",
		"external_status": "RECEIVED",
		"order_type": "DELIVERY",
		"transport_type": "LOGISTICS_DELIVERY",
		"is_preorder": 0,
		"accepted_for": "2026-08-31 13:00:00",
		"promised_for": "2026-08-31 13:30:00",
		"received_on": "2026-08-31 12:00:00",
		"last_platform_update_on": "2026-08-31 12:05:00",
		"cancelled_by": None,
		"cancellation_reason": None,
		"post_picked_up": 0,
		"customer_external_id": "customer-001",
		"customer_first_name": "John",
		"customer_last_name": "Doe",
		"customer_phone_number": "1234567890",
		"customer_formatted_address": "123 Main St",
		"customer_city": "Cairo",
		"customer_latitude": 30.0,
		"customer_longitude": 31.0,
		"payment_type": "ONLINE",
		"sub_total": 100.0,
		"discount": -5.0,
		"order_total": 110.0,
		"total_taxes": 2.0,
		"delivery_fee": 10.0,
		"service_fee": 3.0,
		"container_charge": 0.0,
		"difference_to_minimum": 0.0,
		"collect_at_pickup": None,
		"discounts": [],
		"additional_fees": {},
		"latest_payload": {"order_id": order_id},
		"items": [],
	}
	order.update(overrides)
	return order


class IntegrationTestRefreshOrder(IntegrationTestCase):
	"""Tests for refresh_order API."""

	def setUp(self):
		suffix = frappe.generate_hash(length=8)
		self.connection = _make_connection(f"Test Refresh Connection {suffix}")
		self.account = _make_account(f"Test Refresh Account {suffix}", self.connection)
		self.order = upsert_marketplace_order(self.account.name, _sample_normalized_order())

	@patch("marketplace_integrations.api.talabat_orders.talabat_orders.get_order")
	def test_refresh_order_fetches_and_updates_by_external_order_id(self, mock_get_order):
		"""refresh_order fetches the order by stored external_order_id."""
		mock_get_order.return_value = {
			"order_id": "order-001",
			"external_order_id": "MERCH-order-001",
			"order_code": "CODE-001",
			"status": "READY_FOR_PICKUP",
			"customer": {"delivery_address": {}},
			"payment": {},
			"sys": {},
			"cancellation": {},
			"items": [],
		}

		result = refresh_order(self.order.name)

		self.assertIn("name", result)
		mock_get_order.assert_called_once_with(self.account.name, "order-001")

	@patch("marketplace_integrations.api.talabat_orders.talabat_orders.get_order")
	def test_refresh_order_validates_order_id_matches(self, mock_get_order):
		"""Refreshed order must have the same external_order_id."""
		mock_get_order.return_value = {
			"order_id": "different-order-id",
			"external_order_id": "MERCH-different",
			"customer": {"delivery_address": {}},
			"payment": {},
			"sys": {},
			"cancellation": {},
			"items": [],
		}

		with self.assertRaises(frappe.ValidationError) as cm:
			refresh_order(self.order.name)
		self.assertIn("does not match", str(cm.exception))

	@patch("marketplace_integrations.api.talabat_orders.talabat_orders.get_order")
	def test_refresh_order_requires_write_permission(self, mock_get_order):
		"""refresh_order checks write permission on the Marketplace Order."""
		mock_get_order.return_value = {
			"order_id": "order-001",
			"external_order_id": "MERCH-order-001",
			"customer": {"delivery_address": {}},
			"payment": {},
			"sys": {},
			"cancellation": {},
			"items": [],
		}
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": f"refresh-test-{frappe.generate_hash(length=10)}@example.com",
				"first_name": "Refresh Test User",
				"send_welcome_email": 0,
				"roles": [{"role": "All"}],
			}
		).insert()

		with self.set_user(user.name):
			with self.assertRaises(frappe.PermissionError):
				refresh_order(self.order.name)

	@patch("marketplace_integrations.api.talabat_orders.talabat_orders.get_order")
	def test_refresh_order_returns_updated_document_name(self, mock_get_order):
		"""refresh_order returns the updated Marketplace Order name."""
		mock_get_order.return_value = {
			"order_id": "order-001",
			"external_order_id": "MERCH-order-001",
			"order_code": "CODE-001",
			"status": "READY_FOR_PICKUP",
			"customer": {"delivery_address": {}},
			"payment": {},
			"sys": {},
			"cancellation": {},
			"items": [],
		}

		result = refresh_order(self.order.name)

		self.assertEqual(result["name"], self.order.name)
		self.order.reload()
		self.assertEqual(self.order.external_status, "READY_FOR_PICKUP")


class IntegrationTestImportVendorOrders(IntegrationTestCase):
	"""Tests for import_vendor_orders API."""

	def setUp(self):
		suffix = frappe.generate_hash(length=8)
		self.connection = _make_connection(f"Test Import Connection {suffix}")
		self.account = _make_account(f"Test Import Account {suffix}", self.connection)

	@patch("marketplace_integrations.api.talabat_orders.talabat_orders.get_vendor_orders")
	def test_import_vendor_orders_requires_account_read_permission(self, mock_get_vendor_orders):
		"""import_vendor_orders checks read permission on the account."""
		mock_get_vendor_orders.return_value = {
			"orders": [],
			"page_number": 1,
			"page_size": 20,
			"total_pages": 1,
		}
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": f"import-test-{frappe.generate_hash(length=10)}@example.com",
				"first_name": "Import Test User",
				"send_welcome_email": 0,
				"roles": [{"role": "All"}],
			}
		).insert()

		with self.set_user(user.name):
			with self.assertRaises(frappe.PermissionError):
				import_vendor_orders(self.account.name, "2026-08-01", "2026-08-31")

	@patch("marketplace_integrations.api.talabat_orders.talabat_orders.get_vendor_orders")
	def test_import_vendor_orders_requires_marketplace_order_create_permission(
		self, mock_get_vendor_orders
	):
		"""import_vendor_orders requires create permission on Marketplace Order,
		even for a user who can read the Marketplace Account (i.e. the account
		read check alone must not be enough to import)."""
		mock_get_vendor_orders.return_value = {
			"orders": [
				{
					"order_id": "order-001",
					"customer": {"delivery_address": {}},
					"payment": {},
					"sys": {},
					"cancellation": {},
					"items": [],
				}
			],
			"page_number": 1,
			"page_size": 20,
			"total_pages": 1,
		}
		role_name = f"Import No Create {frappe.generate_hash(length=8)}"
		frappe.get_doc({"doctype": "Role", "role_name": role_name}).insert()
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": f"import-no-create-{frappe.generate_hash(length=10)}@example.com",
				"first_name": "Import No Create Test User",
				"send_welcome_email": 0,
				"roles": [{"role": role_name}],
			}
		).insert()
		had_account_custom_perms = frappe.db.exists("Custom DocPerm", {"parent": "Marketplace Account"})
		had_order_custom_perms = frappe.db.exists("Custom DocPerm", {"parent": "Marketplace Order"})
		account_perm_name = frappe.permissions.add_permission("Marketplace Account", role_name, ptype="read")
		order_perm_name = frappe.permissions.add_permission("Marketplace Order", role_name, ptype="read")
		frappe.clear_cache(user=user.name, doctype="Marketplace Account")
		frappe.clear_cache(user=user.name, doctype="Marketplace Order")
		try:
			with self.set_user(user.name):
				self.assertTrue(frappe.has_permission("Marketplace Account", "read"))
				self.assertFalse(frappe.has_permission("Marketplace Order", "create"))
				with self.assertRaises(frappe.PermissionError):
					import_vendor_orders(self.account.name, "2026-08-01", "2026-08-31")
		finally:
			frappe.delete_doc("Custom DocPerm", account_perm_name, force=True)
			frappe.delete_doc("Custom DocPerm", order_perm_name, force=True)
			if not had_account_custom_perms:
				frappe.db.delete("Custom DocPerm", {"parent": "Marketplace Account"})
			if not had_order_custom_perms:
				frappe.db.delete("Custom DocPerm", {"parent": "Marketplace Order"})
			frappe.delete_doc("User", user.name, force=True)
			frappe.delete_doc("Role", role_name, force=True)
			frappe.clear_cache(user=user.name, doctype="Marketplace Account")
			frappe.clear_cache(user=user.name, doctype="Marketplace Order")

	@patch("marketplace_integrations.api.talabat_orders.talabat_orders.get_vendor_orders")
	def test_import_vendor_orders_requires_date_range(self, mock_get_vendor_orders):
		"""import_vendor_orders requires both start and end dates."""
		with self.assertRaises(frappe.ValidationError):
			import_vendor_orders(self.account.name, "", "2026-08-31")

		with self.assertRaises(frappe.ValidationError):
			import_vendor_orders(self.account.name, "2026-08-01", "   ")

	@patch("marketplace_integrations.api.talabat_orders.talabat_orders.get_vendor_orders")
	def test_import_vendor_orders_paginates_until_total_pages(self, mock_get_vendor_orders):
		"""import_vendor_orders fetches all pages until total_pages is exhausted."""
		mock_get_vendor_orders.side_effect = [
			{
				"orders": [
					{
						"order_id": "order-001",
						"external_order_id": "MERCH-001",
						"customer": {"delivery_address": {}},
						"payment": {},
						"sys": {},
						"cancellation": {},
						"items": [],
					}
				],
				"page_number": 1,
				"page_size": 20,
				"total_pages": 2,
			},
			{
				"orders": [
					{
						"order_id": "order-002",
						"external_order_id": "MERCH-002",
						"customer": {"delivery_address": {}},
						"payment": {},
						"sys": {},
						"cancellation": {},
						"items": [],
					}
				],
				"page_number": 2,
				"page_size": 20,
				"total_pages": 2,
			},
		]

		result = import_vendor_orders(self.account.name, "2026-08-01", "2026-08-31")

		self.assertEqual(len(result["imported"]), 2)
		self.assertEqual(result["count"], 2)
		self.assertEqual(mock_get_vendor_orders.call_count, 2)
		# Verify page numbers increment
		calls = mock_get_vendor_orders.call_args_list
		self.assertEqual(calls[0][1]["page"], 1)
		self.assertEqual(calls[1][1]["page"], 2)

	@patch("marketplace_integrations.api.talabat_orders.talabat_orders.get_vendor_orders")
	def test_import_vendor_orders_stops_at_total_pages(self, mock_get_vendor_orders):
		"""import_vendor_orders stops fetching when page >= total_pages."""
		mock_get_vendor_orders.return_value = {
			"orders": [
				{
					"order_id": "order-001",
					"external_order_id": "MERCH-001",
					"customer": {"delivery_address": {}},
					"payment": {},
					"sys": {},
					"cancellation": {},
					"items": [],
				}
			],
			"page_number": 1,
			"page_size": 20,
			"total_pages": 1,
		}

		result = import_vendor_orders(self.account.name, "2026-08-01", "2026-08-31")

		# Should only fetch once since page 1 >= total_pages 1
		self.assertEqual(mock_get_vendor_orders.call_count, 1)
		self.assertEqual(result["count"], 1)

	@patch("marketplace_integrations.api.talabat_orders.talabat_orders.get_vendor_orders")
	def test_import_vendor_orders_is_idempotent(self, mock_get_vendor_orders):
		"""Re-importing the same date range creates no duplicate Marketplace Orders."""
		order_payload = {
			"order_id": "order-001",
			"external_order_id": "MERCH-001",
			"order_code": "CODE-001",
			"customer": {"delivery_address": {"city": "Cairo"}},
			"payment": {},
			"sys": {},
			"cancellation": {},
			"items": [],
		}
		mock_get_vendor_orders.return_value = {
			"orders": [order_payload],
			"page_number": 1,
			"page_size": 20,
			"total_pages": 1,
		}

		# First import
		result1 = import_vendor_orders(self.account.name, "2026-08-01", "2026-08-31")
		self.assertEqual(result1["count"], 1)
		order_name = result1["imported"][0]

		# Reload and verify
		order1 = frappe.get_doc("Marketplace Order", order_name)
		self.assertEqual(order1.external_order_id, "order-001")

		# Second import of same date range
		result2 = import_vendor_orders(self.account.name, "2026-08-01", "2026-08-31")
		self.assertEqual(result2["count"], 1)
		self.assertEqual(result2["imported"][0], order_name)

		# Verify still only one order exists
		count = frappe.db.count(
			"Marketplace Order",
			{
				"marketplace_account": self.account.name,
				"external_order_id": "order-001",
			},
		)
		self.assertEqual(count, 1)

	@patch("marketplace_integrations.api.talabat_orders.talabat_orders.get_vendor_orders")
	def test_import_vendor_orders_passes_date_range_to_retrieval(self, mock_get_vendor_orders):
		"""Date range parameters are passed through to get_vendor_orders."""
		mock_get_vendor_orders.return_value = {
			"orders": [],
			"page_number": 1,
			"page_size": 20,
			"total_pages": 1,
		}

		import_vendor_orders(self.account.name, "2026-08-01T00:00:00Z", "2026-08-31T23:59:59Z")

		call_kwargs = mock_get_vendor_orders.call_args[1]
		self.assertEqual(call_kwargs["start_created_at_datetime"], "2026-08-01T00:00:00Z")
		self.assertEqual(call_kwargs["end_created_at_datetime"], "2026-08-31T23:59:59Z")

	@patch("marketplace_integrations.api.talabat_orders.talabat_orders.get_vendor_orders")
	def test_import_vendor_orders_uses_max_page_size(self, mock_get_vendor_orders):
		"""import_vendor_orders uses MAX_PAGE_SIZE for all requests."""
		mock_get_vendor_orders.return_value = {
			"orders": [],
			"page_number": 1,
			"page_size": 500,
			"total_pages": 1,
		}

		import_vendor_orders(self.account.name, "2026-08-01", "2026-08-31")

		call_kwargs = mock_get_vendor_orders.call_args[1]
		from marketplace_integrations.integrations.talabat import orders as talabat_orders_module
		self.assertEqual(call_kwargs["page_size"], talabat_orders_module.MAX_PAGE_SIZE)

	@patch("marketplace_integrations.api.talabat_orders.talabat_orders.get_vendor_orders")
	def test_import_vendor_orders_normalizes_and_upserts_each_order(self, mock_get_vendor_orders):
		"""Each raw Talabat order is normalized and upserted."""
		order_payloads = [
			{
				"order_id": f"order-{i:03d}",
				"external_order_id": f"MERCH-{i:03d}",
				"order_code": f"CODE-{i:03d}",
				"status": "RECEIVED",
				"customer": {"_id": f"customer-{i}", "delivery_address": {"city": "Cairo"}},
				"payment": {"order_total": float(100 * (i + 1))},
				"sys": {},
				"cancellation": {},
				"items": [],
			}
			for i in range(3)
		]
		mock_get_vendor_orders.return_value = {
			"orders": order_payloads,
			"page_number": 1,
			"page_size": 20,
			"total_pages": 1,
		}

		result = import_vendor_orders(self.account.name, "2026-08-01", "2026-08-31")

		self.assertEqual(result["count"], 3)
		self.assertEqual(len(result["imported"]), 3)

		# Verify all orders were created
		for i, order_name in enumerate(result["imported"]):
			order = frappe.get_doc("Marketplace Order", order_name)
			self.assertEqual(order.external_order_id, f"order-{i:03d}")
			self.assertEqual(order.marketplace_account, self.account.name)

	@patch("marketplace_integrations.api.talabat_orders.talabat_orders.get_vendor_orders")
	def test_import_vendor_orders_returns_imported_names_and_count(self, mock_get_vendor_orders):
		"""import_vendor_orders returns dict with imported names and count."""
		mock_get_vendor_orders.return_value = {
			"orders": [
				{
					"order_id": "order-001",
					"external_order_id": "MERCH-001",
					"customer": {"delivery_address": {}},
					"payment": {},
					"sys": {},
					"cancellation": {},
					"items": [],
				}
			],
			"page_number": 1,
			"page_size": 20,
			"total_pages": 1,
		}

		result = import_vendor_orders(self.account.name, "2026-08-01", "2026-08-31")

		self.assertIn("imported", result)
		self.assertIn("count", result)
		self.assertIn("errors", result)
		self.assertIsInstance(result["imported"], list)
		self.assertIsInstance(result["errors"], list)
		self.assertEqual(result["count"], len(result["imported"]))

	@patch("marketplace_integrations.api.talabat_orders.talabat_orders.get_vendor_orders")
	def test_import_vendor_orders_isolates_failing_orders_and_continues(self, mock_get_vendor_orders):
		"""A malformed order does not abort the rest of the batch: it's rolled
		back to its own savepoint and reported in errors, while orders
		encountered later in the same page still import successfully."""
		mock_get_vendor_orders.return_value = {
			"orders": [
				{
					"order_id": "",
					"customer": {"delivery_address": {}},
					"payment": {},
					"sys": {},
					"cancellation": {},
					"items": [],
				},
				{
					"order_id": "order-002",
					"external_order_id": "MERCH-002",
					"customer": {"delivery_address": {}},
					"payment": {},
					"sys": {},
					"cancellation": {},
					"items": [],
				},
			],
			"page_number": 1,
			"page_size": 20,
			"total_pages": 1,
		}

		result = import_vendor_orders(self.account.name, "2026-08-01", "2026-08-31")

		self.assertEqual(result["count"], 1)
		self.assertEqual(len(result["imported"]), 1)
		self.assertEqual(len(result["errors"]), 1)
		self.assertEqual(result["errors"][0]["order_id"], "")
		self.assertIn("order_id", result["errors"][0]["error"])

		order = frappe.get_doc("Marketplace Order", result["imported"][0])
		self.assertEqual(order.external_order_id, "order-002")
