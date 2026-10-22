# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Always-on guard for #325's public/js/order_hover_card.js and sales_order_list.js.

The hover card is built on framework internals that no hook protects. If a Frappe/ERPNext
upgrade changes them, the card silently stops appearing. Dev was verified at Frappe 16.51.0
(recheck on any Frappe upgrade and when prod's version changes).
"""

import re

import frappe
from frappe.tests import UnitTestCase


def _read(app, *parts):
	with open(frappe.get_app_path(app, *parts)) as f:
		return f.read()


class UnitTestOrderHoverCardJsWiring(UnitTestCase):
	def test_frappe_still_defines_ui_hover_card(self):
		source = _read("frappe", "public/js/frappe/ui/components/hover_card.js")
		self.assertTrue(
			re.search(r"frappe\.ui\.hover_card\s*=\s*function", source),
			"hover_card.js no longer defines frappe.ui.hover_card. Recheck order_hover_card.js "
			"(attach) against the new hover card API, or its fallback.",
		)
		# order_hover_card.js uses the `content(instance)` option, instance.close() and
		# instance.onreposition().
		for needle in ("onreposition", "close()"):
			self.assertIn(
				needle,
				source,
				f"hover_card.js no longer mentions `{needle}`. order_hover_card.js calls "
				"instance.onreposition() and instance.close(); recheck them.",
			)

	def test_list_view_still_builds_rows_the_card_scans(self):
		source = _read("frappe", "public/js/frappe/list/list_view.js")
		self.assertIn(
			'class="list-row-container"',
			source,
			"list_view.js no longer renders `.list-row-container`. order_hover_card.js "
			"(attach_to_list) selects rows with it; recheck the row markup.",
		)
		self.assertIn(
			"level-right",
			source,
			"list_view.js no longer renders `.level-right`. The Details button is appended there.",
		)
		self.assertIn(
			".list-row-checkbox[data-name=",
			source,
			"list_view.js no longer pairs `.list-row-checkbox` with `data-name` inside "
			"`.list-row-container`. order_hover_card.js (row_docname) reads the document name "
			"from it; recheck.",
		)
		self.assertIn('.closest(".list-row-container")', source)

	def test_erpnext_sales_order_list_still_assigns_listview_settings(self):
		source = _read("erpnext", "selling/doctype/sales_order/sales_order_list.js")
		self.assertTrue(
			re.search(r"frappe\.listview_settings\[\s*[\"']Sales Order[\"']\s*\]\s*=", source),
			"ERPNext's sales_order_list.js no longer assigns frappe.listview_settings"
			'["Sales Order"]. public/js/sales_order_list.js extends that object (onload); '
			"recheck how ERPNext registers list settings.",
		)

	def test_sales_order_list_js_is_registered_in_doctype_list_js(self):
		registered = frappe.get_hooks("doctype_list_js")["Sales Order"]
		self.assertIn("public/js/sales_order_list.js", registered)
