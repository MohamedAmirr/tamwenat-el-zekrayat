# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

import time
from unittest.mock import MagicMock, patch

import frappe
import requests
from frappe.tests import IntegrationTestCase

from marketplace_integrations.integrations.shopify.client import ShopifyClient


def _mock_token_response(access_token, expires_in=86399):
	response = MagicMock()
	response.ok = True
	response.json.return_value = {
		"access_token": access_token,
		"scope": "read_products",
		"expires_in": expires_in,
	}
	return response


def make_connection(connection_name, **kwargs):
	doc = {
		"doctype": "Shopify Connection",
		"connection_name": connection_name,
		"shop_domain": "test-shop.myshopify.com",
		"client_id": "test-client-id",
		"client_secret": "test-client-secret",
	}
	doc.update(kwargs)
	return frappe.get_doc(doc).insert()


class IntegrationTestShopifyClient(IntegrationTestCase):
	def setUp(self):
		self.connection = make_connection("Test Client Connection")
		self.client = ShopifyClient(self.connection.name)

	def tearDown(self):
		self.client.invalidate_access_token()

	@patch("marketplace_integrations.integrations.http.requests.request")
	def test_returns_token_from_successful_fetch_with_exactly_one_post(self, mock_request):
		mock_request.return_value = _mock_token_response("shop-token")

		token = self.client.get_access_token()

		self.assertEqual(token, "shop-token")
		mock_request.assert_called_once()
		args, kwargs = mock_request.call_args
		self.assertEqual(args[0], "POST")
		self.assertEqual(args[1], "https://test-shop.myshopify.com/admin/oauth/access_token")
		self.assertEqual(kwargs["data"]["grant_type"], "client_credentials")
		self.assertEqual(kwargs["data"]["client_id"], "test-client-id")
		self.assertEqual(kwargs["data"]["client_secret"], "test-client-secret")

	@patch("marketplace_integrations.integrations.http.requests.request")
	def test_immediate_second_call_issues_no_http_request(self, mock_request):
		mock_request.return_value = _mock_token_response("shop-token")

		first = self.client.get_access_token()
		second = self.client.get_access_token()

		self.assertEqual(first, "shop-token")
		self.assertEqual(second, "shop-token")
		mock_request.assert_called_once()

	@patch("marketplace_integrations.integrations.http.requests.request")
	def test_cache_ttl_is_expires_in_minus_buffer(self, mock_request):
		mock_request.return_value = _mock_token_response("shop-token", expires_in=86399)

		self.client.get_access_token()

		cached = frappe.cache().get_value(self.client._cache_key())
		self.assertIsNotNone(cached)
		expected_ttl = 86399 - 60
		self.assertAlmostEqual(cached["expires_at"] - time.time(), expected_ttl, delta=2)

	@patch("marketplace_integrations.integrations.http.requests.request")
	def test_two_connections_cache_independent_tokens(self, mock_request):
		other_connection = make_connection(
			"Test Client Connection Other", shop_domain="other-shop.myshopify.com"
		)
		other_client = ShopifyClient(other_connection.name)
		self.addCleanup(other_client.invalidate_access_token)

		mock_request.side_effect = [
			_mock_token_response("token-for-connection-a"),
			_mock_token_response("token-for-connection-b"),
		]

		token_a = self.client.get_access_token()
		token_b = other_client.get_access_token()

		self.assertEqual(token_a, "token-for-connection-a")
		self.assertEqual(token_b, "token-for-connection-b")
		self.assertEqual(mock_request.call_count, 2)

		self.assertEqual(self.client.get_access_token(), "token-for-connection-a")
		self.assertEqual(other_client.get_access_token(), "token-for-connection-b")
		self.assertEqual(mock_request.call_count, 2)

	def test_saving_connection_clears_only_its_own_cached_token(self):
		other_connection = make_connection(
			"Test Cache Save Other", shop_domain="cache-save-other.myshopify.com"
		)
		other_client = ShopifyClient(other_connection.name)
		self.addCleanup(other_client.invalidate_access_token)

		token_payload = {"access_token": "cached-token", "expires_at": time.time() + 3600}
		frappe.cache().set_value(self.client._cache_key(), token_payload, expires_in_sec=3600)
		frappe.cache().set_value(other_client._cache_key(), token_payload, expires_in_sec=3600)

		self.connection.enabled = 0
		self.connection.save()

		self.assertIsNone(frappe.cache().get_value(self.client._cache_key()))
		self.assertEqual(frappe.cache().get_value(other_client._cache_key()), token_payload)

	@patch("marketplace_integrations.integrations.http.requests.request")
	def test_auth_failure_raises_clear_error_without_leaking_secret_or_token(self, mock_request):
		response = MagicMock()
		response.ok = False
		response.json.return_value = {
			"error": "invalid_client",
			"error_description": "Client authentication failed",
		}
		mock_request.return_value = response

		with self.assertRaises(frappe.ValidationError) as ctx:
			self.client.get_access_token()

		message = str(ctx.exception)
		self.assertNotIn("test-client-secret", message)
		self.assertNotIn("shop-token", message)

	@patch("marketplace_integrations.integrations.http.requests.request")
	def test_network_error_raises_clear_error_not_raw_traceback(self, mock_request):
		mock_request.side_effect = requests.exceptions.ConnectionError("connection refused")

		with self.assertRaises(frappe.ValidationError) as ctx:
			self.client.get_access_token()

		message = str(ctx.exception)
		self.assertIn("Shopify", message)
		self.assertNotIn("Talabat", message)

	@patch("marketplace_integrations.integrations.http.requests.request")
	def test_malformed_response_raises_clear_error(self, mock_request):
		response = MagicMock()
		response.ok = True
		response.json.return_value = {"scope": "read_products"}  # missing access_token
		mock_request.return_value = response

		with self.assertRaises(frappe.ValidationError):
			self.client.get_access_token()

	@patch("marketplace_integrations.integrations.http.requests.request")
	def test_null_expires_in_raises_clear_error_not_raw_traceback(self, mock_request):
		response = MagicMock()
		response.ok = True
		response.json.return_value = {"access_token": "shop-token", "expires_in": None}
		mock_request.return_value = response

		with self.assertRaises(frappe.ValidationError):
			self.client.get_access_token()

	@patch("marketplace_integrations.integrations.http.requests.request")
	def test_string_expires_in_raises_clear_error_not_raw_traceback(self, mock_request):
		response = MagicMock()
		response.ok = True
		response.json.return_value = {"access_token": "shop-token", "expires_in": "not-a-number"}
		mock_request.return_value = response

		with self.assertRaises(frappe.ValidationError):
			self.client.get_access_token()
