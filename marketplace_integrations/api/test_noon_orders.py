# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Tests for the permission-checked Desk boundaries (api/noon_orders.py).

The API module is thin by design: it authorizes the caller, then enqueues
the actual import/refresh on the ``long`` queue. Tests verify permission
checks and the queueing behavior, with actual order logic mocked out.
"""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.api.noon_orders import import_orders, refresh_order
from marketplace_integrations.services.marketplace_orders import upsert_marketplace_order
from marketplace_integrations.tests.utils import make_role_user


def _make_noon_connection(connection_name, project_code=None):
	"""Create a test Noon Connection."""
	if project_code is None:
		project_code = f"test-project-{frappe.generate_hash(length=8)}"
	return (
		frappe.get_doc(
			{
				"doctype": "Noon Connection",
				"connection_name": connection_name,
				"project_code": project_code,
				"key_id": "test-key-id",
				"private_key": "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA0Z3VS5JJcds1xfn8fH9gFKvKHxsXNpKZDqz5Fng0Z5HqL8H7\nDUgkY4L8Z5Z7Z5Z7Z5Z7Z5Z7Z5Z7Z5Z7Z5Z7Z5Z7Z5Z7Z5Z7Z5Z7Z5Z7Z5Z7Z5Z7\nZ5Z7Z5Z7Z5Z7Z5Z7Z5Z7Z5Z7Z5Z7Z5Z7Z5Z7Z5Z7Z5Z7Z5Z7Z5Z7Z5Z7Z5Z7Z5Z7\nZ5Z7Z5Z7Z5Z7Z5Z7Z5Z7Z5Z7Z5Z7Z5Z7Z5Z7Z5Z7Z5Z7Z5Z7Z5Z7Z5Z7Z5Z7Z5Z7\nwIDAQABAoIBABj/Dn/+BDrP5L8m5L8m5L8m5L8m5L8m5L8m5L8m5L8m5L8m5L8m\n5L8m5L8m5L8m5L8m5L8m5L8m5L8m5L8m5L8m5L8m5L8m5L8m5L8m5L8m5L8m5L8m\n5L8m5L8m5L8m5L8m5L8m5L8m5L8m5L8m5L8m5L8m5L8m5L8m5L8m5L8m5L8m5L8m\nwIDAQABCkEA5L8m5L8m5L8m5L8m5L8m5L8m5L8m5L8m5L8m5L8m5L8m5L8m5L8m\n-----END RSA PRIVATE KEY-----",
				"environment": "Static Sandbox",
				"enabled": 1,
			}
		)
		.insert()
		.name
	)


def _make_noon_account(
	account_name,
	connection=None,
	warehouse_code="NOON-WH-001",
	country_code="ae",
	enabled=1,
):
	"""Create a test Marketplace Account for Noon.

	The external_account_id must match the connection's project_code (Noon requirement).
	"""
	project_code = f"test-project-{frappe.generate_hash(length=8)}"
	if connection is None:
		connection = _make_noon_connection(account_name + " Connection", project_code)

	return frappe.get_doc(
		{
			"doctype": "Marketplace Account",
			"account_name": account_name,
			"platform": "Noon",
			"external_account_id": project_code,
			"connection_doctype": "Noon Connection",
			"connection": connection,
			"noon_warehouse_code": warehouse_code,
			"noon_country_code": country_code,
			"enabled": enabled,
		}
	).insert()


def _make_item(item_code):
	"""Get or create an Item."""
	if frappe.db.exists("Item", item_code):
		return item_code
	frappe.get_doc(
		{
			"doctype": "Item",
			"item_code": item_code,
			"item_name": item_code,
			"item_group": "Products",
			"stock_uom": "Nos",
			"is_stock_item": 0,
		}
	).insert()
	return item_code


def _order_payload(**overrides):
	"""Minimal Noon order payload for upsert."""
	payload = {
		"external_order_id": "ORD-001",
		"platform_order_reference": "MP-001",
		"order_code": "MP-001",
		"presentment_currency": "AED",
		"items": [],
		"latest_payload": {},
	}
	payload.update(overrides)
	return payload


class IntegrationTestImportOrdersEndpoint(IntegrationTestCase):
	"""Tests for import_orders() API endpoint - permission and queueing."""

	def setUp(self):
		# Patch warehouse code validation to avoid making real Noon API calls
		self.warehouse_patch = patch(
			"marketplace_integrations.integrations.noon.catalog.validate_warehouse_code"
		)
		self.warehouse_patch.start()
		self.addCleanup(self.warehouse_patch.stop)

		suffix = frappe.generate_hash(length=8)
		self.account = _make_noon_account(f"Import {suffix}")

	def tearDown(self):
		frappe.db.delete("Marketplace Order", filters={"marketplace_account": self.account.name})
		frappe.delete_doc("Marketplace Account", self.account.name, ignore_permissions=True, force=True)
		frappe.delete_doc("Noon Connection", self.account.connection, ignore_permissions=True, force=True)
		frappe.db.commit()

	def test_import_orders_requires_marketplace_account_write(self):
		"""import_orders() requires write permission on the Marketplace Account
		being acted on."""
		email, _role = make_role_user(
			self,
			"Import No Write",
			{"Marketplace Account": ["read"], "Marketplace Order": ["create", "write"]},
		)
		with self.set_user(email):
			self.assertTrue(frappe.has_permission("Marketplace Account", "read"))
			self.assertFalse(frappe.has_permission("Marketplace Account", "write"))
			with self.assertRaises(frappe.PermissionError):
				import_orders(self.account.name)

	def test_import_orders_requires_marketplace_order_create(self):
		"""import_orders() requires create permission on Marketplace Order
		doctype (the enqueued worker will write new orders)."""
		# Notably NOT adding create permission
		email, _role = make_role_user(
			self,
			"Import No Create",
			{"Marketplace Account": ["write"], "Marketplace Order": ["write"]},
		)
		with self.set_user(email):
			self.assertTrue(frappe.has_permission("Marketplace Account", "write"))
			self.assertTrue(frappe.has_permission("Marketplace Order", "write"))
			self.assertFalse(frappe.has_permission("Marketplace Order", "create"))
			with self.assertRaises(frappe.PermissionError):
				import_orders(self.account.name)

	def test_import_orders_requires_marketplace_order_write(self):
		"""import_orders() requires write permission on Marketplace Order
		doctype (the enqueued worker will update existing orders too)."""
		# Notably NOT adding write permission
		email, _role = make_role_user(
			self,
			"Import No Write Order",
			{"Marketplace Account": ["write"], "Marketplace Order": ["create"]},
		)
		with self.set_user(email):
			self.assertTrue(frappe.has_permission("Marketplace Account", "write"))
			self.assertTrue(frappe.has_permission("Marketplace Order", "create"))
			self.assertFalse(frappe.has_permission("Marketplace Order", "write"))
			with self.assertRaises(frappe.PermissionError):
				import_orders(self.account.name)

	@patch("marketplace_integrations.api.noon_orders.queue_order_import")
	def test_import_orders_queues_on_long_queue(self, mock_queue):
		"""import_orders() enqueues the actual work and returns immediately
		without making Noon HTTP calls in this request."""
		mock_queue.return_value = {"queued": True}
		result = import_orders(self.account.name)
		mock_queue.assert_called_once_with(self.account.name)
		self.assertEqual(result, {"queued": True})

	@patch("marketplace_integrations.api.noon_orders.queue_order_import")
	def test_import_orders_validates_account_exists(self, mock_queue):
		"""import_orders() must fail if the account doesn't exist."""
		with self.assertRaises(frappe.DoesNotExistError):
			import_orders("NONEXISTENT-ACCOUNT")
		mock_queue.assert_not_called()


