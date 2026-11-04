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
		# order_hover_card.js relies on this contract of the hover card:
		contract = {
			"content(instance) is called with the card": r"content\s*=\s*content\(this\)",
			"instance.onreposition() re-places the open panel": r"this\.onreposition\s*=\s*\(\)\s*=>",
			"instance.close() closes it": r"\n\s*close\(\)\s*\{",
			"instance.destroy() removes the listeners": r"\n\s*destroy\(\)\s*\{",
			"the instance is kept on the trigger's jQuery data": r"\.data\(\s*[\"']es-hover-card[\"']\s*,\s*card\s*\)",
			"side and align options": r"validated\(opts\.side[\s\S]*validated\(opts\.align",
			"focus on the trigger opens it": r"addEventListener\(\s*[\"']focus[\"']\s*,\s*this\.onfocus",
		}
		for what, pattern in contract.items():
			self.assertTrue(
				re.search(pattern, source),
				f"hover_card.js no longer provides: {what}. order_hover_card.js depends on it; recheck.",
			)

	def test_desk_bundle_still_loads_hover_card(self):
		source = _read("frappe", "public/js/desk.bundle.js")
		self.assertIn(
			'import "./frappe/ui/components/hover_card.js";',
			source,
			"desk.bundle.js no longer imports hover_card.js, so frappe.ui.hover_card is not "
			"defined on Desk and the card silently disappears (only the Details button remains).",
		)

	def test_list_view_still_builds_rows_the_card_scans(self):
		source = _read("frappe", "public/js/frappe/list/list_view.js")
		self.assertIn(
			'class="list-row-container"',
			source,
			"list_view.js no longer renders `.list-row-container`. order_hover_card.js "
			"(attach_to_list) selects rows with it; recheck the row markup.",
		)
		self.assertTrue(
			re.search(r'<div class="level-right[^"]*">\s*\$\{right\}', source),
			"list_view.js no longer renders `.level-right` with the row's right-hand HTML. The "
			"Details button is appended to it; recheck the row skeleton.",
		)
		self.assertIn(
			'tabindex="1"',
			source,
			"list_view.js rows no longer have tabindex=1. The keyboard notes in order_hover_card.js "
			"(rows are focused by the arrow keys, the subject link opens the card) need a recheck.",
		)
		self.assertIn(
			'".list-subject a"',
			source,
			"list_view.js no longer finds the subject link as `.list-subject a`. The card is "
			"anchored to `.list-subject a[data-name]` (row_link); recheck the subject markup.",
		)
		self.assertTrue(
			re.search(r"link\.dataset\.name\s*=\s*name", source),
			"list_view.js no longer sets data-name on the subject link. row_docname() reads it.",
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

	def test_carrier_status_helper_loads_before_both_list_scripts(self):
		hooks = frappe.get_hooks("doctype_list_js")
		for doctype, script in (("Sales Order", "sales_order_list.js"), ("Delivery Note", "delivery_note_list.js")):
			registered = hooks[doctype]
			self.assertEqual(registered[0], "public/js/carrier_status.js", doctype)
			self.assertIn(f"public/js/{script}", registered)
