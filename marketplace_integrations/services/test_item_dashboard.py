# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""#374: the Item dashboard override adds a Marketplace group and keeps ERPNext's own connections."""

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.services.item_dashboard import get_data

MAPPING = "Marketplace Product Mapping"


class IntegrationTestItemDashboardOverride(IntegrationTestCase):
	def base_data(self):
		return frappe._dict(
			fieldname="item_code",
			non_standard_fieldnames={"Work Order": "production_item", "BOM": "item"},
			transactions=[{"label": "Sell", "items": ["Sales Order"]}],
		)

	def test_adds_the_group_and_the_fieldname_without_replacing_the_original(self):
		data = get_data(self.base_data())

		self.assertEqual(data["non_standard_fieldnames"][MAPPING], "item")
		self.assertEqual(data["non_standard_fieldnames"]["Work Order"], "production_item")
		self.assertEqual(data["non_standard_fieldnames"]["BOM"], "item")
		self.assertEqual(data["fieldname"], "item_code")
		self.assertEqual(data["transactions"][0], {"label": "Sell", "items": ["Sales Order"]})
		self.assertEqual(data["transactions"][-1], {"label": "Marketplace", "items": [MAPPING]})

	def test_points_the_form_at_the_row_filtered_count_method(self):
		data = get_data(self.base_data())

		self.assertEqual(data["method"], "marketplace_integrations.services.item_dashboard.get_open_count")
		method = frappe.get_attr(data["method"])
		self.assertIn(method, frappe.whitelisted)
		self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[method], ["GET"])

	def test_works_when_the_base_data_has_neither_key(self):
		data = get_data(frappe._dict())

		self.assertEqual(data["non_standard_fieldnames"], {MAPPING: "item"})
		self.assertEqual(data["transactions"], [{"label": "Marketplace", "items": [MAPPING]}])
		self.assertIn("get_open_count", data["method"])


class IntegrationTestItemDashboardHook(IntegrationTestCase):
	def test_the_hook_is_registered_and_erpnexts_own_connections_survive(self):
		hooks = frappe.get_hooks("override_doctype_dashboards", {}).get("Item", [])
		self.assertIn("marketplace_integrations.services.item_dashboard.get_data", hooks)

		data = frappe.get_meta("Item").get_dashboard_data()

		# The keys ERPNext's own item_dashboard.get_data() defines must all survive.
		for doctype, fieldname in {
			"Work Order": "production_item",
			"Product Bundle": "new_item_code",
			"BOM": "item",
			"Batch": "item",
		}.items():
			self.assertEqual(data.non_standard_fieldnames[doctype], fieldname)
		self.assertEqual(data.non_standard_fieldnames[MAPPING], "item")
		labels = [group["label"] for group in data.transactions]
		self.assertIn("Marketplace", labels)
		self.assertIn("Sell", labels)
		self.assertEqual(labels.count("Marketplace"), 1)
