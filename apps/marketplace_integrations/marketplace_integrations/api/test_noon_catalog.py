# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Permission tests for the Noon warehouse discovery boundary (api/noon_catalog.py).

Discovery itself is covered in integrations/noon/test_catalog.py; it is mocked here, so no
Noon call is ever made.
"""

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.api.noon_catalog import check_noon_warehouse, list_warehouses
from marketplace_integrations.integrations.noon.test_fulfillment import _make_noon_account
from marketplace_integrations.tests.utils import make_role_user

DISCOVER = "marketplace_integrations.api.noon_catalog.discover_warehouses"
NOON_CLIENT = "marketplace_integrations.integrations.noon.catalog.NoonClient"
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


def _warehouses_response(*warehouses):
	response = MagicMock(status_code=200)
	response.json.return_value = {"next_token": "", "warehouses": list(warehouses)}
	return response


def _warehouse(code="NOON-WH-001", fulfillment="FBPI", active=True):
	return {
		"warehouse_code": code,
		"display_name": code,
		"fulfillment_system_code": fulfillment,
		"is_active": active,
	}


class IntegrationTestCheckNoonWarehouse(IntegrationTestCase):
	"""``check_noon_warehouse`` (#416): ListWarehouses is mocked, no Noon call is ever made."""

	def setUp(self):
		self.account = _make_noon_account(f"Warehouse Check {frappe.generate_hash(length=6)}")
		frappe.db.commit()

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.delete(
			"Integration Request", {"reference_doctype": "Marketplace Account", "reference_docname": self.account.name}
		)
		frappe.delete_doc("Marketplace Account", self.account.name, ignore_permissions=True, force=True)
		frappe.delete_doc("Noon Connection", self.account.connection, ignore_permissions=True, force=True)
		frappe.db.commit()

	def _check(self, *responses, error_message=None):
		with patch(NOON_CLIENT) as client_class:
			client = client_class.return_value
			client.request.side_effect = list(responses)
			client.error_message.side_effect = lambda response: error_message
			result = check_noon_warehouse(self.account.name)
		return result, client

	def _stored(self):
		return frappe.db.get_value(
			"Marketplace Account",
			self.account.name,
			["noon_fulfillment_system_code", "noon_warehouse_active", "noon_warehouse_checked_on"],
			as_dict=True,
		)

	def _logs(self):
		return frappe.get_all(
			"Integration Request",
			filters={"reference_doctype": "Marketplace Account", "reference_docname": self.account.name},
			fields=["custom_operation_type", "custom_result", "integration_request_service", "output", "error"],
		)

	def test_the_matching_warehouse_is_stored_and_the_call_is_logged(self):
		result, client = self._check(_warehouses_response(_warehouse("OTHER", "X"), _warehouse()))
		stored = self._stored()
		self.assertEqual(
			(stored.noon_fulfillment_system_code, stored.noon_warehouse_active), ("FBPI", 1)
		)
		self.assertTrue(stored.noon_warehouse_checked_on)
		self.assertEqual((result["fulfillment_system_code"], result["is_active"]), ("FBPI", 1))
		self.assertEqual(client.request.call_args.args, ("POST", "/warehouse-platform/v1/warehouses/list"))
		(log,) = self._logs()
		self.assertEqual(
			(log.custom_operation_type, log.custom_result, log.integration_request_service),
			("Warehouse", "Success", "Noon"),
		)

	def test_an_inactive_warehouse_is_stored_inactive(self):
		self._check(_warehouses_response(_warehouse(active=False)))
		self.assertEqual(self._stored().noon_warehouse_active, 0)

	def test_a_warehouse_noon_does_not_list_clears_the_stored_answer(self):
		self._check(_warehouses_response(_warehouse()))
		with self.assertRaisesRegex(frappe.ValidationError, "no warehouse with code"):
			self._check(_warehouses_response(_warehouse("OTHER")))
		stored = self._stored()
		self.assertFalse(stored.noon_fulfillment_system_code)
		self.assertEqual(stored.noon_warehouse_active, 0)
		self.assertFalse(stored.noon_warehouse_checked_on)
		self.assertEqual(sorted(log.custom_result for log in self._logs()), ["Failed", "Success"])

	def test_a_noon_error_is_escaped_logged_and_leaves_the_stored_answer(self):
		self._check(_warehouses_response(_warehouse()))
		failing = MagicMock(status_code=500)
		with self.assertRaises(frappe.ValidationError) as caught:
			self._check(failing, error_message="<script>alert(1)</script>")
		self.assertIn("&lt;script&gt;", str(caught.exception))
		self.assertNotIn("<script>", str(caught.exception))
		self.assertEqual(self._stored().noon_fulfillment_system_code, "FBPI")
		failed = [log for log in self._logs() if log.custom_result == "Failed"]
		self.assertEqual(len(failed), 1)
		self.assertNotIn("<script>", failed[0].error)

	def test_an_unexpected_failure_shows_a_generic_message_without_its_text(self):
		with self.assertRaises(frappe.ValidationError) as caught:
			self._check(RuntimeError("cookie=SECRET"))
		self.assertNotIn("SECRET", str(caught.exception))
		(log,) = self._logs()
		self.assertEqual(log.custom_result, "Failed")
		self.assertNotIn("SECRET", log.error or "")

	def test_a_user_without_write_permission_is_refused_before_any_call(self):
		user, _role = make_role_user(self, "Account Reader", {"Marketplace Account": ["read"]})
		frappe.set_user(user)
		with patch(NOON_CLIENT) as client_class:
			with self.assertRaises(frappe.PermissionError):
				check_noon_warehouse(self.account.name)
		client_class.return_value.request.assert_not_called()
		frappe.set_user("Administrator")
		self.assertEqual(self._logs(), [])

	def test_changing_the_warehouse_code_clears_the_stored_answer(self):
		self._check(_warehouses_response(_warehouse()))
		account = frappe.get_doc("Marketplace Account", self.account.name)
		account.noon_warehouse_code = "NOON-WH-002"
		with patch("marketplace_integrations.integrations.noon.catalog.validate_warehouse_code"):
			account.save()
		stored = self._stored()
		self.assertFalse(stored.noon_fulfillment_system_code)
		self.assertEqual(stored.noon_warehouse_active, 0)
		self.assertFalse(stored.noon_warehouse_checked_on)
