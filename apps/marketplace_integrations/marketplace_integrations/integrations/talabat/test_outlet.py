# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

from unittest.mock import MagicMock, patch
from urllib.parse import quote

import frappe

from marketplace_integrations.integrations.talabat import outlet
from marketplace_integrations.testing_utils import CleanupTestCase


def _response(body, status_code=200):
	response = MagicMock()
	response.ok = status_code < 400
	response.status_code = status_code
	response.json.return_value = body
	return response


def _make_connection(connection_name, **kwargs):
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


class IntegrationTestTalabatOutletStatus(CleanupTestCase):
	def setUp(self):
		suffix = frappe.generate_hash(length=8)
		self.connection = _make_connection(f"Outlet Connection {suffix}")
		self.account = _make_account(f"Outlet Account {suffix}", self.connection)

	@patch("marketplace_integrations.integrations.talabat.outlet.TalabatClient.request")
	def test_get_status_uses_account_vendor_and_persists_confirmed_response(self, mock_request):
		self.connection.chain_id = "chain /?#%"
		self.connection.save()
		self.account.external_account_id = "vendor /?#%"
		self.account.save()
		payload = {
			"status": "CLOSED_UNTIL",
			"closed_reason": "TOO_BUSY_KITCHEN",
			"closed_until": "2026-09-01T10:30:00Z",
		}
		mock_request.return_value = _response(payload)

		with patch("frappe.utils.data.get_system_timezone", return_value="Asia/Kolkata"):
			result = outlet.get_outlet_status(self.account.name)

		self.assertEqual(result.external_status, "CLOSED_UNTIL")
		self.assertEqual(result.external_status_reason, "TOO_BUSY_KITCHEN")
		self.assertEqual(result.external_status_until, "2026-09-01 16:00:00.000000")
		self.assertEqual(frappe.parse_json(result.external_status_payload), payload)
		self.assertFalse(result.external_status_error)
		mock_request.assert_called_once_with(
			"GET", "/v2/chains/chain%20%2F%3F%23%25/vendors/vendor%20%2F%3F%23%25/status"
		)

	@patch("marketplace_integrations.integrations.talabat.outlet.TalabatClient.request")
	def test_put_uses_allowed_reason_and_converts_user_datetime_to_utc(self, mock_request):
		mock_request.return_value = _response(
			{"status": "CLOSED_UNTIL", "closed_until": "2026-09-01T10:00:00Z"}
		)

		with patch("frappe.utils.data.get_system_timezone", return_value="Asia/Kolkata"):
			outlet.update_outlet_status(
				self.account.name,
				"CLOSED_UNTIL",
				"TOO_BUSY_NO_DRIVERS",
				"2026-09-01 15:30:00",
			)

		mock_request.assert_called_once_with(
			"PUT",
			f"/v2/chains/{self.connection.chain_id}/vendors/{quote(self.account.external_account_id, safe='')}/status",
			json={
				"status": "CLOSED_UNTIL",
				"closed_reason": "TOO_BUSY_NO_DRIVERS",
				"closed_until": "2026-09-01T10:00:00Z",
			},
		)

	@patch("marketplace_integrations.integrations.talabat.outlet.TalabatClient.request")
	def test_open_update_does_not_require_or_send_close_fields(self, mock_request):
		mock_request.return_value = _response({"status": "OPEN"})

		outlet.update_outlet_status(self.account.name, "OPEN", "OTHER", "2026-09-01 15:30:00")

		self.assertEqual(mock_request.call_args.kwargs["json"], {"status": "OPEN"})

	@patch("marketplace_integrations.integrations.talabat.outlet.TalabatClient.request")
	def test_failed_request_preserves_confirmed_status_and_records_safe_error(self, mock_request):
		self.account.external_status = "OPEN"
		self.account.external_status_reason = None
		self.account.external_status_until = None
		self.account.external_status_payload = {"status": "OPEN"}
		self.account.save()
		mock_request.return_value = _response({"message": "untrusted remote body"}, status_code=500)

		with self.assertRaises(frappe.ValidationError):
			outlet.update_outlet_status(self.account.name, "CLOSED_TODAY", "OTHER")

		self.account.reload()
		self.assertEqual(self.account.external_status, "OPEN")
		self.assertEqual(frappe.parse_json(self.account.external_status_payload), {"status": "OPEN"})
		self.assertEqual(self.account.external_status_error, outlet.REMOTE_ERROR)

	@patch("marketplace_integrations.integrations.talabat.outlet.TalabatClient.request")
	def test_failed_get_preserves_confirmed_status_and_records_safe_error(self, mock_request):
		self.account.external_status = "CLOSED"
		self.account.external_status_payload = {"status": "CLOSED"}
		self.account.save()
		mock_request.return_value = _response({"message": "untrusted remote body"}, status_code=500)

		with self.assertRaises(frappe.ValidationError):
			outlet.get_outlet_status(self.account.name)

		self.account.reload()
		self.assertEqual(self.account.external_status, "CLOSED")
		self.assertEqual(frappe.parse_json(self.account.external_status_payload), {"status": "CLOSED"})
		self.assertEqual(self.account.external_status_error, outlet.REMOTE_ERROR)

	@patch("marketplace_integrations.integrations.talabat.outlet.TalabatClient.request")
	def test_malformed_success_response_preserves_confirmed_status_and_records_error(self, mock_request):
		self.account.external_status = "OPEN"
		self.account.save()
		mock_request.return_value = _response({"status": "CHECKIN"})

		with self.assertRaises(frappe.ValidationError):
			outlet.get_outlet_status(self.account.name)

		self.account.reload()
		self.assertEqual(self.account.external_status, "OPEN")
		self.assertEqual(self.account.external_status_error, outlet.REMOTE_ERROR)

	@patch("marketplace_integrations.integrations.talabat.outlet.TalabatClient.request")
	def test_rejects_unsupported_updates_before_remote_request(self, mock_request):
		with self.assertRaises(frappe.ValidationError):
			outlet.update_outlet_status(self.account.name, "CHECKIN")
		with self.assertRaises(frappe.ValidationError):
			outlet.update_outlet_status(self.account.name, "CLOSED", "OTHER")
		with self.assertRaises(frappe.ValidationError):
			outlet.update_outlet_status(self.account.name, "CLOSED_TODAY", "NOT_ALLOWED")
		mock_request.assert_not_called()
