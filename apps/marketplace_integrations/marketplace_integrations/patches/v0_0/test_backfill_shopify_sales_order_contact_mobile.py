# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.patches.v0_0.backfill_shopify_sales_order_contact_mobile import execute
from marketplace_integrations.services import test_sales_order_sync as sync_tests
from marketplace_integrations.services.sales_order_sync import sync_sales_order


class IntegrationTestBackfillShopifySalesOrderContactMobile(IntegrationTestCase):
	def setUp(self):
		# One set of sync fixtures (item, price list, Shopify account) shared by every order.
		self.sync = sync_tests.IntegrationTestSalesOrderSync()
		self.sync.setUp()
		self.account = self.sync._make_shopify_account()

	def make_order(self, so_phone, buyer_phone="+201001234567", platform="Shopify"):
		# Uses a real submitted Sales Order so the patch's write on a submitted order is exercised.
		order, _mapping = self.sync._make_shopify_order(self.account, customer_phone_number=buyer_phone)
		so = sync_sales_order(order.name)["sales_order"]
		frappe.db.set_value("Sales Order", so, "contact_mobile", so_phone, update_modified=False)
		frappe.db.set_value("Marketplace Order", order.name, "platform", platform, update_modified=False)
		return so

	def test_fills_only_empty_phones_and_is_idempotent(self):
		empty = self.make_order(None)
		typed = self.make_order("01112223334")
		talabat = self.make_order(None, platform="Talabat")

		execute()
		execute()

		get = lambda so: frappe.db.get_value("Sales Order", so, "contact_mobile")  # noqa: E731
		self.assertEqual(get(empty), "+201001234567")
		self.assertEqual(get(typed), "01112223334")
		self.assertFalse(get(talabat))
