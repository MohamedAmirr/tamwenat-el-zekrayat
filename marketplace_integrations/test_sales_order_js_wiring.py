# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Always-on guard for #183's public/js/sales_order.js.

The script wraps ERPNext's `close_sales_order` (no hook exists for Close) and relies
on the Close buttons calling `this.close_sales_order()`. If an ERPNext upgrade
renames or removes either, the Close prompt silently stops working.
"""

import re

import frappe
from frappe.tests import UnitTestCase


class UnitTestSalesOrderJsWiring(UnitTestCase):
	def test_erpnext_sales_order_still_defines_and_calls_close_sales_order(self):
		path = frappe.get_app_path("erpnext", "selling/doctype/sales_order/sales_order.js")
		with open(path) as f:
			source = f.read()
		self.assertTrue(re.search(r"^\s*close_sales_order\(\)\s*\{", source, re.M))
		self.assertIn("this.close_sales_order()", source)

	def test_sales_order_js_is_registered_in_doctype_js(self):
		registered = frappe.get_hooks("doctype_js")["Sales Order"]
		self.assertIn("public/js/sales_order.js", registered)
