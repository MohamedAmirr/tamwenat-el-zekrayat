# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.integrations.shopify.test_products import make_shopify_account
from marketplace_integrations.patches.v0_0.backfill_shopify_sales_order_contact_mobile import execute
from marketplace_integrations.services import test_sales_order_sync as sync_tests
from marketplace_integrations.services.marketplace_orders import upsert_marketplace_order
from marketplace_integrations.services.sales_order_sync import sync_sales_order


class IntegrationTestBackfillShopifySalesOrderContactMobile(IntegrationTestCase):
	def setUp(self):
		self.account = make_shopify_account(f"Backfill Phone {frappe.generate_hash(length=8)}")
		self.sales_orders = []

	def make_order(self, so_phone, buyer_phone="+201001234567", platform="Shopify"):
		# A Sales Order is not needed for the patch's SQL-level write; use a real submitted one.
		so = self._sales_order()
		frappe.db.set_value("Sales Order", so, "contact_mobile", so_phone, update_modified=False)
		order = upsert_marketplace_order(
			self.account.name,
			{
				"external_order_id": f"backfill-phone-{frappe.generate_hash(length=8)}",
				"latest_payload": {"id": "1"},
				"customer_phone_number": buyer_phone,
				"items": [],
			},
		)
		frappe.db.set_value(
			"Marketplace Order",
			order.name,
			{"erpnext_sales_order": so, "platform": platform},
			update_modified=False,
		)
		return so

	def _sales_order(self):
		helper = sync_tests.IntegrationTestSalesOrderSync()
		helper.setUp()
		account = helper._make_shopify_account()
		order, _mapping = helper._make_shopify_order(account)
		return sync_sales_order(order.name)["sales_order"]

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
