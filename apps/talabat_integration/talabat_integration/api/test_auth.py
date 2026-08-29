# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

from unittest.mock import MagicMock, patch

import frappe
import requests
from frappe.tests import IntegrationTestCase

from talabat_integration.api.auth import _cache_key, get_access_token


def _mock_response(access_token, expires_in=7200):
	response = MagicMock()
	response.ok = True
	response.json.return_value = {
		"access_token": access_token,
		"token_type": "Bearer",
		"expires_in": expires_in,
	}
	return response


class IntegrationTestAuth(IntegrationTestCase):
	def setUp(self):
		frappe.cache().delete_value(_cache_key("Sandbox"))
		frappe.cache().delete_value(_cache_key("Production"))

		settings = frappe.get_single("Talabat Settings")
		settings.client_id = "test-client-id"
		settings.client_secret = "test-client-secret"
		settings.chain_id = "test-chain-id"
		settings.environment = "Sandbox"
		settings.save()

	def tearDown(self):
		frappe.cache().delete_value(_cache_key("Sandbox"))
		frappe.cache().delete_value(_cache_key("Production"))

	@patch("talabat_integration.api.auth.requests.post")
	def test_returns_token_from_successful_fetch(self, mock_post):
		mock_post.return_value = _mock_response("sandbox-token")

		token = get_access_token()

		self.assertEqual(token, "sandbox-token")
		mock_post.assert_called_once()
		_, kwargs = mock_post.call_args
		self.assertEqual(kwargs["data"]["grant_type"], "client_credentials")
		self.assertEqual(kwargs["data"]["client_id"], "test-client-id")
		self.assertEqual(kwargs["data"]["client_secret"], "test-client-secret")

	@patch("talabat_integration.api.auth.requests.post")
	def test_cache_hit_does_not_refetch(self, mock_post):
		mock_post.return_value = _mock_response("sandbox-token")

		first = get_access_token()
		second = get_access_token()

		self.assertEqual(first, "sandbox-token")
		self.assertEqual(second, "sandbox-token")
		mock_post.assert_called_once()

	@patch("talabat_integration.api.auth.requests.post")
	def test_environment_switch_uses_separate_cache(self, mock_post):
		mock_post.side_effect = [
			_mock_response("sandbox-token"),
			_mock_response("production-token"),
		]

		sandbox_token = get_access_token()

		settings = frappe.get_single("Talabat Settings")
		settings.environment = "Production"
		settings.save()
		production_token = get_access_token()

		self.assertEqual(sandbox_token, "sandbox-token")
		self.assertEqual(production_token, "production-token")
		self.assertEqual(mock_post.call_count, 2)

	@patch("talabat_integration.api.auth.requests.post")
	def test_auth_failure_raises_clear_error(self, mock_post):
		response = MagicMock()
		response.ok = False
		response.json.return_value = {
			"error": "invalid_client",
			"error_description": "Client authentication failed",
		}
		mock_post.return_value = response

		with self.assertRaises(frappe.ValidationError):
			get_access_token()

	@patch("talabat_integration.api.auth.requests.post")
	def test_network_error_raises_clear_error(self, mock_post):
		mock_post.side_effect = requests.exceptions.ConnectionError("connection refused")

		with self.assertRaises(frappe.ValidationError):
			get_access_token()

	@patch("talabat_integration.api.auth.requests.post")
	def test_malformed_response_raises_clear_error(self, mock_post):
		response = MagicMock()
		response.ok = True
		response.json.return_value = {"token_type": "Bearer"}  # missing access_token
		mock_post.return_value = response

		with self.assertRaises(frappe.ValidationError):
			get_access_token()
