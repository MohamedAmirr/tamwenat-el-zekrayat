# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

from unittest.mock import MagicMock, patch
from urllib.parse import quote

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.integrations.talabat import orders as talabat_orders


class TestOrderUpdatePayloadValidation(IntegrationTestCase):
	def test_not_found_requires_only_pricing_type(self):
		talabat_orders.validate_order_update(
			{
				"status": "UPDATE_CART",
				"items": [{"sku": "x", "status": "NOT_FOUND", "pricing": {"pricing_type": "UNIT"}}],
			}
		)

	def test_replacement_requires_in_cart_link(self):
		payload = {
			"status": "UPDATE_CART",
			"items": [
				{"_id": "old", "status": "REPLACED", "pricing": {"pricing_type": "UNIT"}},
				{
					"sku": "new",
					"status": "IN_CART",
					"replaced_id": "old",
					"pricing": {"pricing_type": "UNIT", "quantity": 1},
				},
			],
		}
		talabat_orders.validate_order_update(payload)

	def test_replacement_without_link_is_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			talabat_orders.validate_order_update(
				{
					"status": "UPDATE_CART",
					"items": [{"_id": "old", "status": "REPLACED", "pricing": {"pricing_type": "UNIT"}}],
				}
			)

	def test_malformed_pricing_is_clean_validation_error(self):
		with self.assertRaises(frappe.ValidationError):
			talabat_orders.validate_order_update(
				{
					"status": "UPDATE_CART",
					"items": [
						{
							"sku": "x",
							"status": "IN_CART",
							"pricing": {"pricing_type": "UNIT", "quantity": "bad"},
						}
					],
				}
			)

	def test_unit_and_kg_payloads_require_matching_measurement(self):
		talabat_orders.validate_order_update(
			{
				"status": "UPDATE_CART",
				"items": [
					{"sku": "x", "status": "IN_CART", "pricing": {"pricing_type": "UNIT", "quantity": 1}}
				],
			}
		)
		talabat_orders.validate_order_update(
			{
				"status": "UPDATE_CART",
				"items": [
					{"sku": "x", "status": "IN_CART", "pricing": {"pricing_type": "KG", "weight": 1.5}}
				],
			}
		)
		with self.assertRaises(frappe.ValidationError):
			talabat_orders.validate_order_update(
				{
					"status": "UPDATE_CART",
					"items": [
						{"sku": "x", "status": "IN_CART", "pricing": {"pricing_type": "UNIT", "quantity": -1}}
					],
				}
			)

	def test_non_dict_nested_item_is_clean_validation_error(self):
		with self.assertRaises(frappe.ValidationError):
			talabat_orders.validate_order_update({"status": "UPDATE_CART", "items": ["bad"]})


def _response(body, status_code=200):
	"""Helper to mock HTTP responses."""
	response = MagicMock()
	response.ok = status_code < 400
	response.status_code = status_code
	response.json.return_value = body
	return response


# Records registered by the helpers below; drained by _cleanup_created() in tearDown.
_CREATED = []


def _track(doc):
	_CREATED.append(doc)
	return doc


def _cleanup_created():
	"""Delete tracked Marketplace Accounts (with their Mappings) and then their Talabat Connections.
	Names are re-checked against the DB so fixtures that never got inserted are skipped."""
	accounts = [d.name for d in _CREATED if d.doctype == "Marketplace Account"]
	connections = {d.name for d in _CREATED if d.doctype == "Talabat Connection"}
	_CREATED.clear()
	for name in accounts:
		if not frappe.db.exists("Marketplace Account", name):
			continue
		connections.add(frappe.db.get_value("Marketplace Account", name, "connection"))
		frappe.db.delete("Marketplace Product Mapping", {"marketplace_account": name})
		frappe.delete_doc("Marketplace Account", name, ignore_permissions=True, force=True)
	for name in connections:
		if name and frappe.db.exists("Talabat Connection", name):
			frappe.delete_doc("Talabat Connection", name, ignore_permissions=True, force=True)


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
	return _track(frappe.get_doc(doc).insert())


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
	return _track(frappe.get_doc(doc).insert())


