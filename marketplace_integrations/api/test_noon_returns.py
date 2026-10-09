# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Tests for the permission-checked Desk boundary (api/noon_returns.py).

The API module is thin by design: it authorizes the caller and rejects a
non-Noon account, then enqueues the actual ListReturnReferences lookup on
the ``long`` queue - this module never makes a Noon HTTP call in the Desk
request itself (verified below by asserting the queue function is never
actually called, only the whitelisted wrapper around it).
"""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.api.noon_returns import lookup_return
from marketplace_integrations.tests.utils import make_role_user


def _make_noon_connection(connection_name, project_code):
	return frappe.get_doc(
		{
			"doctype": "Noon Connection",
			"connection_name": connection_name,
			"project_code": project_code,
			"key_id": "test-key-id",
			"private_key": "-----BEGIN RSA PRIVATE KEY-----\ntest\n-----END RSA PRIVATE KEY-----",
			"environment": "Static Sandbox",
			"enabled": 1,
		}
	).insert()


def _make_noon_account(account_name, warehouse_code="NOON-WH-001", country_code="ae"):
	project_code = f"test-project-{frappe.generate_hash(length=8)}"
	connection = _make_noon_connection(account_name + " Connection", project_code)
	with patch("marketplace_integrations.integrations.noon.catalog.validate_warehouse_code"):
		return frappe.get_doc(
			{
				"doctype": "Marketplace Account",
				"account_name": account_name,
				"platform": "Noon",
				"external_account_id": project_code,
				"connection_doctype": "Noon Connection",
				"connection": connection.name,
				"noon_warehouse_code": warehouse_code,
				"noon_country_code": country_code,
				"enabled": 1,
			}
		).insert()


def _make_talabat_connection(connection_name):
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


def _make_non_noon_account(account_name):
	connection = _make_talabat_connection(account_name + " Connection")
	return frappe.get_doc(
		{
			"doctype": "Marketplace Account",
			"account_name": account_name,
			"platform": "Talabat",
			"external_account_id": f"ext-{frappe.generate_hash(length=8)}",
			"connection_doctype": "Talabat Connection",
			"connection": connection.name,
			"enabled": 1,
		}
	).insert()


class IntegrationTestLookupReturnEndpoint(IntegrationTestCase):
	def setUp(self):
		suffix = frappe.generate_hash(length=8)
		self.account = _make_noon_account(f"ReturnsApi {suffix}")

	def tearDown(self):
		frappe.delete_doc("Marketplace Account", self.account.name, ignore_permissions=True, force=True)
		frappe.delete_doc("Noon Connection", self.account.connection, ignore_permissions=True, force=True)
		frappe.db.commit()

	@patch("marketplace_integrations.api.noon_returns.queue_manual_lookup")
	def test_lookup_return_queues_without_noon_http_call(self, mock_queue):
		"""lookup_return() enqueues the lookup and returns immediately - no
		Noon HTTP call happens in this request."""
		mock_queue.return_value = {"queued": True}
		result = lookup_return(self.account.name, "BARCODE-1")
		mock_queue.assert_called_once_with(self.account.name, "BARCODE-1")
		self.assertEqual(result, {"queued": True})

	@patch("marketplace_integrations.api.noon_returns.queue_manual_lookup")
	def test_lookup_return_requires_marketplace_account_write(self, mock_queue):
		"""A user with only read permission on the Marketplace Account is
		rejected before anything is queued."""
		email, _role = make_role_user(self, "Returns No Write", {"Marketplace Account": ["read"]})
		with self.set_user(email):
			self.assertTrue(frappe.has_permission("Marketplace Account", "read"))
			self.assertFalse(frappe.has_permission("Marketplace Account", "write"))
			with self.assertRaises(frappe.PermissionError):
				lookup_return(self.account.name, "BARCODE-2")
		mock_queue.assert_not_called()

	@patch("marketplace_integrations.api.noon_returns.queue_manual_lookup")
	def test_lookup_return_rejects_unauthorized_guest(self, mock_queue):
		"""An unauthorized (Guest) caller is rejected before anything is
		queued."""
		with self.set_user("Guest"):
			with self.assertRaises(frappe.PermissionError):
				lookup_return(self.account.name, "BARCODE-3")
		mock_queue.assert_not_called()

	@patch("marketplace_integrations.api.noon_returns.queue_manual_lookup")
	def test_lookup_return_rejects_non_noon_account(self, mock_queue):
		"""A non-Noon Marketplace Account is rejected before anything is
		queued."""
		other_account = _make_non_noon_account(f"NonNoon {frappe.generate_hash(length=8)}")
		try:
			with self.assertRaises(frappe.ValidationError):
				lookup_return(other_account.name, "BARCODE-4")
		finally:
			frappe.delete_doc(
				"Marketplace Account", other_account.name, ignore_permissions=True, force=True
			)
			frappe.delete_doc(
				"Talabat Connection", other_account.connection, ignore_permissions=True, force=True
			)
		mock_queue.assert_not_called()

	@patch("marketplace_integrations.api.noon_returns.queue_manual_lookup")
	def test_lookup_return_validates_account_exists(self, mock_queue):
		with self.assertRaises(frappe.DoesNotExistError):
			lookup_return("NONEXISTENT-ACCOUNT", "BARCODE-5")
		mock_queue.assert_not_called()
