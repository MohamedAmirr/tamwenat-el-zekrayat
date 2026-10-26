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
				"response_data": '{"status":"Invalid","message":"gateway declined"}',
			}
		).insert(ignore_permissions=True)

		log = self._call_as("Administrator")["latest_payment_log"]

		self.assertEqual(log["status"], "Error")
		self.assertEqual(log["message"], "gateway declined")
		self.assertNotIn("response_data", log)

	def test_shopify_amounts_use_the_shop_currency_and_the_timeline_fields_are_returned(self):
		frappe.db.set_value(
			"Marketplace Order",
			self.order.name,
			{
				"presentment_currency": "USD",
				"cancelled_at": "2026-10-01 09:00:00",
				"cancellation_reason": "customer",
				"received_on": "2026-09-30 08:00:00",
				"last_platform_update_on": "2026-10-02 10:00:00",
				"last_erpnext_sync_on": "2026-10-02 11:00:00",
			},
		)

		summary = self._call_as("Administrator")

		self.assertEqual(summary["amounts"]["currency"], "EGP")
		self.assertEqual(summary["cancellation_reason"], "customer")
		for key in ("cancelled_at", "received_on", "last_platform_update_on", "last_erpnext_sync_on"):
			self.assertTrue(summary[key], key)

	def test_talabat_and_noon_summaries_claim_no_currency(self):
		frappe.db.set_value(
			"Marketplace Order",
			self.order.name,
			{"platform": "Talabat", "payment_type": "CASH", "collect_at_pickup": 80, "shop_currency": "EGP"},
		)
		talabat = self._call_as("Administrator")
		self.assertEqual(talabat["platform"], "Talabat")
		self.assertEqual(talabat["payment_type"], "CASH")
		self.assertEqual(talabat["amounts"]["collect_at_pickup"], 80)
		self.assertEqual(talabat["amounts"]["currency"], "")

		frappe.db.set_value("Marketplace Order", self.order.name, "platform", "Noon")
		noon = self._call_as("Administrator")
		self.assertEqual(noon["platform"], "Noon")
		self.assertEqual(noon["amounts"]["currency"], "")

	def test_the_kashier_example_returns_all_three_transactions_in_order(self):
		frappe.db.set_value(
			"Marketplace Order",
			self.order.name,
			"payment_transactions",
			frappe.as_json(
				[
					{"id": "1", "kind": "SALE", "status": "ERROR", "gateway": "Paymob"},
					{"id": "2", "kind": "SALE", "status": "FAILURE", "gateway": "Kashier"},
					{"id": "3", "kind": "SALE", "status": "SUCCESS", "gateway": "Kashier"},
				]
			),
		)

		transactions = self._call_as("Administrator")["payment_transactions"]

		self.assertEqual([t["status"] for t in transactions], ["ERROR", "FAILURE", "SUCCESS"])
		self.assertEqual([t["gateway"] for t in transactions], ["Paymob", "Kashier", "Kashier"])

	def test_platform_role_filter_applies_to_the_summary(self):
		perms = {"Sales Order": ["read"], "Marketplace Order": ["read"]}
		shopify_user, _role = make_role_user(self, "SO Summary Shopify", perms)
		noon_user, _role = make_role_user(self, "SO Summary Noon", perms)
		frappe.get_doc("User", shopify_user).add_roles("Shopify User")
		frappe.get_doc("User", noon_user).add_roles("Noon User")
		frappe.clear_cache(user=shopify_user)
		frappe.clear_cache(user=noon_user)

		self.assertEqual(self._call_as(shopify_user)["name"], self.order.name)
		self.assertIsNone(self._call_as(noon_user))

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
