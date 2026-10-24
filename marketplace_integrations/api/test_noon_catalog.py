# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Permission tests for the Noon warehouse discovery boundary (api/noon_catalog.py).

Discovery itself is covered in integrations/noon/test_catalog.py; it is mocked here, so no
Noon call is ever made.
"""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.api.noon_catalog import list_warehouses

DISCOVER = "marketplace_integrations.api.noon_catalog.discover_warehouses"
WAREHOUSES = [{"warehouse_code": "WH-1", "display_name": "One"}]


class IntegrationTestNoonCatalogApi(IntegrationTestCase):
	def setUp(self):
		self.connection = frappe.get_doc(
			{
				"doctype": "Noon Connection",
				"connection_name": f"Noon Catalog API {frappe.generate_hash(length=6)}",
				"project_code": f"test-project-{frappe.generate_hash(length=8)}",
				"key_id": "test-key-id",
				"private_key": "-----BEGIN RSA PRIVATE KEY-----\ntest\n-----END RSA PRIVATE KEY-----",
				"environment": "Static Sandbox",
				"enabled": 1,
			}
		).insert()
		self.addCleanup(
			frappe.delete_doc, "Noon Connection", self.connection.name, force=True, ignore_permissions=True
		)

	def _make_user(self, *roles):
		email = f"noon-catalog-{frappe.generate_hash(length=8)}@example.com"
		frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": "Noon Catalog Test",
				"send_welcome_email": 0,
				"roles": [{"role": role} for role in roles],
			}
		).insert()
		self.addCleanup(
			frappe.delete_doc, "User", email, force=True, ignore_permissions=True, delete_permanently=True
		)
		return email

	def _call_as(self, user):
		frappe.set_user(user)
		try:
			return list_warehouses(self.connection.name)
		finally:
			frappe.set_user("Administrator")

	def test_noon_user_cannot_use_a_connection_they_cannot_read(self):
		# #322: Noon User passes the doc-less Marketplace Account write check, but Noon
		# Connection stays System Manager only.
		user = self._make_user("Noon User")
		with patch(DISCOVER, return_value=WAREHOUSES) as discover:
			with self.assertRaises(frappe.PermissionError):
				self._call_as(user)
		discover.assert_not_called()

	def test_system_manager_can_list_warehouses(self):
		user = self._make_user("System Manager")
		with patch(DISCOVER, return_value=WAREHOUSES) as discover:
			self.assertEqual(self._call_as(user), WAREHOUSES)
		discover.assert_called_once_with(self.connection.name)

	def test_nonexistent_connection_is_a_clean_error(self):
		with self.assertRaisesRegex(frappe.ValidationError, "does not exist"):
			list_warehouses("NOONCONN-DOES-NOT-EXIST")
