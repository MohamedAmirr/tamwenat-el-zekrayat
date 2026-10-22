# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.api.order_hover_card import MAX_ITEMS, get_sales_order_card
from marketplace_integrations.integrations.shopify.test_products import make_shopify_account
from marketplace_integrations.services.linked_sales_order_fixture import LinkedSalesOrderFixture
from marketplace_integrations.tests.utils import make_role_user


class IntegrationTestOrderHoverCard(IntegrationTestCase):
	def setUp(self):
		self.fixture = LinkedSalesOrderFixture()
		self.addCleanup(self._delete_fixtures)
		self.suffix = frappe.generate_hash(length=8)
		self.account = make_shopify_account(f"Hover Card Account {self.suffix}")
		self.marketplace_order = frappe.get_doc(
			{
				"doctype": "Marketplace Order",
				"marketplace_account": self.account.name,
				"external_order_id": f"EXT-{self.suffix}",
				"platform": "Shopify",
				"platform_order_reference": f"#PLAT-{self.suffix}",
				"external_financial_status": "PAID",
				"erpnext_sales_order": self.fixture.name,
			}
		).insert()
		frappe.db.set_value(
			"Marketplace Order", self.marketplace_order.name, "last_sync_error", "Item is not mapped"
		)

	def _delete_fixtures(self):
		frappe.set_user("Administrator")
		self.fixture.cleanup()
		frappe.delete_doc(
			"Marketplace Order", self.marketplace_order.name, force=True, ignore_permissions=True
		)
		frappe.delete_doc("Marketplace Account", self.account.name, force=True, ignore_permissions=True)

	def _call_as(self, email):
		frappe.set_user(email)
		try:
			return get_sales_order_card(self.fixture.name)
		finally:
			frappe.set_user("Administrator")

	def test_card_for_a_user_who_can_read_everything(self):
		card = get_sales_order_card(self.fixture.name)

		self.assertEqual(card["sales_order"], self.fixture.name)
		self.assertEqual(card["customer"], self.fixture.customer.customer_name)
		self.assertEqual(
			card["items"], [{"item_name": self.fixture.item.item_name, "qty": 1.0, "uom": "Nos"}]
		)
		self.assertFalse(card["more_items"])
		self.assertEqual(card["marketplace"]["reference"], f"#PLAT-{self.suffix}")
		self.assertEqual(card["marketplace"]["platform"], "Shopify")
		self.assertEqual(card["marketplace"]["payment_status"], "PAID")
		self.assertEqual(card["marketplace"]["last_sync_error"], "Item is not mapped")
		self.assertIsNone(card["bosta"])

	def test_reference_falls_back_to_the_external_order_id_never_the_mktord_name(self):
		frappe.db.set_value(
			"Marketplace Order", self.marketplace_order.name, "platform_order_reference", None
		)

		reference = get_sales_order_card(self.fixture.name)["marketplace"]["reference"]

		self.assertEqual(reference, f"EXT-{self.suffix}")
		self.assertNotIn("MKTORD-", reference)

	def test_order_without_a_marketplace_order_has_no_marketplace_part(self):
		frappe.db.set_value("Marketplace Order", self.marketplace_order.name, "erpnext_sales_order", None)

		self.assertIsNone(get_sales_order_card(self.fixture.name)["marketplace"])

	def test_user_without_sales_order_read_is_refused(self):
		email, _role = make_role_user(self, "Hover No SO", {"Customer": ["read"]})

		with self.assertRaises(frappe.PermissionError):
			self._call_as(email)

	def test_sales_order_reader_without_marketplace_read_gets_no_marketplace_fields(self):
		email, _role = make_role_user(self, "Hover SO Only", {"Sales Order": ["read"]})

		card = self._call_as(email)

		self.assertEqual(card["customer"], self.fixture.customer.customer_name)
		self.assertTrue(card["items"])
		self.assertIsNone(card["marketplace"])
		self.assertIsNone(card["bosta"])
		self.assertNotIn("Item is not mapped", frappe.as_json(card))
		self.assertNotIn(f"#PLAT-{self.suffix}", frappe.as_json(card))

	def test_item_list_is_capped_and_flagged(self):
		order = frappe.get_doc("Sales Order", self.fixture.name)
		for _ in range(MAX_ITEMS):
			first = order.items[0]
			order.append(
				"items",
				{
					"item_code": self.fixture.item.name,
					"qty": 1,
					"rate": 10,
					"uom": "Nos",
					"warehouse": first.warehouse,
					"delivery_date": first.delivery_date,
				},
			)
		order.save()

		card = get_sales_order_card(self.fixture.name)

		self.assertEqual(len(card["items"]), MAX_ITEMS)
		self.assertTrue(card["more_items"])
