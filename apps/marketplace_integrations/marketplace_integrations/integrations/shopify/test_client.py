# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

import time
from unittest.mock import MagicMock, patch

import frappe
import requests
from frappe.tests import IntegrationTestCase

from marketplace_integrations.integrations.shopify.client import (
	DEFAULT_THROTTLE_BACKOFF_SECONDS,
	MAX_THROTTLE_ATTEMPTS,
	MAX_THROTTLE_BACKOFF_SECONDS,
	ShopifyClient,
)


def _mock_token_response(access_token, expires_in=86399):
	response = MagicMock()
	response.ok = True
	response.json.return_value = {
		"access_token": access_token,
		"scope": "read_products",
		"expires_in": expires_in,
	}
	return response


def _response(status_code):
	response = MagicMock()
	response.status_code = status_code
	response.ok = status_code < 400
	return response


def _mock_graphql_response(data, errors=None):
	response = MagicMock()
	response.ok = True
	response.status_code = 200
	body = {"data": data}
	if errors:
		body["errors"] = errors
	response.json.return_value = body
	return response


def _mock_throttled_response(retry_after):
	return _mock_graphql_response(
		data=None,
		errors=[
			{
				"message": "Throttled",
				"extensions": {"code": "THROTTLED", "retryAfter": retry_after},
			}
		],
	)


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

	# -- execute ----------------------------------------------------------

	@patch.object(ShopifyClient, "get_access_token")
	@patch("marketplace_integrations.integrations.http.requests.request")
	def test_execute_posts_query_and_variables_with_access_token_header(self, mock_request, mock_get_token):
		mock_get_token.return_value = "token-abc"
		mock_request.return_value = _mock_graphql_response({"shop": {"name": "Test Shop"}})

		data = self.client.execute("query { shop { name } }", {"id": "gid://shopify/Shop/1"})

		self.assertEqual(data, {"shop": {"name": "Test Shop"}})
		mock_request.assert_called_once()
		args, kwargs = mock_request.call_args
		self.assertEqual(args[0], "POST")
		self.assertEqual(args[1], "https://test-shop.myshopify.com/admin/api/2026-07/graphql.json")
		self.assertEqual(kwargs["headers"]["X-Shopify-Access-Token"], "token-abc")
		self.assertEqual(kwargs["json"]["query"], "query { shop { name } }")
		self.assertEqual(kwargs["json"]["variables"], {"id": "gid://shopify/Shop/1"})

	def test_execute_raises_when_connection_disabled(self):
		self.connection.enabled = 0
		self.connection.save()

		with self.assertRaises(frappe.ValidationError):
			self.client.execute("query { shop { name } }")

	@patch.object(ShopifyClient, "invalidate_access_token")
	@patch.object(ShopifyClient, "get_access_token")
	@patch("marketplace_integrations.integrations.http.requests.request")
	def test_execute_401_triggers_single_invalidate_and_retry(
		self, mock_request, mock_get_token, mock_invalidate
	):
		mock_get_token.side_effect = ["stale-token", "fresh-token"]
		mock_request.side_effect = [
			_response(401),
			_mock_graphql_response({"shop": {"name": "Test Shop"}}),
		]

		data = self.client.execute("query { shop { name } }")

		self.assertEqual(data, {"shop": {"name": "Test Shop"}})
		mock_invalidate.assert_called_once()
		self.assertEqual(mock_request.call_count, 2)
		second_call_kwargs = mock_request.call_args_list[1][1]
		self.assertEqual(second_call_kwargs["headers"]["X-Shopify-Access-Token"], "fresh-token")

	@patch.object(ShopifyClient, "invalidate_access_token")
	@patch.object(ShopifyClient, "get_access_token")
	@patch("marketplace_integrations.integrations.http.requests.request")
	def test_execute_second_401_raises_without_third_attempt(
		self, mock_request, mock_get_token, mock_invalidate
	):
		mock_get_token.side_effect = ["stale-token", "still-stale-token"]
		mock_request.side_effect = [_response(401), _response(401)]

		with self.assertRaises(frappe.ValidationError):
			self.client.execute("query { shop { name } }")

		mock_invalidate.assert_called_once()
		self.assertEqual(mock_request.call_count, 2)

	@patch.object(ShopifyClient, "get_access_token")
	@patch("marketplace_integrations.integrations.http.requests.request")
	def test_execute_raises_on_top_level_errors(self, mock_request, mock_get_token):
		mock_get_token.return_value = "token-abc"
		mock_request.return_value = _mock_graphql_response(
			data=None, errors=[{"message": "Field 'bogus' doesn't exist on type 'Shop'"}]
		)

		with self.assertRaises(frappe.ValidationError) as ctx:
			self.client.execute("query { shop { bogus } }")

		self.assertIn("bogus", str(ctx.exception))

	@patch.object(ShopifyClient, "get_access_token")
	@patch("marketplace_integrations.integrations.http.requests.request")
	def test_execute_returns_mutation_user_errors_without_raising(self, mock_request, mock_get_token):
		mock_get_token.return_value = "token-abc"
		mock_request.return_value = _mock_graphql_response(
			{
				"someMutation": {
					"result": None,
					"userErrors": [{"field": ["input"], "message": "Invalid input"}],
				}
			}
		)

		data = self.client.execute("mutation { someMutation { userErrors { field message } } }")

		self.assertEqual(
			data["someMutation"]["userErrors"], [{"field": ["input"], "message": "Invalid input"}]
		)

	@patch("marketplace_integrations.integrations.shopify.client.time.sleep")
	@patch.object(ShopifyClient, "get_access_token")
	@patch("marketplace_integrations.integrations.http.requests.request")
	def test_execute_retries_on_throttle_with_bounded_backoff_then_succeeds(
		self, mock_request, mock_get_token, mock_sleep
	):
		mock_get_token.return_value = "token-abc"
		mock_request.side_effect = [
			_mock_throttled_response(retry_after=2),
			_mock_graphql_response({"shop": {"name": "Test Shop"}}),
		]

		data = self.client.execute("query { shop { name } }")

		self.assertEqual(data, {"shop": {"name": "Test Shop"}})
		mock_sleep.assert_called_once_with(2)
		self.assertEqual(mock_request.call_count, 2)

	@patch("marketplace_integrations.integrations.shopify.client.time.sleep")
	@patch.object(ShopifyClient, "get_access_token")
	@patch("marketplace_integrations.integrations.http.requests.request")
	def test_execute_gives_up_after_fixed_throttle_attempt_cap(
		self, mock_request, mock_get_token, mock_sleep
	):
		mock_get_token.return_value = "token-abc"
		mock_request.return_value = _mock_throttled_response(retry_after=1)

		with self.assertRaises(frappe.ValidationError):
			self.client.execute("query { shop { name } }")

		self.assertEqual(mock_request.call_count, MAX_THROTTLE_ATTEMPTS)
		self.assertEqual(mock_sleep.call_count, MAX_THROTTLE_ATTEMPTS - 1)

	@patch("marketplace_integrations.integrations.shopify.client.time.sleep")
	@patch.object(ShopifyClient, "get_access_token")
	@patch("marketplace_integrations.integrations.http.requests.request")
	def test_throttled_response_missing_retry_after_still_retries_with_default_backoff(
		self, mock_request, mock_get_token, mock_sleep
	):
		mock_get_token.return_value = "token-abc"
		mock_request.side_effect = [
			_mock_graphql_response(
				data=None,
				errors=[{"message": "Throttled", "extensions": {"code": "THROTTLED"}}],
			),
			_mock_graphql_response({"shop": {"name": "Test Shop"}}),
		]

		data = self.client.execute("query { shop { name } }")

		self.assertEqual(data, {"shop": {"name": "Test Shop"}})
		mock_sleep.assert_called_once_with(DEFAULT_THROTTLE_BACKOFF_SECONDS)
		self.assertEqual(mock_request.call_count, 2)

	@patch("marketplace_integrations.integrations.shopify.client.time.sleep")
	@patch.object(ShopifyClient, "get_access_token")
	@patch("marketplace_integrations.integrations.http.requests.request")
	def test_throttled_response_with_retry_after_above_cap_is_capped(
		self, mock_request, mock_get_token, mock_sleep
	):
		mock_get_token.return_value = "token-abc"
		mock_request.side_effect = [
			_mock_throttled_response(retry_after=9999),
			_mock_graphql_response({"shop": {"name": "Test Shop"}}),
		]

		data = self.client.execute("query { shop { name } }")

		self.assertEqual(data, {"shop": {"name": "Test Shop"}})
		mock_sleep.assert_called_once_with(MAX_THROTTLE_BACKOFF_SECONDS)

	@patch.object(ShopifyClient, "get_access_token")
	@patch("marketplace_integrations.integrations.http.requests.request")
	def test_execute_raises_clean_error_on_non_json_success_response(self, mock_request, mock_get_token):
		mock_get_token.return_value = "token-abc"
		response = MagicMock()
		response.ok = True
		response.status_code = 200
		response.json.side_effect = ValueError("not json")
		response.text = "<html>captive portal</html>"
		mock_request.return_value = response

		with self.assertRaises(frappe.ValidationError):
			self.client.execute("query { shop { name } }")

	@patch.object(ShopifyClient, "get_access_token")
	@patch("marketplace_integrations.integrations.http.requests.request")
	def test_execute_raises_clean_error_on_list_response_body(self, mock_request, mock_get_token):
		mock_get_token.return_value = "token-abc"
		response = MagicMock()
		response.ok = True
		response.status_code = 200
		response.json.return_value = ["unexpected", "list"]
		response.text = '["unexpected", "list"]'
		mock_request.return_value = response

		with self.assertRaises(frappe.ValidationError):
			self.client.execute("query { shop { name } }")

	@patch.object(ShopifyClient, "get_access_token")
	@patch("marketplace_integrations.integrations.http.requests.request")
	def test_execute_raises_clean_error_when_body_has_neither_data_nor_errors(
		self, mock_request, mock_get_token
	):
		mock_get_token.return_value = "token-abc"
		response = MagicMock()
		response.ok = True
		response.status_code = 200
		response.json.return_value = {"extensions": {"cost": {}}}
		mock_request.return_value = response

		with self.assertRaises(frappe.ValidationError):
			self.client.execute("query { shop { name } }")

	@patch("marketplace_integrations.integrations.shopify.client.time.sleep")
	@patch.object(ShopifyClient, "get_access_token")
	@patch("marketplace_integrations.integrations.http.requests.request")
	def test_throttled_response_with_non_numeric_string_retry_after_uses_default_backoff(
		self, mock_request, mock_get_token, mock_sleep
	):
		mock_get_token.return_value = "token-abc"
		mock_request.side_effect = [
			_mock_throttled_response(retry_after="5"),
			_mock_graphql_response({"shop": {"name": "Test Shop"}}),
		]

		data = self.client.execute("query { shop { name } }")

		self.assertEqual(data, {"shop": {"name": "Test Shop"}})
		mock_sleep.assert_called_once_with(DEFAULT_THROTTLE_BACKOFF_SECONDS)

	@patch("marketplace_integrations.integrations.shopify.client.time.sleep")
	@patch.object(ShopifyClient, "get_access_token")
	@patch("marketplace_integrations.integrations.http.requests.request")
	def test_throttled_response_with_boolean_retry_after_uses_default_backoff(
		self, mock_request, mock_get_token, mock_sleep
	):
		mock_get_token.return_value = "token-abc"
		mock_request.side_effect = [
			_mock_throttled_response(retry_after=True),
			_mock_graphql_response({"shop": {"name": "Test Shop"}}),
		]

		data = self.client.execute("query { shop { name } }")

		self.assertEqual(data, {"shop": {"name": "Test Shop"}})
		mock_sleep.assert_called_once_with(DEFAULT_THROTTLE_BACKOFF_SECONDS)

	@patch("marketplace_integrations.integrations.shopify.client.time.sleep")
	@patch.object(ShopifyClient, "get_access_token")
	@patch("marketplace_integrations.integrations.http.requests.request")
	def test_throttled_response_with_negative_retry_after_is_floored_at_zero(
		self, mock_request, mock_get_token, mock_sleep
	):
		mock_get_token.return_value = "token-abc"
		mock_request.side_effect = [
			_mock_throttled_response(retry_after=-5),
			_mock_graphql_response({"shop": {"name": "Test Shop"}}),
		]

		data = self.client.execute("query { shop { name } }")

		self.assertEqual(data, {"shop": {"name": "Test Shop"}})
		mock_sleep.assert_called_once_with(0)

	@patch("marketplace_integrations.integrations.shopify.client.time.sleep")
	@patch.object(ShopifyClient, "get_access_token")
	@patch("marketplace_integrations.integrations.http.requests.request")
	def test_throttled_response_retry_after_is_capped_at_ten_seconds_not_thirty(
		self, mock_request, mock_get_token, mock_sleep
	):
		mock_get_token.return_value = "token-abc"
		mock_request.side_effect = [
			_mock_throttled_response(retry_after=9999),
			_mock_graphql_response({"shop": {"name": "Test Shop"}}),
		]

		data = self.client.execute("query { shop { name } }")

		self.assertEqual(data, {"shop": {"name": "Test Shop"}})
		self.assertEqual(MAX_THROTTLE_BACKOFF_SECONDS, 10)
		mock_sleep.assert_called_once_with(10)

	@patch.object(ShopifyClient, "get_access_token")
	@patch("marketplace_integrations.integrations.http.requests.request")
	def test_execute_raises_clean_error_on_dict_shaped_errors_field(self, mock_request, mock_get_token):
		mock_get_token.return_value = "token-abc"
		response = MagicMock()
		response.ok = True
		response.status_code = 200
		response.json.return_value = {
			"data": None,
			"errors": {"message": "Something went wrong"},
		}
		mock_request.return_value = response

		with self.assertRaises(frappe.ValidationError) as ctx:
			self.client.execute("query { shop { name } }")

		self.assertIn("Something went wrong", str(ctx.exception))

	@patch.object(ShopifyClient, "get_access_token")
	@patch("marketplace_integrations.integrations.http.requests.request")
	def test_execute_raises_clean_error_on_string_shaped_errors_field(self, mock_request, mock_get_token):
		mock_get_token.return_value = "token-abc"
		response = MagicMock()
		response.ok = True
		response.status_code = 200
		response.json.return_value = {"data": None, "errors": "Something went wrong"}
		mock_request.return_value = response

		with self.assertRaises(frappe.ValidationError) as ctx:
			self.client.execute("query { shop { name } }")

		self.assertIn("Something went wrong", str(ctx.exception))

	@patch.object(ShopifyClient, "get_access_token")
	@patch("marketplace_integrations.integrations.http.requests.request")
	def test_execute_raises_clean_error_when_data_is_explicitly_null(self, mock_request, mock_get_token):
		mock_get_token.return_value = "token-abc"
		response = MagicMock()
		response.ok = True
		response.status_code = 200
		response.json.return_value = {"data": None}
		mock_request.return_value = response

		with self.assertRaises(frappe.ValidationError):
			self.client.execute("query { shop { name } }")

	@patch("marketplace_integrations.integrations.shopify.client.time.sleep")
	@patch.object(ShopifyClient, "invalidate_access_token")
	@patch.object(ShopifyClient, "get_access_token")
	@patch("marketplace_integrations.integrations.http.requests.request")
	def test_execute_spends_only_one_token_refresh_across_throttle_and_401(
		self, mock_request, mock_get_token, mock_invalidate, mock_sleep
	):
		mock_get_token.side_effect = ["stale-token", "fresh-token", "fresh-token"]
		mock_request.side_effect = [
			_response(401),
			_mock_throttled_response(retry_after=1),
			_response(401),
		]

		with self.assertRaises(frappe.ValidationError):
			self.client.execute("query { shop { name } }")

		mock_invalidate.assert_called_once()
		self.assertEqual(mock_request.call_count, 3)