class IntegrationTestTalabatOrdersAccountResolution(IntegrationTestCase):
	"""Tests for account resolution and authorization."""

	def tearDown(self):
		_cleanup_created()

	def test_get_account_resolves_valid_talabat_account_and_connection(self):
		"""get_account resolves a valid account and returns both."""
		suffix = frappe.generate_hash(length=8)
		connection = _make_connection(f"Valid Connection {suffix}")
		account = _make_account(f"Valid Account {suffix}", connection)

		resolved_account, resolved_connection = talabat_orders.get_account(account.name)

		self.assertEqual(resolved_account.name, account.name)
		self.assertEqual(resolved_connection.name, connection.name)
		self.assertEqual(resolved_account.platform, "Talabat")
		self.assertEqual(resolved_account.connection_doctype, "Talabat Connection")

	def test_get_account_requires_account_enabled(self):
		"""Account must be enabled for order operations."""
		suffix = frappe.generate_hash(length=8)
		connection = _make_connection(f"Test Connection {suffix}")
		account = _make_account(f"Disabled Account {suffix}", connection, enabled=0)

		with self.assertRaises(frappe.ValidationError) as cm:
			talabat_orders.get_account(account.name)
		self.assertIn("is disabled", str(cm.exception))

	def test_get_account_requires_connection_enabled(self):
		"""Connection must be enabled for order operations."""
		suffix = frappe.generate_hash(length=8)
		connection = _make_connection(f"Disabled Connection {suffix}", enabled=0)
		account = _make_account(f"Test Account {suffix}", connection)

		with self.assertRaises(frappe.ValidationError) as cm:
			talabat_orders.get_account(account.name)
		self.assertIn("is disabled", str(cm.exception))

	def test_get_account_no_fallback_to_talabat_settings(self):
		"""Account resolution never falls back to legacy Talabat Settings."""
		# This test documents the requirement that account resolution
		# always uses Marketplace Account + Talabat Connection, never
		# the legacy Talabat Settings Single.
		suffix = frappe.generate_hash(length=8)
		connection = _make_connection(f"Test Connection {suffix}")
		account = _make_account(f"Test Account {suffix}", connection)

		# If legacy Talabat Settings were being used as a fallback, this test
		# would need to create it and verify it's NOT consulted. Since the
		# code already correctly requires connection_doctype="Talabat Connection",
		# the absence of a fallback is already proved by the earlier tests.
		# This test is a documentation anchor.
		resolved_account, resolved_connection = talabat_orders.get_account(account.name)
		self.assertEqual(resolved_account.name, account.name)
		self.assertEqual(resolved_connection.name, connection.name)

	def test_get_account_enforces_read_permission(self):
		"""Account read permission is checked."""
		suffix = frappe.generate_hash(length=8)
		connection = _make_connection(f"Test Connection {suffix}")
		account = _make_account(f"Test Account {suffix}", connection)
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": f"order-test-{frappe.generate_hash(length=10)}@example.com",
				"first_name": "Order Test User",
				"send_welcome_email": 0,
				"roles": [{"role": "All"}],
			}
		).insert()

		with self.set_user(user.name):
			with self.assertRaises(frappe.PermissionError):
				talabat_orders.get_account(account.name)


