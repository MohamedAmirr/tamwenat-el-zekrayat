# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Thin API-boundary tests for api/shopify_orders.py - the real business
logic (import_order()/list_order_ids_updated_since()/is_stale, etc.) is
exercised by integrations/shopify/test_orders.py; these confirm delegation,
whitelisting metadata, and - per review finding #1 - that queue_order_import()/
refresh_order() actually reject a user with no read/write on the Marketplace
Account through the REAL (unmocked) whitelisted boundary, mirroring
api/test_shopify_products.py's IntegrationTestShopifyProductsApiPermissions
pattern."""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.api.shopify_orders import (
	get_order_import_status,
	queue_order_import,
	refresh_order,
)
from marketplace_integrations.integrations.shopify.test_products import make_shopify_account


class IntegrationTestShopifyOrdersApi(IntegrationTestCase):
	def test_queue_order_import_delegates(self):
		# api/shopify_orders.py imports queue_order_import as
		# `_queue_order_import` (name-bound at import time), so the patch
		# target is that bound name in THIS module's namespace, not the
		# original orders.queue_order_import attribute.
		with patch(
			"marketplace_integrations.api.shopify_orders._queue_order_import",
			return_value={"job_id": "abc", "status": "queued"},
		) as mocked:
			result = queue_order_import("Some Account", "2026-09-01T00:00:00Z", "2026-09-27T00:00:00Z")

		mocked.assert_called_once_with(
			"Some Account", "2026-09-01T00:00:00Z", "2026-09-27T00:00:00Z"
		)
		self.assertEqual(result, {"job_id": "abc", "status": "queued"})

	def test_get_order_import_status_delegates(self):
		with patch(
			"marketplace_integrations.api.shopify_orders._get_order_import_status",
			return_value={"status": "running", "processed": 3},
		) as mocked:
			result = get_order_import_status("job-1")

		mocked.assert_called_once_with("job-1")
		self.assertEqual(result, {"status": "running", "processed": 3})

	def test_queue_order_import_is_post_only(self):
		self.assertEqual(
			frappe.allowed_http_methods_for_whitelisted_func[queue_order_import], ["POST"]
		)

	def test_refresh_order_is_post_only(self):
		self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[refresh_order], ["POST"])

	def test_get_order_import_status_is_post_only(self):
		self.assertEqual(
			frappe.allowed_http_methods_for_whitelisted_func[get_order_import_status], ["POST"]
		)


def _make_role_only_user(email_prefix):
	return frappe.get_doc(
		{
			"doctype": "User",
			"email": f"{email_prefix}-{frappe.generate_hash(length=10)}@example.com",
			"first_name": "Shopify Order Permission Test User",
			"send_welcome_email": 0,
			"roles": [{"role": "All"}],
		}
	).insert()


class IntegrationTestShopifyOrdersApiPermissions(IntegrationTestCase):
	"""Exercises actual permission rejection through the real (unmocked)
	api.shopify_orders.queue_order_import()/refresh_order() boundaries."""

	def setUp(self):
		self.account = make_shopify_account(f"Order Perm Account {frappe.generate_hash(length=8)}")
		self.order = frappe.get_doc(
			{
				"doctype": "Marketplace Order",
				"marketplace_account": self.account.name,
				"external_order_id": f"gid://shopify/Order/{frappe.generate_hash(length=8)}",
			}
		).insert()

	def test_queue_order_import_rejects_user_without_account_read_permission(self):
		# No share/role grants this user anything on the account -
		# queue_order_import()'s own account.check_permission("read") must
		# reject before any job is enqueued.
		user = _make_role_only_user("shopify-order-import-no-read")

		with self.set_user(user.name):
			with self.assertRaises(frappe.PermissionError):
				queue_order_import(self.account.name, "2026-09-01T00:00:00Z", "2026-09-27T00:00:00Z")

	def test_refresh_order_rejects_user_without_order_write_permission(self):
		# Read-only share on the Marketplace Order is not enough -
		# refresh_order() requires `write` on the specific document.
		user = _make_role_only_user("shopify-order-refresh-no-write")
		frappe.share.add("Marketplace Order", self.order.name, user.name, read=1)

		with self.set_user(user.name):
			with self.assertRaises(frappe.PermissionError):
				refresh_order(self.order.name)

	def test_refresh_order_rejects_user_with_no_grant_at_all(self):
		user = _make_role_only_user("shopify-order-refresh-no-grant")

		with self.set_user(user.name):
			with self.assertRaises(frappe.PermissionError):
				refresh_order(self.order.name)
