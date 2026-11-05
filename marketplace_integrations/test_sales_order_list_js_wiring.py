# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Always-on guard for the bulk "Delivery Note" action (public/js/sales_order_bulk_delivery.js).

The action replaces what ERPNext's own "Delivery Note" Actions-menu item does by wrapping
``page.add_action_item`` while ERPNext's ``onload`` runs. If an ERPNext upgrade renames the item, moves it out
of ``onload`` or stops using ``page.add_action_item``, the replacement silently stops and ERPNext's own bulk
action returns. Verified against ERPNext 16.51.1 on dev; recheck on any ERPNext upgrade.
"""

import re

import frappe
from frappe.tests import UnitTestCase


def _read(app, *parts):
	with open(frappe.get_app_path(app, *parts)) as f:
		return f.read()


class UnitTestSalesOrderListJsWiring(UnitTestCase):
	def test_erpnext_still_adds_its_delivery_note_action_with_page_add_action_item(self):
		source = _read("erpnext", "selling", "doctype", "sales_order", "sales_order_list.js")
		self.assertTrue(
			re.search(r"onload\s*:\s*function\s*\(\s*listview\s*\)", source),
			"ERPNext's Sales Order list no longer defines onload(listview). Recheck sales_order_bulk_delivery.js.",
		)
		self.assertTrue(
			re.search(r"listview\.page\.add_action_item\(\s*__\(\s*[\"']Delivery Note[\"']\s*\)", source),
			'ERPNext no longer adds its bulk Delivery Note action with listview.page.add_action_item(__("Delivery Note"), ...). '
			"The app's replacement (sales_order_bulk_delivery.js) matches that label; recheck it.",
		)

	def test_this_app_loads_the_bulk_helper_on_every_desk_page(self):
		# The progress listener is registered once at startup, so the helper is an app include, not a list file.
		includes = frappe.get_hooks("app_include_js", app_name="marketplace_integrations")
		self.assertIn("/assets/marketplace_integrations/js/sales_order_bulk_delivery.js", includes)
		lists = (
			frappe.get_hooks("doctype_list_js", app_name="marketplace_integrations").get("Sales Order") or []
		)
		self.assertIn("public/js/sales_order_list.js", lists)
		self.assertNotIn("public/js/sales_order_bulk_delivery.js", lists)
