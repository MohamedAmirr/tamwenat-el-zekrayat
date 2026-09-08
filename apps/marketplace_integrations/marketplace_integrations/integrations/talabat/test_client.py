# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

import time
from unittest.mock import MagicMock, patch

import frappe
import requests
from frappe.tests import IntegrationTestCase

from marketplace_integrations.integrations.talabat.client import TalabatClient


def _mock_token_response(access_token, expires_in=7200):
	response = MagicMock()
	response.ok = True
	response.json.return_value = {
		"access_token": access_token,
		"token_type": "Bearer",
		"expires_in": expires_in,
	}
	return response


def _response(status_code):
	response = MagicMock()
	response.status_code = status_code
	return response


def make_connection(connection_name, **kwargs):
	doc = {
		"doctype": "Talabat Connection",
		"connection_name": connection_name,
		"client_id": "test-client-id",
		"client_secret": "test-client-secret",
		"chain_id": "test-chain-id",
		"environment": "Sandbox",
	}
	doc.update(kwargs)
	return frappe.get_doc(doc).insert()


class IntegrationTestTalabatClient(IntegrationTestCase):
	def setUp(self):
		self.connection = make_connection("Test Client Connection")
		self.client = TalabatClient(self.connection.name)

	def tearDown(self):
		self.client.invalidate_access_token()

	# -- get_access_token ---------------------------------------------------

	@patch("marketplace_integrations.integrations.http.requests.request")
	def test_returns_token_from_successful_fetch(self, mock_request):
		mock_request.return_value = _mock_token_response("sandbox-token")

		token = self.client.get_access_token()

		self.assertEqual(token, "sandbox-token")
		mock_request.assert_called_once()
		_, kwargs = mock_request.call_args
		self.assertEqual(kwargs["data"]["grant_type"], "client_credentials")
		self.assertEqual(kwargs["data"]["client_id"], "test-client-id")
		self.assertEqual(kwargs["data"]["client_secret"], "test-client-secret")

	@patch("marketplace_integrations.integrations.http.requests.request")
	def test_cache_hit_does_not_refetch(self, mock_request):
		mock_request.return_value = _mock_token_response("sandbox-token")

		first = self.client.get_access_token()
		second = self.client.get_access_token()

		self.assertEqual(first, "sandbox-token")
		self.assertEqual(second, "sandbox-token")
		mock_request.assert_called_once()

	@patch("marketplace_integrations.integrations.http.requests.request")
	def test_two_connections_cache_independent_tokens(self, mock_request):
		other_connection = make_connection("Test Client Connection Other", client_id="other-client-id")
		other_client = TalabatClient(other_connection.name)
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

		# Re-fetching either one must hit its own cache, not the other
		# connection's, and not trigger a third HTTP call.
		self.assertEqual(self.client.get_access_token(), "token-for-connection-a")
		self.assertEqual(other_client.get_access_token(), "token-for-connection-b")
		self.assertEqual(mock_request.call_count, 2)

	def test_connection_update_invalidates_cached_token(self):
		cache_key = self.client._cache_key()
		frappe.cache().set_value(
			cache_key,
			{"access_token": "old-token", "expires_at": time.time() + 3600},
			expires_in_sec=3600,
		)

		self.connection.environment = "Production"
		self.connection.save()

		self.assertIsNone(frappe.cache().get_value(cache_key))

	def test_connection_trash_invalidates_cached_token(self):
		cache_key = self.client._cache_key()
		frappe.cache().set_value(
			cache_key,
			{"access_token": "old-token", "expires_at": time.time() + 3600},
			expires_in_sec=3600,
		)

		self.connection.delete()

		self.assertIsNone(frappe.cache().get_value(cache_key))

	@patch("marketplace_integrations.integrations.http.requests.request")
	def test_auth_failure_raises_clear_error(self, mock_request):
		response = MagicMock()
		response.ok = False
		response.json.return_value = {
			"error": "invalid_client",
			"error_description": "Client authentication failed",
		}
		mock_request.return_value = response

		with self.assertRaises(frappe.ValidationError):
			self.client.get_access_token()

	@patch("marketplace_integrations.integrations.http.requests.request")
	def test_network_error_raises_clear_error_not_raw_traceback(self, mock_request):
		mock_request.side_effect = requests.exceptions.ConnectionError("connection refused")

		with self.assertRaises(frappe.ValidationError) as ctx:
			self.client.get_access_token()

		message = str(ctx.exception)
		self.assertIn("Talabat", message)
		self.assertNotIn("Shopify", message)

	@patch("marketplace_integrations.integrations.http.requests.request")
	def test_malformed_response_raises_clear_error(self, mock_request):
		response = MagicMock()
		response.ok = True
		response.json.return_value = {"token_type": "Bearer"}  # missing access_token
		mock_request.return_value = response

		with self.assertRaises(frappe.ValidationError):
			self.client.get_access_token()

	@patch("marketplace_integrations.integrations.http.requests.request")
	def test_null_expires_in_raises_clear_error_not_raw_traceback(self, mock_request):
		response = MagicMock()
		response.ok = True
		response.json.return_value = {"access_token": "sandbox-token", "expires_in": None}
		mock_request.return_value = response

		with self.assertRaises(frappe.ValidationError):
			self.client.get_access_token()

	@patch("marketplace_integrations.integrations.http.requests.request")
	def test_string_expires_in_raises_clear_error_not_raw_traceback(self, mock_request):
		response = MagicMock()
		response.ok = True
		response.json.return_value = {"access_token": "sandbox-token", "expires_in": "not-a-number"}
		mock_request.return_value = response

		with self.assertRaises(frappe.ValidationError):
			self.client.get_access_token()

	# -- request --------------------------------------------------------------

	@patch.object(TalabatClient, "get_access_token")
	@patch("marketplace_integrations.integrations.http.requests.request")
	def test_successful_request_passes_token(self, mock_request, mock_get_token):
		mock_get_token.return_value = "token-abc"
		mock_request.return_value = _response(200)

		response = self.client.request("GET", "/v2/chains/test-chain-id/vendors/v1/status")

		self.assertEqual(response.status_code, 200)
		mock_request.assert_called_once()
		_, kwargs = mock_request.call_args
		self.assertEqual(kwargs["headers"]["Authorization"], "Bearer token-abc")

	@patch.object(TalabatClient, "invalidate_access_token")
	@patch.object(TalabatClient, "get_access_token")
	@patch("marketplace_integrations.integrations.http.requests.request")
	def test_401_triggers_single_retry_with_refreshed_token(
		self, mock_request, mock_get_token, mock_invalidate
	):
		mock_get_token.side_effect = ["stale-token", "fresh-token"]
		mock_request.side_effect = [_response(401), _response(200)]

		response = self.client.request("GET", "/v2/chains/test-chain-id/vendors/v1/status")

		self.assertEqual(response.status_code, 200)
		mock_invalidate.assert_called_once()
		self.assertEqual(mock_request.call_count, 2)
		second_call_kwargs = mock_request.call_args_list[1][1]
		self.assertEqual(second_call_kwargs["headers"]["Authorization"], "Bearer fresh-token")

	@patch.object(TalabatClient, "invalidate_access_token")
	@patch.object(TalabatClient, "get_access_token")
	@patch("marketplace_integrations.integrations.http.requests.request")
	def test_second_401_raises_clear_error(self, mock_request, mock_get_token, mock_invalidate):
		mock_get_token.side_effect = ["stale-token", "still-stale-token"]
		mock_request.side_effect = [_response(401), _response(401)]

		with self.assertRaises(frappe.ValidationError):
			self.client.request("GET", "/v2/chains/test-chain-id/vendors/v1/status")

		mock_invalidate.assert_called_once()
		self.assertEqual(mock_request.call_count, 2)

	@patch.object(TalabatClient, "get_access_token")
	@patch("marketplace_integrations.integrations.http.requests.request")
	def test_network_error_from_request_raises_clear_error(self, mock_request, mock_get_token):
		mock_get_token.return_value = "token-abc"
		mock_request.side_effect = requests.exceptions.ConnectionError("connection refused")

		with self.assertRaises(frappe.ValidationError):
			self.client.request("GET", "/v2/chains/test-chain-id/vendors/v1/status")

	@patch.object(TalabatClient, "get_access_token")
	@patch("marketplace_integrations.integrations.http.requests.request")
	def test_path_without_leading_slash_is_normalized(self, mock_request, mock_get_token):
		mock_get_token.return_value = "token-abc"
		mock_request.return_value = _response(200)

		self.client.request("GET", "v2/chains/test-chain-id/vendors/v1/status")

		args, _ = mock_request.call_args
		self.assertEqual(
			args[1], "https://sandbox.partner.deliveryhero.io/v2/chains/test-chain-id/vendors/v1/status"
		)