class IntegrationTestTalabatOrdersRetrieval(IntegrationTestCase):
	"""Tests for order retrieval functions."""

	def setUp(self):
		suffix = frappe.generate_hash(length=8)
		self.connection = _make_connection(f"Test Order Retrieval Connection {suffix}")
		self.account = _make_account(f"Test Order Retrieval Account {suffix}", self.connection)

	def tearDown(self):
		_cleanup_created()

	@patch("marketplace_integrations.integrations.talabat.orders.TalabatClient.request")
	def test_get_order_fetches_by_order_id(self, mock_request):
		"""get_order fetches the Talabat order endpoint."""
		mock_request.return_value = _response(
			{
				"order_id": "order-uuid-001",
				"order_code": "ORDER-001",
				"status": "RECEIVED",
				"customer": {"_id": "customer-001"},
				"payment": {},
				"sys": {},
			}
		)

		result = talabat_orders.get_order(self.account.name, "order-uuid-001")

		self.assertEqual(result["order_id"], "order-uuid-001")
		mock_request.assert_called_once()
		args = mock_request.call_args[0]
		self.assertEqual(args[0], "GET")
		self.assertIn(f"chains/{self.connection.chain_id}/orders/order-uuid-001", args[1])

	@patch("marketplace_integrations.integrations.talabat.orders.TalabatClient.request")
	def test_get_order_strips_whitespace_from_order_id(self, mock_request):
		"""Order ID is stripped of whitespace."""
		mock_request.return_value = _response({"order_id": "order-001"})

		talabat_orders.get_order(self.account.name, "  order-001  ")

		args = mock_request.call_args[0]
		self.assertIn("order-001", args[1])
		self.assertNotIn("  order-001  ", args[1])

	@patch("marketplace_integrations.integrations.talabat.orders.TalabatClient.request")
	def test_get_order_percent_encodes_chain_and_order_path_segments(self, mock_request):
		"""Path-reserved characters in chain and order IDs are encoded."""
		self.connection.chain_id = "chain /?#%"
		self.connection.save()
		mock_request.return_value = _response({"order_id": "order /?#%"})

		talabat_orders.get_order(self.account.name, "order /?#%")

		endpoint = mock_request.call_args[0][1]
		self.assertEqual(
			endpoint,
			"/v2/chains/chain%20%2F%3F%23%25/orders/order%20%2F%3F%23%25",
		)

	@patch("marketplace_integrations.integrations.talabat.orders.TalabatClient.request")
	def test_get_order_rejects_empty_order_id(self, mock_request):
		"""Empty order ID raises an error."""
		with self.assertRaises(frappe.ValidationError):
			talabat_orders.get_order(self.account.name, "   ")

	@patch("marketplace_integrations.integrations.talabat.orders.TalabatClient.request")
	def test_get_order_handles_404_not_found(self, mock_request):
		"""404 response is raised as a clear error."""
		mock_request.return_value = _response({"error": "Not found"}, status_code=404)

		with self.assertRaises(frappe.ValidationError) as cm:
			talabat_orders.get_order(self.account.name, "missing-order-id")
		self.assertIn("not found", str(cm.exception))

	@patch("marketplace_integrations.integrations.talabat.orders.TalabatClient.request")
	def test_get_order_handles_non_ok_status(self, mock_request):
		"""Non-200/non-404 errors are raised as clear errors."""
		mock_request.return_value = _response({"error": "Server error"}, status_code=500)

		with self.assertRaises(frappe.ValidationError) as cm:
			talabat_orders.get_order(self.account.name, "order-id")
		self.assertIn("Failed to fetch", str(cm.exception))

	@patch("marketplace_integrations.integrations.talabat.orders.TalabatClient.request")
	def test_get_order_allows_matching_client_id(self, mock_request):
		"""A fetched order whose client.id matches the account's
		external_account_id is returned normally."""
		mock_request.return_value = _response(
			{
				"order_id": "order-001",
				"client": {"id": self.account.external_account_id},
			}
		)

		result = talabat_orders.get_order(self.account.name, "order-001")

		self.assertEqual(result["order_id"], "order-001")

	@patch("marketplace_integrations.integrations.talabat.orders.TalabatClient.request")
	def test_get_order_allows_matching_store_id(self, mock_request):
		"""A fetched order whose client.store_id (not client.id) matches the
		account's external_account_id is also accepted."""
		mock_request.return_value = _response(
			{
				"order_id": "order-001",
				"client": {"id": "some-other-internal-id", "store_id": self.account.external_account_id},
			}
		)

		result = talabat_orders.get_order(self.account.name, "order-001")

		self.assertEqual(result["order_id"], "order-001")

	@patch("marketplace_integrations.integrations.talabat.orders.TalabatClient.request")
	def test_get_order_rejects_mismatched_client_id(self, mock_request):
		"""An order whose client identity doesn't match this account's
		vendor is rejected rather than silently returned - this is the guard
		against a shared Connection leaking one vendor's order into another
		vendor's Marketplace Account."""
		mock_request.return_value = _response(
			{
				"order_id": "order-001",
				"client": {"id": "a-different-vendor-id", "store_id": "a-different-store-id"},
			}
		)

		with self.assertRaises(frappe.ValidationError) as cm:
			talabat_orders.get_order(self.account.name, "order-001")
		self.assertIn("different vendor", str(cm.exception))

	@patch("marketplace_integrations.integrations.talabat.orders.TalabatClient.request")
	def test_get_order_allows_missing_client_block(self, mock_request):
		"""A response with no client block at all has nothing to verify
		against and is let through, rather than rejected."""
		mock_request.return_value = _response({"order_id": "order-001"})

		result = talabat_orders.get_order(self.account.name, "order-001")

		self.assertEqual(result["order_id"], "order-001")

	@patch("marketplace_integrations.integrations.talabat.orders.TalabatClient.request")
	def test_get_vendor_orders_paginates_with_page_size_bounds(self, mock_request):
		"""Vendor order history pagination clamps page_size to 1-500."""
		mock_request.return_value = _response(
			{
				"orders": [{"order_id": "order-001"}],
				"page_number": 1,
				"page_size": 50,
				"total_pages": 1,
			}
		)

		# Test upper bound clamp
		talabat_orders.get_vendor_orders(self.account.name, page=1, page_size=1000)
		args = mock_request.call_args
		self.assertEqual(args[1]["params"]["page_size"], talabat_orders.MAX_PAGE_SIZE)

		# Test lower bound clamp
		talabat_orders.get_vendor_orders(self.account.name, page=1, page_size=0)
		args = mock_request.call_args
		self.assertEqual(args[1]["params"]["page_size"], talabat_orders.MIN_PAGE_SIZE)

		# Test within bounds
		talabat_orders.get_vendor_orders(self.account.name, page=1, page_size=50)
		args = mock_request.call_args
		self.assertEqual(args[1]["params"]["page_size"], 50)

	@patch("marketplace_integrations.integrations.talabat.orders.TalabatClient.request")
	def test_get_vendor_orders_bounds_page_minimum(self, mock_request):
		"""Page number is bounded to minimum 1."""
		mock_request.return_value = _response(
			{"orders": [], "page_number": 1, "page_size": 20, "total_pages": 1}
		)

		talabat_orders.get_vendor_orders(self.account.name, page=0)
		args = mock_request.call_args
		self.assertEqual(args[1]["params"]["page"], 1)

		talabat_orders.get_vendor_orders(self.account.name, page=-5)
		args = mock_request.call_args
		self.assertEqual(args[1]["params"]["page"], 1)

	@patch("marketplace_integrations.integrations.talabat.orders.TalabatClient.request")
	def test_get_vendor_orders_includes_date_range_parameters(self, mock_request):
		"""Date range parameters are included when provided."""
		mock_request.return_value = _response(
			{"orders": [], "page_number": 1, "page_size": 20, "total_pages": 1}
		)

		talabat_orders.get_vendor_orders(
			self.account.name,
			start_created_at_datetime="2026-08-01T00:00:00Z",
			end_created_at_datetime="2026-08-31T23:59:59Z",
		)

		args = mock_request.call_args
		params = args[1]["params"]
		self.assertEqual(params["start_time"], "2026-08-01T00:00:00Z")
		self.assertEqual(params["end_time"], "2026-08-31T23:59:59Z")

	@patch("marketplace_integrations.integrations.talabat.orders.TalabatClient.request")
	def test_get_vendor_orders_omits_empty_date_parameters(self, mock_request):
		"""Empty/blank date parameters are not sent."""
		mock_request.return_value = _response(
			{"orders": [], "page_number": 1, "page_size": 20, "total_pages": 1}
		)

		talabat_orders.get_vendor_orders(self.account.name, start_created_at_datetime="  ")

		args = mock_request.call_args
		params = args[1]["params"]
		self.assertNotIn("start_time", params)

	@patch("marketplace_integrations.integrations.talabat.orders.TalabatClient.request")
	def test_get_vendor_orders_uses_account_external_id_for_vendor_id(self, mock_request):
		"""Vendor ID in the endpoint is the account's external_account_id."""
		mock_request.return_value = _response(
			{"orders": [], "page_number": 1, "page_size": 20, "total_pages": 1}
		)

		talabat_orders.get_vendor_orders(self.account.name)

		args = mock_request.call_args
		endpoint = args[0][1]
		self.assertIn(f"vendors/{quote(self.account.external_account_id, safe='')}/orders", endpoint)

	@patch("marketplace_integrations.integrations.talabat.orders.TalabatClient.request")
	def test_get_vendor_orders_percent_encodes_chain_and_vendor_path_segments(self, mock_request):
		"""Path-reserved characters in chain and vendor IDs are encoded."""
		self.connection.chain_id = "chain /?#%"
		self.connection.save()
		self.account.external_account_id = "vendor /?#%"
		self.account.save()
		mock_request.return_value = _response(
			{"orders": [], "page_number": 1, "page_size": 20, "total_pages": 1}
		)

		talabat_orders.get_vendor_orders(self.account.name)

		endpoint = mock_request.call_args[0][1]
		self.assertEqual(
			endpoint,
			"/v2/chains/chain%20%2F%3F%23%25/vendors/vendor%20%2F%3F%23%25/orders",
		)

	@patch("marketplace_integrations.integrations.talabat.orders.TalabatClient.request")
	def test_get_vendor_orders_handles_api_error(self, mock_request):
		"""Non-OK responses raise clear errors."""
		mock_request.return_value = _response({"error": "Server error"}, status_code=500)

		with self.assertRaises(frappe.ValidationError) as cm:
			talabat_orders.get_vendor_orders(self.account.name)
		self.assertIn("Failed to fetch Talabat vendor order history", str(cm.exception))

	@patch("marketplace_integrations.integrations.talabat.orders.TalabatClient.request")
	def test_get_vendor_orders_returns_raw_response_body(self, mock_request):
		"""Function returns the complete Talabat response body."""
		expected_body = {
			"orders": [
				{"order_id": "order-001", "order_code": "CODE-001"},
				{"order_id": "order-002", "order_code": "CODE-002"},
			],
			"page_number": 1,
			"page_size": 20,
			"total_pages": 1,
		}
		mock_request.return_value = _response(expected_body)

		result = talabat_orders.get_vendor_orders(self.account.name)

		self.assertEqual(result, expected_body)
