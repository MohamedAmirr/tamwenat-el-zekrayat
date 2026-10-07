# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Test-only fixture (#178): a REAL draft Sales Order a test can link a Marketplace
Order to, so re-import/``save()`` link validation passes.  Not collected by the test
runner (no ``test_`` prefix) and not imported by production code.

Built the way ``services/test_sales_order_sync.py`` builds its prerequisites: a
non-stock Item, a generic ``Company`` Customer, the default company and an enabled
selling Price List and Warehouse.  A draft (never submitted) is enough: link validation
only needs the document to exist.
"""

import frappe
from frappe.utils import add_days, nowdate


class LinkedSalesOrderFixture:
	def __init__(self):
		suffix = frappe.generate_hash(length=8)
		company = frappe.defaults.get_global_default("company")
		price_list = frappe.get_all(
			"Price List", filters={"selling": 1, "enabled": 1}, pluck="name", limit=1
		)[0]
		warehouse = frappe.get_all(
			"Warehouse",
			filters={"disabled": 0, "is_group": 0, "company": company},
			pluck="name",
			limit=1,
		)[0]
		self.item = frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": f"TEST-LINKED-SO-{suffix}",
				"item_group": "Products",
				"stock_uom": "Nos",
				"is_stock_item": 0,
			}
		).insert()
		self.customer = frappe.get_doc(
			{"doctype": "Customer", "customer_name": f"Test Linked SO {suffix}", "customer_type": "Company"}
		).insert()
		self.sales_order = frappe.get_doc(
			{
				"doctype": "Sales Order",
				"company": company,
				"customer": self.customer.name,
				"selling_price_list": price_list,
				"transaction_date": nowdate(),
				"delivery_date": add_days(nowdate(), 1),
				"ignore_pricing_rule": 1,
				"items": [
					{
						"item_code": self.item.name,
						"qty": 1,
						"rate": 10,
						"uom": "Nos",
						"warehouse": warehouse,
					}
				],
			}
		).insert()
		self.name = self.sales_order.name

	def link(self, marketplace_order_name):
		"""Link one Marketplace Order to the fixture's Sales Order and clear debounce state."""
		frappe.db.set_value(
			"Marketplace Order",
			marketplace_order_name,
			{
				"erpnext_sales_order": self.name,
				"sync_debounce_until": None,
				"pending_sync_trigger_source": None,
			},
		)

	def cleanup(self):
		"""Unlink, then delete the Sales Order, Customer and Item (in dependency order).

		Safe to call from ``addCleanup()`` or from a ``tearDown`` that also commits (call it
		BEFORE that commit so the deletes are part of what gets committed).
		"""
		frappe.db.set_value(
			"Marketplace Order", {"erpnext_sales_order": self.name}, "erpnext_sales_order", None
		)
		frappe.delete_doc("Sales Order", self.name, force=True, ignore_permissions=True)
		frappe.delete_doc("Customer", self.customer.name, force=True, ignore_permissions=True)
		frappe.delete_doc("Item", self.item.name, force=True, ignore_permissions=True)
