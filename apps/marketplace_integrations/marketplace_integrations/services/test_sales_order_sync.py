# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, nowdate

from marketplace_integrations.services.marketplace_orders import upsert_marketplace_order
from marketplace_integrations.services.sales_order_sync import sync_sales_order


class IntegrationTestSalesOrderSync(IntegrationTestCase):
	def setUp(self):
		self.suffix = frappe.generate_hash(length=8)
		self.company = frappe.defaults.get_global_default("company")
		self.price_list = frappe.get_all(
			"Price List", filters={"selling": 1, "enabled": 1}, pluck="name", limit=1
		)[0]
		self.item = self._make_item()
		self.account = self._make_account()
		self._make_mapping("SKU-UNIT", self.item.name)

	def _make_item(self):
		item = frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": f"TEST-SO-SYNC-{self.suffix}",
				"item_group": "Products",
				"stock_uom": "Nos",
				"is_stock_item": 0,
			}
		).insert()
		frappe.get_doc(
			{
				"doctype": "Item Price",
				"price_list": self.price_list,
				"item_code": item.name,
				"price_list_rate": 17,
				"selling": 1,
			}
		).insert()
		return item

	def _make_account(self):
		connection = frappe.get_doc(
			{
				"doctype": "Talabat Connection",
				"connection_name": f"Sales Order Sync {self.suffix}",
				"client_id": f"sales-order-sync-client-{self.suffix}",
				"client_secret": "test-secret",
				"chain_id": f"sales-order-sync-chain-{self.suffix}",
			}
		).insert()
		return frappe.get_doc(
			{
				"doctype": "Marketplace Account",
				"account_name": f"Sales Order Sync Account {self.suffix}",
				"platform": "Talabat",
				"external_account_id": f"sales-order-sync-vendor-{self.suffix}",
				"connection_doctype": "Talabat Connection",
				"connection": connection.name,
				"erpnext_company": self.company,
				"erpnext_selling_price_list": self.price_list,
			}
		).insert()

	def _make_mapping(self, sku, item):
		return frappe.get_doc(
			{
				"doctype": "Marketplace Product Mapping",
				"marketplace_account": self.account.name,
				"item": item,
				"external_reference": sku,
				"sales_uom": "Nos",
			}
		).insert()

	def _make_order(self, sku="SKU-UNIT", pricing_type="UNIT"):
		return upsert_marketplace_order(
			self.account.name,
			{
				"external_order_id": f"sales-order-sync-order-{self.suffix}-{frappe.generate_hash(length=4)}",
				"latest_payload": {"order_id": "external-order"},
				"promised_for": add_days(nowdate(), 1),
				"customer_first_name": "Talabat Customer",
				"customer_phone_number": "masked",
				"order_total": 999,
				"items": [
					{
						"external_item_id": "line-1",
						"external_sku": sku,
						"external_status": "IN_CART",
						"pricing_type": pricing_type,
						"fulfilled_quantity": 2,
						"fulfilled_weight": 1.25 if pricing_type == "KG" else None,
						"fulfilled_unit_price": 499.5,
						"fulfilled_total_price": 999,
					}
				],
			},
		)

	def test_creates_one_draft_sales_order_with_erpnext_price_list_values(self):
		order = self._make_order()

		result = sync_sales_order(order.name)
		order.reload()
		self.account.reload()
		sales_order = frappe.get_doc("Sales Order", order.erpnext_sales_order)

		self.assertTrue(result["ok"])
		self.assertEqual(order.erpnext_sync_status, "Synced")
		self.assertEqual(sales_order.docstatus, 0)
		self.assertEqual(sales_order.company, self.company)
		self.assertEqual(sales_order.selling_price_list, self.price_list)
		self.assertEqual(str(sales_order.delivery_date), str(order.promised_for)[:10])
		self.assertEqual(sales_order.customer, self.account.erpnext_customer)
		self.assertEqual(sales_order.items[0].item_code, self.item.name)
		self.assertEqual(sales_order.items[0].qty, 2)
		self.assertEqual(sales_order.items[0].rate, 17)
		self.assertNotEqual(sales_order.grand_total, order.order_total)
		self.assertFalse(frappe.db.get_value("Customer", self.account.erpnext_customer, "mobile_no"))

	def test_retry_does_not_create_a_second_sales_order(self):
		order = self._make_order()
		first = sync_sales_order(order.name)
		second = sync_sales_order(order.name)

		self.assertTrue(first["ok"])
		self.assertTrue(second["ok"])
		self.assertEqual(first["sales_order"], second["sales_order"])
		self.assertEqual(frappe.db.count("Sales Order", {"name": first["sales_order"]}), 1)

	def test_changed_fulfilled_quantity_updates_the_same_draft_sales_order(self):
		order = self._make_order()
		first = sync_sales_order(order.name)
		order.reload()
		order.items[0].fulfilled_quantity = 3
		order.save()
		order.reload()

		self.assertEqual(order.erpnext_sync_status, "Modified After Sync")
		second = sync_sales_order(order.name)
		sales_order = frappe.get_doc("Sales Order", first["sales_order"])
		order.reload()

		self.assertTrue(second["ok"])
		self.assertEqual(second["sales_order"], first["sales_order"])
		self.assertEqual(sales_order.items[0].qty, 3)
		self.assertEqual(order.erpnext_sync_status, "Synced")

	def test_unmapped_final_line_blocks_without_creating_a_sales_order(self):
		order = self._make_order(sku="UNMAPPED-SKU")

		result = sync_sales_order(order.name)
		order.reload()

		self.assertFalse(result["ok"])
		self.assertIn("UNMAPPED-SKU", result["error"])
		self.assertFalse(order.erpnext_sales_order)
		self.assertEqual(order.erpnext_sync_status, "Sync Error")

	def test_kg_line_blocks_until_a_uom_policy_is_configured(self):
		self._make_mapping("SKU-KG", self.item.name)
		order = self._make_order(sku="SKU-KG", pricing_type="KG")

		result = sync_sales_order(order.name)

		self.assertFalse(result["ok"])
		self.assertIn("KG", result["error"])

	def test_submitted_sales_order_remains_modified_after_sync(self):
		order = self._make_order()
		first = sync_sales_order(order.name)
		sales_order = frappe.get_doc("Sales Order", first["sales_order"])
		sales_order.submit()
		order.reload()
		order.items[0].fulfilled_quantity = 3
		order.save()

		result = sync_sales_order(order.name)
		order.reload()

		self.assertFalse(result["ok"])
		self.assertEqual(order.erpnext_sync_status, "Modified After Sync")
		self.assertIn("submitted", result["error"])

	def test_talabat_only_changes_do_not_dirty_the_sales_order_snapshot(self):
		order = self._make_order()
		sync_sales_order(order.name)
		order.reload()
		order.customer_phone_number = "new masked value"
		order.external_status = "READY_FOR_PICKUP"
		order.save()
		order.reload()

		self.assertEqual(order.erpnext_sync_status, "Synced")
