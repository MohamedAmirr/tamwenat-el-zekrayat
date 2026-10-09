# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.api.talabat_orders import (
	import_vendor_orders,
	refresh_order,
	update_order,
)
from marketplace_integrations.services.marketplace_orders import upsert_marketplace_order
from marketplace_integrations.tests.utils import make_role_user


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


def _delete_account_fixtures(account):
	"""Delete everything a test class created for ``account``, in dependency order, and commit
	(make_role_user's cleanup commits, so fixtures must not be left to class-level rollback)."""
	connection = account.connection
	for doctype in ("Marketplace Order", "Marketplace Sync Log"):
		for name in frappe.get_all(doctype, filters={"marketplace_account": account.name}, pluck="name"):
			frappe.delete_doc(doctype, name, ignore_permissions=True, force=True)
	frappe.delete_doc("Marketplace Account", account.name, ignore_permissions=True, force=True)
	frappe.delete_doc("Talabat Connection", connection, ignore_permissions=True, force=True)
	frappe.db.commit()


class IntegrationTestRefreshOrder(IntegrationTestCase):
	"""Tests for refresh_order API."""

	def setUp(self):
		suffix = frappe.generate_hash(length=8)
		self.connection = _make_connection(f"Test Refresh Connection {suffix}")
		self.account = _make_account(f"Test Refresh Account {suffix}", self.connection)
		self.order = upsert_marketplace_order(self.account.name, _sample_normalized_order())

	def tearDown(self):
		_delete_account_fixtures(self.account)

	@patch("marketplace_integrations.api.talabat_orders.talabat_orders.update_order")
	def test_update_order_sends_selected_cancellation_reason(self, mock_update):
		mock_update.return_value = {
			"order_id": "order-001",
			"status": "CANCELLED",
			"items": [],
			"customer": {},
			"payment": {},
			"sys": {},
			"cancellation": {"reason": "CLOSED"},
		}
		result = update_order(self.order.name, "CANCELLED", "CLOSED")
		self.assertEqual(result["name"], self.order.name)
		payload = mock_update.call_args.args[2]
		self.assertEqual(payload["cancellation"], {"reason": "CLOSED"})

	@patch("marketplace_integrations.api.talabat_orders.talabat_orders.update_order")
	def test_update_order_rejects_missing_cancellation_reason_before_remote_call(self, mock_update):
		with self.assertRaises(frappe.ValidationError):
			update_order(self.order.name, "CANCELLED")
		mock_update.assert_not_called()

	@patch("marketplace_integrations.api.talabat_orders.talabat_orders.update_order")
	def test_update_order_rejects_cancelled_for_an_order_with_a_sales_order(self, mock_update):
		# set_value skips link validation; the guard only reads the field.
		frappe.db.set_value("Marketplace Order", self.order.name, "erpnext_sales_order", "SO-NOT-REAL-0001")
		with self.assertRaises(frappe.ValidationError) as ctx:
			update_order(self.order.name, "CANCELLED", "CLOSED")
		self.assertIn("Cancel Order button", str(ctx.exception))
		self.assertIn("request_platform_cancel", str(ctx.exception))
		mock_update.assert_not_called()

	@patch("marketplace_integrations.api.talabat_orders.upsert_marketplace_order")
	@patch("marketplace_integrations.api.talabat_orders.talabat_orders.update_order")
	def test_update_order_reconciles_successful_remote_response(self, mock_update, mock_upsert):
		mock_update.return_value = {
			"order_id": "order-001",
			"status": "CANCELLED",
			"items": [],
			"customer": {},
			"payment": {},
			"sys": {},
			"cancellation": {"reason": "CLOSED"},
		}
		mock_upsert.return_value = self.order
		result = update_order(self.order.name, "CANCELLED", "CLOSED")
		self.assertEqual(result["name"], self.order.name)
		mock_upsert.assert_called_once()

	@patch("marketplace_integrations.api.talabat_orders.upsert_marketplace_order")
	@patch(
		"marketplace_integrations.api.talabat_orders.talabat_orders.update_order",
		side_effect=frappe.ValidationError("remote"),
	)
	def test_update_order_remote_failure_does_not_upsert(self, mock_update, mock_upsert):
		with self.assertRaises(frappe.ValidationError):
			update_order(self.order.name, "CANCELLED", "CLOSED")
		mock_upsert.assert_not_called()

	def test_update_order_rejects_unit_quantity_above_original(self):
		self.order.append(
			"items",
			{
				"external_item_id": "item-1",
				"external_sku": "sku-1",
				"pricing_type": "UNIT",
				"original_quantity": 1,
				"fulfilled_quantity": 1,
				"external_status": "IN_CART",
			},
		)
		self.order.save()
		with self.assertRaises(frappe.ValidationError):
			update_order(
				self.order.name,
				"UPDATE_CART",
				items=[
					{"_id": "item-1", "status": "IN_CART", "pricing": {"pricing_type": "UNIT", "quantity": 2}}
				],
			)

	def test_update_order_rejects_kg_weight_above_original(self):
		self.order.append(
			"items",
			{
				"external_item_id": "item-kg",
				"external_sku": "sku-kg",
				"pricing_type": "KG",
				"original_weight": 1,
				"fulfilled_weight": 1,
				"external_status": "IN_CART",
			},
		)
		self.order.save()
		with self.assertRaises(frappe.ValidationError):
			update_order(
				self.order.name,
				"UPDATE_CART",
				items=[
					{"_id": "item-kg", "status": "IN_CART", "pricing": {"pricing_type": "KG", "weight": 2}}
				],
			)

	@patch("marketplace_integrations.api.talabat_orders.talabat_orders.update_order")
	def test_update_order_rejects_wrong_transport_and_terminal_status(self, mock_update):
		with self.assertRaises(frappe.ValidationError):
			update_order(self.order.name, "DISPATCHED")
		self.order.external_status = "DELIVERED"
		self.order.save()
		with self.assertRaises(frappe.ValidationError):
			update_order(self.order.name, "CANCELLED", "CLOSED")
		mock_update.assert_not_called()

	@patch("marketplace_integrations.api.talabat_orders.upsert_marketplace_order")
	@patch("marketplace_integrations.integrations.talabat.orders.TalabatClient.request")
	def test_update_order_rejects_response_from_different_vendor(self, mock_request, mock_upsert):
		"""A mismatched vendor in Talabat's PUT response is rejected before upsert."""
		mock_request.return_value = _response(
			{
				"order_id": "order-001",
				"client": {"id": "different-vendor", "store_id": "different-store"},
			}
		)
		with self.assertRaises(frappe.ValidationError):
			update_order(self.order.name, "CANCELLED", "CLOSED")
		mock_request.assert_called_once()
		self.assertEqual(mock_request.call_args.args[0], "PUT")
		mock_upsert.assert_not_called()

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
		email, _role = make_role_user(
			self,
			"Refresh No Write",
			{"Marketplace Order": ["read"]},
		)

		with self.set_user(email):
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

	def tearDown(self):
		_delete_account_fixtures(self.account)

	@patch("marketplace_integrations.api.talabat_orders.talabat_orders.get_vendor_orders")
	def test_import_vendor_orders_requires_account_read_permission(self, mock_get_vendor_orders):
		"""import_vendor_orders checks read permission on the account."""
		mock_get_vendor_orders.return_value = {
			"orders": [],
			"page_number": 1,
			"page_size": 20,
			"total_pages": 1,
		}
		email, _role = make_role_user(
			self,
			"Import No Read",
			{},
		)

		with self.set_user(email):
			with self.assertRaises(frappe.PermissionError):
				import_vendor_orders(self.account.name, "2026-08-01", "2026-08-31")

	@patch("marketplace_integrations.api.talabat_orders.talabat_orders.get_vendor_orders")
	def test_import_vendor_orders_requires_marketplace_order_create_permission(self, mock_get_vendor_orders):
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
		email, _role = make_role_user(
			self,
			"Import No Create",
			{"Marketplace Account": ["read"], "Marketplace Order": ["read"]},
		)
		with self.set_user(email):
			self.assertTrue(frappe.has_permission("Marketplace Account", "read"))
			self.assertFalse(frappe.has_permission("Marketplace Order", "create"))
			with self.assertRaises(frappe.PermissionError):
				import_vendor_orders(self.account.name, "2026-08-01", "2026-08-31")

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

	@patch("marketplace_integrations.api.talabat_orders.normalize_order")
	@patch("marketplace_integrations.api.talabat_orders.talabat_orders.get_vendor_orders")
	def test_import_vendor_orders_persists_a_failure_as_an_order_import_error_log(
		self, mock_get_vendor_orders, mock_normalize
	):
		"""#169: a failed order is written to Marketplace Sync Log (not just
		returned in the response) so the Needs Attention report can show it."""
		mock_get_vendor_orders.return_value = {
			"orders": [{"order_id": "order-bad-001"}],
			"page_number": 1,
			"page_size": 20,
			"total_pages": 1,
		}
		mock_normalize.side_effect = frappe.ValidationError("bad order")

		result = import_vendor_orders(self.account.name, "2026-08-01", "2026-08-31")

		self.assertEqual(len(result["errors"]), 1)
		logs = frappe.get_all(
			"Marketplace Sync Log",
			filters={
				"marketplace_account": self.account.name,
				"sync_type": "Order Import",
				"status": "Error",
			},
			fields=["name", "trigger_source", "request_data", "traceback"],
		)
		try:
			self.assertEqual(len(logs), 1)
			self.assertEqual(logs[0].trigger_source, "Manual")
			self.assertEqual(frappe.parse_json(logs[0].request_data)["order_id"], "order-bad-001")
			self.assertEqual(logs[0].traceback, result["errors"][0]["error"])
		finally:
			for log in logs:
				frappe.delete_doc("Marketplace Sync Log", log.name, force=True)
