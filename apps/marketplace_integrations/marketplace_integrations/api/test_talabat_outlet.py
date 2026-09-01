# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.api.talabat_outlet import refresh_outlet_status, set_outlet_status


def _make_connection(connection_name):
	return frappe.get_doc(
		{
			"doctype": "Talabat Connection",
			"connection_name": connection_name,
			"client_id": "test-client-id",
			"client_secret": "test-client-secret",
			"chain_id": "test-chain-id",
			"environment": "Sandbox",
			"enabled": 1,
		}
	).insert()


def _make_account(account_name, connection):
	return frappe.get_doc(
		{
			"doctype": "Marketplace Account",
			"account_name": account_name,
			"platform": "Talabat",
			"external_account_id": f"test-vendor-{account_name}",
			"connection_doctype": "Talabat Connection",
			"connection": connection.name,
			"enabled": 1,
		}
	).insert()


class IntegrationTestTalabatOutletApi(IntegrationTestCase):
	def setUp(self):
		suffix = frappe.generate_hash(length=8)
		connection = _make_connection(f"Outlet API Connection {suffix}")
		self.account = _make_account(f"Outlet API Account {suffix}", connection)

	@patch("marketplace_integrations.api.talabat_outlet.outlet.get_outlet_status")
	def test_refresh_requires_write_permission(self, mock_refresh):
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": f"outlet-test-{frappe.generate_hash(length=10)}@example.com",
				"first_name": "Outlet Test User",
				"send_welcome_email": 0,
				"roles": [{"role": "All"}],
			}
		).insert()

		with self.set_user(user.name):
			with self.assertRaises(frappe.PermissionError):
				refresh_outlet_status(self.account.name)
		mock_refresh.assert_not_called()

	@patch("marketplace_integrations.api.talabat_outlet.outlet.update_outlet_status")
	def test_set_status_returns_confirmed_cache(self, mock_update):
		self.account.external_status = "OPEN"
		self.account.save()
		mock_update.return_value = self.account

		result = set_outlet_status(self.account.name, "OPEN")

		mock_update.assert_called_once_with(self.account.name, "OPEN", None, None)
		self.assertEqual(result["name"], self.account.name)
		self.assertEqual(result["external_status"], "OPEN")
