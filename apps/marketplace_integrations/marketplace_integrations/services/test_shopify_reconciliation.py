# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Tests for the Shopify Reconciliation dashboard's Section 4 (Order Import
failures) live queries (#139)."""

from datetime import timedelta
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import now_datetime

from marketplace_integrations.integrations.shopify.orders import _log_order_import_failure
from marketplace_integrations.integrations.shopify.test_products import make_shopify_account
from marketplace_integrations.marketplace_integrations.doctype.marketplace_account.test_marketplace_account import (
	STAND_IN_CONNECTION,
	STAND_IN_CONNECTION_DOCTYPE,
)
from marketplace_integrations.marketplace_integrations.doctype.marketplace_order.marketplace_order import (
	compute_order_identity_key,
)
from marketplace_integrations.services.shopify_reconciliation import get_order_import_failures

NOON_PEM = "-----BEGIN RSA PRIVATE KEY-----\ntest\n-----END RSA PRIVATE KEY-----"


def make_shopify_order(account_name, **overrides):
	suffix = frappe.generate_hash(length=8)
	values = {
		"doctype": "Marketplace Order",
		"marketplace_account": account_name,
		"external_order_id": f"reconciliation-order-{suffix}",
		"platform": "Shopify",
		"order_code": f"#{suffix}",
		**overrides,
	}
	order = frappe.get_doc(values)
	order.flags.skip_sync_status_update = True
	order.order_identity_key = compute_order_identity_key(account_name, order.external_order_id)
	order.insert()
	return order


class IntegrationTestGetOrderImportFailures(IntegrationTestCase):
	def setUp(self):
		self._created = []

	def tearDown(self):
		for doctype, name in reversed(self._created):
			if frappe.db.exists(doctype, name):
				frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)

	def _track(self, doctype, name):
		self._created.append((doctype, name))

	def make_account(self):
		account = make_shopify_account(f"Order Import Failures {frappe.generate_hash(length=8)}")
		self._track("Shopify Connection", account.connection)
		self._track("Marketplace Account", account.name)
		return account

	def make_other_platform_account(self, platform):
		code = f"import-failures-{platform.lower()}-{frappe.generate_hash(length=8)}"
		values = {
			"doctype": "Marketplace Account",
			"account_name": code,
			"platform": platform,
			"external_account_id": code,
		}
		if platform == "Noon":
			connection = frappe.get_doc(
				{
					"doctype": "Noon Connection",
					"connection_name": code,
					"project_code": code,
					"key_id": "test-key-id",
					"private_key": NOON_PEM,
					"environment": "Static Sandbox",
					"enabled": 1,
				}
			).insert()
			self._track("Noon Connection", connection.name)
			values.update(
				{
					"connection_doctype": "Noon Connection",
					"connection": connection.name,
					"noon_warehouse_code": "NOON-WH-FAILURES",
					"noon_country_code": "ae",
				}
			)
			with patch("marketplace_integrations.integrations.noon.catalog.validate_warehouse_code"):
				account = frappe.get_doc(values).insert()
		else:
			values.update(
				{"connection_doctype": STAND_IN_CONNECTION_DOCTYPE, "connection": STAND_IN_CONNECTION}
			)
			account = frappe.get_doc(values).insert()
		self._track("Marketplace Account", account.name)
		return account

	def make_failure(self, account, **overrides):
		values = {
			"doctype": "Marketplace Sync Log",
			"sync_type": "Order Import",
			"marketplace_account": account.name,
			"status": "Error",
			"attempted_at": now_datetime(),
			"request_data": frappe.as_json({"account": account.name, "order_gid": "gid://shopify/Order/1"}),
			"traceback": "boom",
			**overrides,
		}
		log = frappe.get_doc(values).insert()
		self._track("Marketplace Sync Log", log.name)
		return log

	def _names(self):
		return [row.name for row in get_order_import_failures()]

	def test_returns_only_order_import_error_rows(self):
		account = self.make_account()
		failure = self.make_failure(account)
		# A Sales Order Sync error row must not show up here.
		order = make_shopify_order(account.name)
		self._track("Marketplace Order", order.name)
		sync_error = frappe.get_doc(
			{
				"doctype": "Marketplace Sync Log",
				"sync_type": "Sales Order Sync",
				"marketplace_order": order.name,
				"marketplace_account": account.name,
				"status": "Error",
				"attempted_at": now_datetime(),
			}
		).insert()
		self._track("Marketplace Sync Log", sync_error.name)

		names = self._names()

		self.assertIn(failure.name, names)
		self.assertNotIn(sync_error.name, names)

	def test_lists_the_row_written_by_log_order_import_failure(self):
		account = self.make_account()

		with patch("frappe.db.commit"):
			_log_order_import_failure(account, "gid://shopify/Order/77", "import exploded", "Scheduled Reconciliation")
		log = frappe.get_last_doc("Marketplace Sync Log", {"marketplace_account": account.name})
		self._track("Marketplace Sync Log", log.name)

		self.assertEqual(log.sync_type, "Order Import")
		self.assertEqual(log.status, "Error")
		self.assertEqual(log.trigger_source, "Scheduled Reconciliation")
		self.assertEqual(log.traceback, "import exploded")
		self.assertEqual(
			frappe.parse_json(log.request_data),
			{"account": account.name, "order_gid": "gid://shopify/Order/77"},
		)
		self.assertIn(log.name, self._names())

	def test_excludes_other_platforms_and_rows_without_an_account(self):
		shopify = self.make_account()
		talabat = self.make_other_platform_account("Talabat")
		noon = self.make_other_platform_account("Noon")
		shopify_log = self.make_failure(shopify)
		talabat_log = self.make_failure(
			talabat, request_data=frappe.as_json({"account": talabat.name, "order_id": "t-1"})
		)
		noon_log = self.make_failure(
			noon, request_data=frappe.as_json({"account": noon.name, "fbpi_order_nr": "n-1"})
		)
		no_account = frappe.get_doc(
			{
				"doctype": "Marketplace Sync Log",
				"sync_type": "Order Import",
				"status": "Error",
				"attempted_at": now_datetime(),
			}
		).insert()
		self._track("Marketplace Sync Log", no_account.name)

		names = self._names()

		self.assertIn(shopify_log.name, names)
		self.assertNotIn(talabat_log.name, names)
		self.assertNotIn(noon_log.name, names)
		self.assertNotIn(no_account.name, names)

	def test_hides_a_failure_recovered_by_a_later_import(self):
		account = self.make_account()
		recovered = self.make_failure(
			account,
			request_data=frappe.as_json({"account": account.name, "order_gid": "gid://shopify/Order/5"}),
			attempted_at=now_datetime() - timedelta(minutes=1),
		)
		still_failing = self.make_failure(
			account,
			request_data=frappe.as_json({"account": account.name, "order_gid": "gid://shopify/Order/6"}),
		)
		# Shopify's updatedAt predates the failed attempt; the order was
		# created (imported) after it.
		order = make_shopify_order(
			account.name,
			external_order_id="gid://shopify/Order/5",
			last_platform_update_on=now_datetime() - timedelta(hours=1),
		)
		self._track("Marketplace Order", order.name)

		names = self._names()

		self.assertNotIn(recovered.name, names)
		self.assertIn(still_failing.name, names)

	def test_keeps_the_limit_and_most_recent_first_order(self):
		account = self.make_account()
		base = now_datetime()
		logs = [
			self.make_failure(account, attempted_at=base - timedelta(seconds=index)) for index in range(3)
		]

		rows = get_order_import_failures(limit=2)

		self.assertEqual([row.name for row in rows], [log.name for log in logs[:2]])

	def test_requires_system_manager(self):
		frappe.set_user("Guest")
		try:
			with self.assertRaises(frappe.PermissionError):
				get_order_import_failures()
		finally:
			frappe.set_user("Administrator")
