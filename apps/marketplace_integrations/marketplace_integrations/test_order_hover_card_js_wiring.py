# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Always-on guard for #325's public/js/order_hover_card.js and sales_order_list.js.

The hover card is built on framework internals that no hook protects. If a Frappe/ERPNext
upgrade changes them, the card silently stops appearing. Dev was verified at Frappe 16.51.0
(recheck on any Frappe upgrade and when prod's version changes).
"""

import json
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
		for doctype, script in (
			("Sales Order", "sales_order_list.js"),
			("Delivery Note", "delivery_note_list.js"),
		):
			registered = hooks[doctype]
			self.assertIn("public/js/carrier_status.js", registered, doctype)
			self.assertLess(
				registered.index("public/js/carrier_status.js"),
				registered.index(f"public/js/{script}"),
				doctype,
			)

	def test_platform_badge_helper_is_registered_for_all_platform_lists(self):
		hooks = frappe.get_hooks("doctype_list_js")
		# The core Sales Order list loads the helper before its list script. Marketplace Order and
		# Marketplace Account own their list scripts, which Frappe loads before hook files, so they
		# look the helper up per row instead.
		sales_order = hooks["Sales Order"]
		self.assertLess(
			sales_order.index("public/js/platform_badge.js"),
			sales_order.index("public/js/sales_order_list.js"),
		)
		for doctype in ("Marketplace Order", "Marketplace Account"):
			self.assertIn("public/js/platform_badge.js", hooks[doctype], doctype)
		for doctype, script, formatter in (
			("Marketplace Order", "marketplace_order/marketplace_order_list.js", "format_order_platform"),
			("Marketplace Account", "marketplace_account/marketplace_account_list.js", "format_account_platform"),
		):
			source = _read("marketplace_integrations", "marketplace_integrations/doctype", script)
			self.assertIn("platform_badge", source, doctype)
			# the platform column's formatter is registered in settings.formatters and calls the helper
			self.assertRegex(source, rf"formatters:\s*\{{[^}}]*platform:\s*{formatter}\b", doctype)
			self.assertRegex(
				source,
				rf"function {formatter}\([^)]*\)\s*\{{[^}}]*platform_badge[^}}]*\.formatter\(",
				doctype,
			)

	def test_delivery_note_list_loads_badge_helper_and_carrier_column(self):
		registered = frappe.get_hooks("doctype_list_js")["Delivery Note"]
		for helper in ("public/js/carrier_status.js", "public/js/platform_badge.js"):
			self.assertLess(
				registered.index(helper), registered.index("public/js/delivery_note_list.js"), helper
			)
		source = _read("marketplace_integrations", "public/js/delivery_note_list.js")
		self.assertRegex(source, r"\[CARRIER_FIELD\]:\s*format_carrier")
		self.assertIn('CARRIER_FIELD = "transporter"', source)
		self.assertIn("carrier_formatter(", source)
		# transporter is a standard field: it is a list column only through this Property Setter
		name = "Delivery Note-transporter-in_list_view"
		rows = [r for r in json.loads(_read("marketplace_integrations", "fixtures/property_setter.json")) if r["name"] == name]
		self.assertEqual(len(rows), 1)
		self.assertEqual(
			(rows[0]["doc_type"], rows[0]["field_name"], rows[0]["property"], rows[0]["value"]),
			("Delivery Note", "transporter", "in_list_view", "1"),
		)
		self.assertIn(f'"{name}"', _read("marketplace_integrations", "hooks.py"))
