# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Tests for the permission-checked Desk boundary (api/noon_fulfillment.py).

Thin by design, matching api/noon_orders.py's own test shape: every endpoint
is checked for write permission on the Marketplace Order and that it is
actually a Noon order, then delegates to
``integrations/noon/fulfillment.py`` (mocked out here - its own behavior is
covered by integrations/noon/test_fulfillment.py). The shipment endpoints are gone (#415): shipments
live on the Delivery Note (``api/carrier_shipments.py``).
"""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.api import noon_fulfillment
from marketplace_integrations.api.noon_fulfillment import (
	check_invoice_upload,
	check_mark_out_of_stock,
	get_available_actions,
	get_order_invoice_state,
	get_out_of_stock_rows,
	mark_item_out_of_stock,
	release_stuck_oos_reservation,
	retry_order_invoice,
	upload_order_invoice,
)
from marketplace_integrations.services.marketplace_orders import upsert_marketplace_order
from marketplace_integrations.tests.handover_fixture import SubmittedOrderFixture
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


def _make_noon_account(account_name):
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
				"noon_warehouse_code": "NOON-WH-001",
				"noon_country_code": "ae",
				"enabled": 1,
			}
		).insert()


class IntegrationTestNoonFulfillmentEndpoints(IntegrationTestCase):
	"""Covers the Marketplace Order permission check shared by the invoice and recovery endpoints (the same
	``_get_noon_order()`` helper), the Sales Order write check of the Mark as Out of Stock endpoints, plus one
	delegation test per endpoint."""

	def setUp(self):
		suffix = frappe.generate_hash(length=8)
		self.account = _make_noon_account(f"Fulfillment API {suffix}")
		self.order = upsert_marketplace_order(
			self.account.name,
			{"external_order_id": f"ORD-API-{suffix}", "items": [], "latest_payload": {}},
		)

	def tearDown(self):
		frappe.db.delete("Marketplace Order", filters={"marketplace_account": self.account.name})
		frappe.delete_doc("Marketplace Account", self.account.name, ignore_permissions=True, force=True)
		frappe.delete_doc("Noon Connection", self.account.connection, ignore_permissions=True, force=True)
		frappe.db.commit()

	def test_the_shipment_endpoints_are_gone(self):
		for name in (
			"create_shipment",
			"cancel_shipment",
			"mark_handed_over",
			"discard_shipment",
			"add_awb",
			"upload_invoice",
			"retry",
			"retry_delivery_note_shipment",
		):
			with self.subTest(endpoint=name):
				self.assertFalse(hasattr(noon_fulfillment, name))

	def test_requires_marketplace_order_write_permission(self):
		email, _role = make_role_user(self, "Noon Fulfillment No Write", {"Marketplace Order": ["read"]})
		with self.set_user(email):
			self.assertTrue(frappe.has_permission("Marketplace Order", "read"))
			self.assertFalse(frappe.has_permission("Marketplace Order", "write"))
			with self.assertRaises(frappe.PermissionError):
				release_stuck_oos_reservation(self.order.name)

	def test_rejects_non_noon_order(self):
		frappe.db.set_value("Marketplace Order", self.order.name, "platform", "Talabat")
		with self.assertRaises(frappe.ValidationError):
			release_stuck_oos_reservation(self.order.name)

	@patch("marketplace_integrations.api.noon_fulfillment.fulfillment.get_available_actions")
	def test_get_available_actions_needs_only_read(self, mock_fn):
		mock_fn.return_value = {"actions": ["release_stuck_oos_reservation"], "cancelled_on_noon": False}
		email, _role = make_role_user(self, "Noon Fulfillment Read Only", {"Marketplace Order": ["read"]})
		# A document-level read also passes the platform filter (permissions.py), which needs the
		# platform role; that role carries write too, so the rule "read only" is proved by the
		# permission type the endpoint asks for.
		frappe.get_doc("User", email).add_roles("Noon User")
		with (
			self.set_user(email),
			patch("frappe.model.document.Document.check_permission", autospec=True) as check_permission,
		):
			result = get_available_actions(self.order.name)
		self.assertEqual(result, {"actions": ["release_stuck_oos_reservation"], "cancelled_on_noon": False})
		self.assertEqual([call.args[1] for call in check_permission.call_args_list], ["read"])

	def test_get_available_actions_requires_read_and_a_noon_order(self):
		email, _role = make_role_user(self, "Noon Fulfillment No Read", {"Sales Order": ["read"]})
		with self.set_user(email):
			with self.assertRaises(frappe.PermissionError):
				get_available_actions(self.order.name)
		frappe.db.set_value("Marketplace Order", self.order.name, "platform", "Talabat")
		with self.assertRaises(frappe.ValidationError):
			get_available_actions(self.order.name)

	def test_the_unit_checklist_endpoints_are_gone(self):
		for name in ("get_shippable_units", "mark_out_of_stock"):
			with self.subTest(endpoint=name):
				self.assertFalse(hasattr(noon_fulfillment, name))

	def _sales_order(self):
		fixture = SubmittedOrderFixture(self.account, [("SKU-A", 1)])
		self.addCleanup(fixture.cleanup)
		return fixture

	def test_mark_item_out_of_stock_needs_write_on_the_sales_order_only(self):
		"""A Sales Order writer without any Marketplace Order right can mark an item; read alone cannot."""
		fixture = self._sales_order()
		row = fixture.row_for("SKU-A").name
		writer, _role = make_role_user(self, "Noon OOS Sales Writer", {"Sales Order": ["read", "write"]})
		reader, _role = make_role_user(self, "Noon OOS Sales Reader", {"Sales Order": ["read"]})
		with patch(
			"marketplace_integrations.api.noon_fulfillment.fulfillment.mark_row_out_of_stock",
			return_value={"queued": True, "request_id": "r1"},
		) as mock_fn:
			with self.set_user(writer):
				self.assertFalse(frappe.has_permission("Marketplace Order", "read"))
				self.assertEqual(mark_item_out_of_stock(fixture.name, row), {"queued": True, "request_id": "r1"})
			mock_fn.assert_called_once_with(fixture.name, row)
			with self.set_user(reader), self.assertRaises(frappe.PermissionError):
				mark_item_out_of_stock(fixture.name, row)
			self.assertEqual(mock_fn.call_count, 1)

	def test_the_row_list_and_the_poll_need_write_on_the_sales_order(self):
		fixture = self._sales_order()
		writer, _role = make_role_user(self, "Noon OOS Rows Writer", {"Sales Order": ["read", "write"]})
		reader, _role = make_role_user(self, "Noon OOS Rows Reader", {"Sales Order": ["read"]})
		with (
			patch(
				"marketplace_integrations.api.noon_fulfillment.fulfillment.out_of_stock_rows", return_value=["row-1"]
			),
			patch(
				"marketplace_integrations.api.noon_fulfillment.fulfillment.out_of_stock_result",
				return_value={"state": "pending", "message": ""},
			),
		):
			with self.set_user(writer):
				self.assertEqual(get_out_of_stock_rows(fixture.name), ["row-1"])
				self.assertEqual(check_mark_out_of_stock(fixture.name, "r1")["state"], "pending")
			with self.set_user(reader):
				with self.assertRaises(frappe.PermissionError):
					get_out_of_stock_rows(fixture.name)
				with self.assertRaises(frappe.PermissionError):
					check_mark_out_of_stock(fixture.name, "r1")

	def test_the_order_invoice_endpoints_need_write_on_the_sales_order_only(self):
		"""A Sales Order writer without any Marketplace Order right can upload, retry and read the state; a
		reader cannot. The Marketplace Order is derived from the Sales Order and passed with the Sales Order."""
		fixture = self._sales_order()
		order = fixture.marketplace_order.name
		writer, _role = make_role_user(self, "Noon Invoice Sales Writer", {"Sales Order": ["read", "write"]})
		reader, _role = make_role_user(self, "Noon Invoice Sales Reader", {"Sales Order": ["read"]})
		base = "marketplace_integrations.api.noon_fulfillment.fulfillment"
		with (
			patch(f"{base}.upload_order_invoice", return_value={"queued": True, "idempotency_key": "k"}) as upload,
			patch(f"{base}.retry_order_invoice", return_value={"queued": True, "idempotency_key": "k"}) as retry,
			patch(f"{base}.order_invoice_state", return_value={"can_upload": True}) as state,
		):
			with self.set_user(writer):
				self.assertFalse(frappe.has_permission("Marketplace Order", "read"))
				upload_order_invoice(fixture.name, "/private/files/invoice.pdf", "INV-1", "2026-10-01")
				retry_order_invoice(fixture.name)
				self.assertEqual(get_order_invoice_state(fixture.name), {"can_upload": True})
			upload.assert_called_once_with(
				order, "/private/files/invoice.pdf", "INV-1", "2026-10-01", sales_order=fixture.name
			)
			retry.assert_called_once_with(order, sales_order=fixture.name)
			self.assertEqual(state.call_count, 1)
			with self.set_user(reader):
				for call in (
					lambda: upload_order_invoice(fixture.name, "/private/files/invoice.pdf", "INV-1", "2026-10-01"),
					lambda: retry_order_invoice(fixture.name),
					lambda: get_order_invoice_state(fixture.name),
				):
					with self.assertRaises(frappe.PermissionError):
						call()
			self.assertEqual((upload.call_count, retry.call_count, state.call_count), (1, 1, 1))

	def test_the_order_invoice_endpoints_refuse_a_sales_order_that_is_not_a_noon_order(self):
		fixture = self._sales_order()
		frappe.db.set_value("Marketplace Order", fixture.marketplace_order.name, "platform", "Talabat")
		with self.assertRaises(frappe.ValidationError):
			upload_order_invoice(fixture.name, "/private/files/invoice.pdf", "INV-1", "2026-10-01")

	def test_the_invoice_poll_needs_write_on_the_document_and_a_known_type(self):
		fixture = self._sales_order()
		writer, _role = make_role_user(self, "Noon Poll Writer", {"Sales Order": ["read", "write"]})
		reader, _role = make_role_user(self, "Noon Poll Reader", {"Sales Order": ["read"]})
		with patch(
			"marketplace_integrations.api.noon_fulfillment.fulfillment.invoice_result",
			return_value={"state": "done", "message": ""},
		):
			with self.set_user(writer):
				self.assertEqual(check_invoice_upload("Sales Order", fixture.name, "k")["state"], "done")
			with self.set_user(reader), self.assertRaises(frappe.PermissionError):
				check_invoice_upload("Sales Order", fixture.name, "k")
			with self.assertRaises(frappe.ValidationError):
				check_invoice_upload("User", "Administrator", "k")

	@patch("marketplace_integrations.api.noon_fulfillment.fulfillment.release_stuck_oos_reservation")
	def test_release_stuck_oos_reservation_delegates(self, mock_fn):
		mock_fn.return_value = {"released": ["1"]}
		result = release_stuck_oos_reservation(self.order.name)
		mock_fn.assert_called_once_with(self.order.name)
		self.assertEqual(result, {"released": ["1"]})
