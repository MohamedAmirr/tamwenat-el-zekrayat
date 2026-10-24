# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.api.sales_order_marketplace import get_marketplace_order_summary
from marketplace_integrations.integrations.shopify.test_products import make_shopify_account
from marketplace_integrations.services.linked_sales_order_fixture import LinkedSalesOrderFixture
from marketplace_integrations.tests.utils import make_role_user


class IntegrationTestSalesOrderMarketplaceSummary(IntegrationTestCase):
	def setUp(self):
		# The success-path summary tests exercise the unrestricted Administrator read path
		# deliberately; permission-specific cases switch to ordinary users explicitly.
		frappe.set_user("Administrator")
		self.fixture = LinkedSalesOrderFixture()
		self.suffix = frappe.generate_hash(length=8)
		self.account = make_shopify_account(f"SO Marketplace Summary {self.suffix}")
		self.order = frappe.get_doc(
			{
				"doctype": "Marketplace Order",
				"marketplace_account": self.account.name,
				"external_order_id": f"EXT-{self.suffix}",
				"platform": "Shopify",
				"platform_order_reference": f"#SHOP-{self.suffix}",
				"external_status": "OPEN",
				"external_financial_status": "PARTIALLY_PAID",
				"customer_first_name": "Casey",
				"customer_last_name": "Buyer",
				"customer_phone_number": "01000000000",
				"payment_transactions": frappe.as_json(
					[{"id": f"txn-{self.suffix}", "kind": "SALE", "status": "FAILURE"}]
				),
				"order_total": 125,
				"shop_currency": "EGP",
				"erpnext_sync_status": "Synced",
				"erpnext_sales_order": self.fixture.name,
			}
		).insert()
		frappe.db.set_value(
			"Sales Order", self.fixture.name, "custom_marketplace_order", self.order.name
		)
		self.addCleanup(self._cleanup)

	def _cleanup(self):
		frappe.set_user("Administrator")
		frappe.db.delete("Marketplace Sync Log", {"marketplace_order": self.order.name})
		frappe.db.set_value("Sales Order", self.fixture.name, "custom_marketplace_order", None)
		self.fixture.cleanup()
		frappe.delete_doc("Marketplace Order", self.order.name, force=True, ignore_permissions=True)
		frappe.delete_doc("Marketplace Account", self.account.name, force=True, ignore_permissions=True)

	def test_summary_returns_linked_order_payment_and_sync_values(self):
		summary = self._call_as("Administrator")

		self.assertEqual(summary["reference"], f"#SHOP-{self.suffix}")
		self.assertEqual(summary["customer"], "Casey Buyer")
		self.assertEqual(summary["payment_transactions"][0]["status"], "FAILURE")
		self.assertEqual(summary["amounts"]["order_total"], 125)
		self.assertEqual(summary["erpnext_sync_status"], "Synced")

	def test_latest_payment_sync_log_is_returned(self):
		frappe.get_doc(
			{
				"doctype": "Marketplace Sync Log",
				"marketplace_order": self.order.name,
				"marketplace_account": self.account.name,
				"sync_type": "Payment",
				"trigger_source": "Manual",
				"status": "Error",
				"attempted_at": frappe.utils.now_datetime(),
				"response_data": '{"error":"gateway declined"}',
			}
		).insert(ignore_permissions=True)

		log = self._call_as("Administrator")["latest_payment_log"]

		self.assertEqual(log["status"], "Error")
		self.assertIn("gateway declined", log["response_data"])

	def test_sales_order_reader_without_marketplace_read_gets_no_marketplace_data(self):
		email, _role = make_role_user(self, "SO Summary No MO", {"Sales Order": ["read"]})
		self.assertIsNone(self._call_as(email))

	def test_user_without_sales_order_read_is_refused(self):
		email, _role = make_role_user(self, "SO Summary No SO", {"Customer": ["read"]})
		with self.assertRaises(frappe.PermissionError):
			self._call_as(email)

	def _call_as(self, user):
		previous_user = frappe.session.user
		frappe.set_user(user)
		try:
			return get_marketplace_order_summary(self.fixture.name)
		finally:
			frappe.set_user(previous_user)

	def test_unlinked_order_is_not_returned(self):
		frappe.db.set_value("Marketplace Order", self.order.name, "erpnext_sales_order", None)
		self.assertIsNone(self._call_as("Administrator"))