class IntegrationTestRefreshOrderEndpoint(IntegrationTestCase):
	"""Tests for refresh_order() API endpoint - permission and queueing."""

	def setUp(self):
		# Patch warehouse code validation to avoid making real Noon API calls
		self.warehouse_patch = patch(
			"marketplace_integrations.integrations.noon.catalog.validate_warehouse_code"
		)
		self.warehouse_patch.start()
		self.addCleanup(self.warehouse_patch.stop)

		suffix = frappe.generate_hash(length=8)
		self.account = _make_noon_account(f"Refresh {suffix}")
		_make_item("ITEM-TEST")
		payload = _order_payload()
		self.order = upsert_marketplace_order(self.account.name, payload)

	def tearDown(self):
		frappe.db.delete("Marketplace Order", filters={"marketplace_account": self.account.name})
		frappe.delete_doc("Marketplace Account", self.account.name, ignore_permissions=True, force=True)
		frappe.delete_doc("Noon Connection", self.account.connection, ignore_permissions=True, force=True)
		frappe.db.commit()

	def test_refresh_order_requires_marketplace_order_write(self):
		"""refresh_order() requires write permission on the specific
		Marketplace Order being refreshed."""
		email, _role = make_role_user(self, "Refresh No Write", {"Marketplace Order": ["read"]})
		with self.set_user(email):
			self.assertTrue(frappe.has_permission("Marketplace Order", "read"))
			self.assertFalse(frappe.has_permission("Marketplace Order", "write"))
			with self.assertRaises(frappe.PermissionError):
				refresh_order(self.order.name)

	@patch("marketplace_integrations.api.noon_orders.queue_order_refresh")
	def test_refresh_order_queues_on_long_queue(self, mock_queue):
		"""refresh_order() enqueues the refresh and returns immediately."""
		mock_queue.return_value = {"queued": True}
		result = refresh_order(self.order.name)
		mock_queue.assert_called_once_with(self.order.name)
		self.assertEqual(result, {"queued": True})

	@patch("marketplace_integrations.api.noon_orders.queue_order_refresh")
	def test_refresh_order_validates_order_exists(self, mock_queue):
		"""refresh_order() must fail if the order doesn't exist."""
		with self.assertRaises(frappe.DoesNotExistError):
			refresh_order("NONEXISTENT-ORDER")
		mock_queue.assert_not_called()

	@patch("marketplace_integrations.api.noon_orders.queue_order_refresh")
	def test_refresh_order_rejects_non_noon_order(self, mock_queue):
		"""refresh_order() must reject a non-Noon order before queueing
		anything (review finding D) - otherwise a Talabat/Shopify order would
		be queued and only fail inside the worker, with no Marketplace Sync
		Log row written for the failure."""
		frappe.db.set_value("Marketplace Order", self.order.name, "platform", "Talabat")
		with self.assertRaises(frappe.ValidationError):
			refresh_order(self.order.name)
		mock_queue.assert_not_called()
