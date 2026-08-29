# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

from unittest.mock import MagicMock, patch

import frappe
import requests
from frappe.tests import IntegrationTestCase

from talabat_integration.api.client import talabat_request


def _response(status_code):
	response = MagicMock()
	response.status_code = status_code
	return response


class IntegrationTestClient(IntegrationTestCase):
	def setUp(self):
		settings = frappe.get_single("Talabat Settings")
		settings.client_id = "test-client-id"
		settings.client_secret = "test-client-secret"
		settings.chain_id = "test-chain-id"
		settings.environment = "Sandbox"
		settings.save()

	@patch("talabat_integration.api.client.get_access_token")
	@patch("talabat_integration.api.auth.requests.request")
	def test_successful_request_passes_token(self, mock_request, mock_get_token):
		mock_get_token.return_value = "token-abc"
		mock_request.return_value = _response(200)

		response = talabat_request("GET", "/v2/chains/test-chain-id/vendors/v1/status")

		self.assertEqual(response.status_code, 200)
		mock_request.assert_called_once()
		_, kwargs = mock_request.call_args
		self.assertEqual(kwargs["headers"]["Authorization"], "Bearer token-abc")

	@patch("talabat_integration.api.client.invalidate_access_token")
	@patch("talabat_integration.api.client.get_access_token")
	@patch("talabat_integration.api.auth.requests.request")
	def test_401_triggers_single_retry_with_refreshed_token(
		self, mock_request, mock_get_token, mock_invalidate
	):
		mock_get_token.side_effect = ["stale-token", "fresh-token"]
		mock_request.side_effect = [_response(401), _response(200)]

		response = talabat_request("GET", "/v2/chains/test-chain-id/vendors/v1/status")

		self.assertEqual(response.status_code, 200)
		mock_invalidate.assert_called_once()
		self.assertEqual(mock_request.call_count, 2)
		second_call_kwargs = mock_request.call_args_list[1][1]
		self.assertEqual(second_call_kwargs["headers"]["Authorization"], "Bearer fresh-token")

	@patch("talabat_integration.api.client.invalidate_access_token")
	@patch("talabat_integration.api.client.get_access_token")
	@patch("talabat_integration.api.auth.requests.request")
	def test_second_401_raises_clear_error(self, mock_request, mock_get_token, mock_invalidate):
		mock_get_token.side_effect = ["stale-token", "still-stale-token"]
		mock_request.side_effect = [_response(401), _response(401)]

		with self.assertRaises(frappe.ValidationError):
			talabat_request("GET", "/v2/chains/test-chain-id/vendors/v1/status")

		mock_invalidate.assert_called_once()
		self.assertEqual(mock_request.call_count, 2)

	@patch("talabat_integration.api.client.get_access_token")
	@patch("talabat_integration.api.auth.requests.request")
	def test_network_error_raises_clear_error(self, mock_request, mock_get_token):
		mock_get_token.return_value = "token-abc"
		mock_request.side_effect = requests.exceptions.ConnectionError("connection refused")

		with self.assertRaises(frappe.ValidationError):
			talabat_request("GET", "/v2/chains/test-chain-id/vendors/v1/status")

	@patch("talabat_integration.api.client.get_access_token")
	@patch("talabat_integration.api.auth.requests.request")
	def test_path_without_leading_slash_is_normalized(self, mock_request, mock_get_token):
		mock_get_token.return_value = "token-abc"
		mock_request.return_value = _response(200)

		talabat_request("GET", "v2/chains/test-chain-id/vendors/v1/status")

		args, _ = mock_request.call_args
		self.assertEqual(
			args[1], "https://sandbox.partner.deliveryhero.io/v2/chains/test-chain-id/vendors/v1/status"
		)
