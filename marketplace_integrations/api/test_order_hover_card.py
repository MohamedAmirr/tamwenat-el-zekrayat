# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.api.order_hover_card import MAX_ITEMS, get_sales_order_card
from marketplace_integrations.integrations.shopify.test_products import make_shopify_account
from marketplace_integrations.marketplace_integrations.doctype.bosta_shipment.bosta_shipment import (
	save_as_system,
)
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
		for name in frappe.get_all(
			"Bosta Shipment", filters={"sales_order": self.fixture.name}, pluck="name"
		):
			frappe.delete_doc("Bosta Shipment", name, force=True, ignore_permissions=True)
		frappe.db.delete("Marketplace Sync Log", {"marketplace_order": self.marketplace_order.name})
		self.fixture.cleanup()
		frappe.delete_doc(
			"Marketplace Order", self.marketplace_order.name, force=True, ignore_permissions=True
		)
		frappe.delete_doc("Marketplace Account", self.account.name, force=True, ignore_permissions=True)

	def _make_sync_log(self, status):
		return frappe.get_doc(
			{
				"doctype": "Marketplace Sync Log",
				"marketplace_order": self.marketplace_order.name,
				"marketplace_account": self.account.name,
				"sync_type": "Sales Order Sync",
				"trigger_source": "Manual",
				"status": status,
				"attempted_at": frappe.utils.now_datetime(),
			}
		).insert(ignore_permissions=True)

	def _make_shipment(self):
		return save_as_system(
			frappe.get_doc(
				{
					"doctype": "Bosta Shipment",
					"sales_order": self.fixture.name,
					"status": "Bosta Created",
					"receiver_name": "Hover Receiver",
					"receiver_phone": "01000000000",
					"city": "Cairo",
					"address_first_line": "12 Example Street",
					"tracking_number": f"TRK-{self.suffix}",
					"bosta_state_value": "Out for delivery",
				}
			),
			insert=True,
		)

	def _call_as(self, email, sales_order=None):
		frappe.set_user(email)
		try:
			return get_sales_order_card(sales_order or self.fixture.name)
		finally:
			frappe.set_user("Administrator")

	def test_card_for_a_user_who_can_read_everything(self):
		card = get_sales_order_card(self.fixture.name)

		self.assertEqual(card["sales_order"], self.fixture.name)
		self.assertEqual(card["customer"], self.fixture.customer.customer_name)
		self.assertEqual(card["items"], [{"item_name": self.fixture.item.item_name, "qty": 1.0}])
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

	def test_user_without_sales_order_read_gets_permission_error_for_a_missing_name_too(self):
		email, _role = make_role_user(self, "Hover No SO Missing", {"Customer": ["read"]})

		# The same error whether or not the name exists: no existence probing.
		with self.assertRaises(frappe.PermissionError):
			self._call_as(email, "SAL-ORD-DOES-NOT-EXIST")

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

	def test_response_holds_only_what_the_card_renders(self):
		self._make_sync_log("Error")
		self._make_shipment()

		card = get_sales_order_card(self.fixture.name)

		self.assertEqual(
			set(card),
			{
				"sales_order",
				"customer",
				"phone",
				"status",
				"currency",
				"total",
				"items",
				"more_items",
				"marketplace",
				"bosta",
			},
		)
		self.assertEqual(
			set(card["marketplace"]),
			{"reference", "platform", "payment_status", "last_sync_error", "last_sync"},
		)
		self.assertEqual(set(card["marketplace"]["last_sync"]), {"status"})
		self.assertEqual(set(card["bosta"]), {"status", "tracking_number"})

	def test_last_sync_is_the_latest_sync_logs_status(self):
		self._make_sync_log("Success")
		self._make_sync_log("Error")

		card = get_sales_order_card(self.fixture.name)

		self.assertEqual(card["marketplace"]["last_sync"], {"status": "Error"})

	def _make_platform_user(self, label, platform_role="Shopify User"):
		"""A Sales Order reader holding the platform role of the fixture order's account."""
		email, _role = make_role_user(self, label, {"Sales Order": ["read"]})
		frappe.get_doc("User", email).add_roles(platform_role)
		frappe.clear_cache(user=email)
		return email

	def test_marketplace_reader_without_a_platform_role_gets_no_marketplace_part(self):
		# #322: rows are filtered by platform role, so a plain Marketplace Order reader sees none.
		self._make_sync_log("Error")
		email, _role = make_role_user(
			self, "Hover No Platform", {"Sales Order": ["read"], "Marketplace Order": ["read"]}
		)

		card = self._call_as(email)

		self.assertIsNone(card["marketplace"])
		self.assertNotIn(f"#PLAT-{self.suffix}", frappe.as_json(card))
		self.assertNotIn("Item is not mapped", frappe.as_json(card))
		self.assertIsNone(card["bosta"])

	def test_bosta_part_does_not_depend_on_a_platform_role(self):
		self._make_shipment()
		email, _role = make_role_user(
			self,
			"Hover Bosta No Platform",
			{"Sales Order": ["read"], "Marketplace Order": ["read"], "Bosta Shipment": ["read"]},
		)

		card = self._call_as(email)

		self.assertIsNone(card["marketplace"])
		self.assertEqual(card["bosta"]["tracking_number"], f"TRK-{self.suffix}")

	def test_platform_user_gets_the_marketplace_part_with_last_sync(self):
		self._make_sync_log("Error")
		email = self._make_platform_user("Hover Shopify User")

		card = self._call_as(email)

		self.assertEqual(card["marketplace"]["reference"], f"#PLAT-{self.suffix}")
		self.assertEqual(card["marketplace"]["platform"], "Shopify")
		self.assertEqual(card["marketplace"]["last_sync"], {"status": "Error"})

	def test_user_of_another_platform_gets_no_marketplace_part(self):
		email = self._make_platform_user("Hover Talabat User", "Talabat User")

		self.assertIsNone(self._call_as(email)["marketplace"])

	def test_last_sync_is_hidden_when_sync_log_read_is_denied(self):
		# Platform roles grant full Sync Log rights, so a platform user without Sync Log read
		# cannot exist; the mock is the only way to reach the permission gate in _last_sync.
		self._make_sync_log("Error")
		email = self._make_platform_user("Hover Shopify No Log")
		real_has_permission = frappe.has_permission

		def has_permission(doctype=None, ptype="read", doc=None, user=None, throw=False, **kwargs):
			if doctype == "Marketplace Sync Log" and ptype == "read":
				if throw:
					raise frappe.PermissionError
				return False
			return real_has_permission(doctype, ptype, doc, user, throw, **kwargs)

		with patch("frappe.has_permission", has_permission):
			card = self._call_as(email)

		self.assertEqual(card["marketplace"]["reference"], f"#PLAT-{self.suffix}")
		self.assertIsNone(card["marketplace"]["last_sync"])

	def test_bosta_part_shows_the_shipments_state_and_tracking_number(self):
		self._make_shipment()

		card = get_sales_order_card(self.fixture.name)

		self.assertEqual(
			card["bosta"], {"status": "Out for delivery", "tracking_number": f"TRK-{self.suffix}"}
		)

	def test_bosta_part_is_hidden_from_a_user_without_bosta_shipment_read(self):
		self._make_shipment()
		email, _role = make_role_user(self, "Hover No Bosta", {"Sales Order": ["read"]})

		card = self._call_as(email)

		self.assertIsNone(card["bosta"])
		self.assertNotIn(f"TRK-{self.suffix}", frappe.as_json(card))

	def test_deleted_order_is_a_validation_error_not_a_404(self):
		# DoesNotExistError (HTTP 404) makes Desk open a modal even for silent calls.
		with self.assertRaises(frappe.ValidationError) as caught:
			get_sales_order_card("SAL-ORD-DOES-NOT-EXIST")

		self.assertNotIsInstance(caught.exception, frappe.DoesNotExistError)
		self.assertEqual(caught.exception.http_status_code, 417)

	def test_deleted_order_for_a_sales_order_reader_is_still_a_validation_error(self):
		email, _role = make_role_user(self, "Hover SO Reader Gone", {"Sales Order": ["read"]})

		with self.assertRaises(frappe.ValidationError) as caught:
			self._call_as(email, "SAL-ORD-DOES-NOT-EXIST")

		self.assertNotIsInstance(caught.exception, frappe.PermissionError)
		self.assertEqual(caught.exception.http_status_code, 417)
