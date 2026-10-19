# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Tests for the permission-checked Desk boundary (api/noon_fulfillment.py).

Thin by design, matching api/noon_orders.py's own test shape: every endpoint
is checked for write permission on the Marketplace Order and that it is
actually a Noon order, then delegates to
``integrations/noon/fulfillment.py`` (mocked out here - its own behavior is
covered by integrations/noon/test_fulfillment.py).
"""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.api.noon_fulfillment import (
	add_awb,
	cancel_shipment,
	create_shipment,
	discard_shipment,
	get_shippable_units,
	mark_out_of_stock,
	release_stuck_oos_reservation,
	retry,
	retry_order_invoice,
	upload_invoice,
	upload_order_invoice,
)
from marketplace_integrations.services.marketplace_orders import upsert_marketplace_order
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
	"""Covers permission checks shared by every endpoint (asserted once via
	create_shipment and once via get_shippable_units - both go through the
	same ``_get_noon_order()`` helper every other endpoint also uses) plus
	one delegation test per endpoint."""

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

	def test_requires_marketplace_order_write_permission(self):
		email, _role = make_role_user(self, "Noon Fulfillment No Write", {"Marketplace Order": ["read"]})
		with self.set_user(email):
			self.assertTrue(frappe.has_permission("Marketplace Order", "read"))
			self.assertFalse(frappe.has_permission("Marketplace Order", "write"))
			with self.assertRaises(frappe.PermissionError):
				get_shippable_units(self.order.name)
			with self.assertRaises(frappe.PermissionError):
				create_shipment(self.order.name, ["1"], "Noon")

	def test_rejects_non_noon_order(self):
		frappe.db.set_value("Marketplace Order", self.order.name, "platform", "Talabat")
		with self.assertRaises(frappe.ValidationError):
			get_shippable_units(self.order.name)
		with self.assertRaises(frappe.ValidationError):
			create_shipment(self.order.name, ["1"], "Noon")

	@patch("marketplace_integrations.api.noon_fulfillment.fulfillment.get_shippable_units")
	def test_get_shippable_units_delegates(self, mock_fn):
		mock_fn.return_value = [{"mp_item_nr": "1", "partner_sku": "SKU-1"}]
		result = get_shippable_units(self.order.name)
		self.assertEqual(result, [{"mp_item_nr": "1", "partner_sku": "SKU-1"}])
		mock_fn.assert_called_once()

	@patch("marketplace_integrations.api.noon_fulfillment.fulfillment.create_shipment")
	def test_create_shipment_delegates_and_parses_json_list(self, mock_fn):
		mock_fn.return_value = {"integration_shipment_nr": "ORD-1", "queued": True}
		result = create_shipment(self.order.name, '["1", "2"]', "Noon")
		mock_fn.assert_called_once_with(self.order.name, ["1", "2"], "Noon", None, None)
		self.assertEqual(result, {"integration_shipment_nr": "ORD-1", "queued": True})

	@patch("marketplace_integrations.api.noon_fulfillment.fulfillment.cancel_shipment")
	def test_cancel_shipment_delegates(self, mock_fn):
		mock_fn.return_value = {"queued": True}
		result = cancel_shipment(self.order.name, "ORD-1")
		mock_fn.assert_called_once_with(self.order.name, "ORD-1")
		self.assertEqual(result, {"queued": True})

	@patch("marketplace_integrations.api.noon_fulfillment.fulfillment.add_awb")
	def test_add_awb_delegates(self, mock_fn):
		mock_fn.return_value = {"queued": True}
		result = add_awb(self.order.name, "ORD-1", "Aramex", "AWB-1")
		mock_fn.assert_called_once_with(self.order.name, "ORD-1", "Aramex", "AWB-1")
		self.assertEqual(result, {"queued": True})

	@patch("marketplace_integrations.api.noon_fulfillment.fulfillment.mark_out_of_stock")
	def test_mark_out_of_stock_delegates_and_parses_json_list(self, mock_fn):
		mock_fn.return_value = {"queued": True}
		result = mark_out_of_stock(self.order.name, '["1"]')
		mock_fn.assert_called_once_with(self.order.name, ["1"])
		self.assertEqual(result, {"queued": True})

	@patch("marketplace_integrations.api.noon_fulfillment.fulfillment.upload_invoice")
	def test_upload_invoice_delegates(self, mock_fn):
		mock_fn.return_value = {"queued": True}
		result = upload_invoice(self.order.name, "ORD-1", "/files/invoice.pdf", "INV-1", "2026-10-01")
		mock_fn.assert_called_once_with(self.order.name, "ORD-1", "/files/invoice.pdf", "INV-1", "2026-10-01")
		self.assertEqual(result, {"queued": True})

	@patch("marketplace_integrations.api.noon_fulfillment.fulfillment.upload_order_invoice")
	def test_upload_order_invoice_delegates(self, mock_fn):
		mock_fn.return_value = {"queued": True}
		result = upload_order_invoice(self.order.name, "/files/invoice.pdf", "INV-1", "2026-10-01")
		mock_fn.assert_called_once_with(self.order.name, "/files/invoice.pdf", "INV-1", "2026-10-01")
		self.assertEqual(result, {"queued": True})

	@patch("marketplace_integrations.api.noon_fulfillment.fulfillment.retry_order_invoice")
	def test_retry_order_invoice_delegates(self, mock_fn):
		mock_fn.return_value = {"queued": True}
		result = retry_order_invoice(self.order.name)
		mock_fn.assert_called_once_with(self.order.name)
		self.assertEqual(result, {"queued": True})

	@patch("marketplace_integrations.api.noon_fulfillment.fulfillment.retry")
	def test_retry_delegates(self, mock_fn):
		mock_fn.return_value = {"queued": True}
		result = retry(self.order.name, "ORD-1")
		mock_fn.assert_called_once_with(self.order.name, "ORD-1")
		self.assertEqual(result, {"queued": True})

	@patch("marketplace_integrations.api.noon_fulfillment.fulfillment.discard_shipment")
	def test_discard_shipment_delegates(self, mock_fn):
		mock_fn.return_value = {"discarded": True}
		result = discard_shipment(self.order.name, "ORD-1")
		mock_fn.assert_called_once_with(self.order.name, "ORD-1")
		self.assertEqual(result, {"discarded": True})

	@patch("marketplace_integrations.api.noon_fulfillment.fulfillment.release_stuck_oos_reservation")
	def test_release_stuck_oos_reservation_delegates(self, mock_fn):
		mock_fn.return_value = {"released": ["1"]}
		result = release_stuck_oos_reservation(self.order.name)
		mock_fn.assert_called_once_with(self.order.name)
		self.assertEqual(result, {"released": ["1"]})
