# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.patches.v0_0.remove_marketplace_order_in_list_view_property_setters import (
	execute,
)

TARGET_FILTERS = {"doc_type": "Marketplace Order", "property": "in_list_view"}


class IntegrationTestRemoveMarketplaceOrderInListViewPropertySetters(IntegrationTestCase):
	def setUp(self):
		# The patch deletes every matching setter, so never run it on a real site.
		if frappe.local.site != "test.localhost":
			self.skipTest("destructive patch test: test.localhost only")
		# Park pre-existing matching setters; they are restored in cleanup.
		self.preexisting = [
			frappe.get_doc("Property Setter", name).as_dict()
			for name in frappe.get_all("Property Setter", filters=TARGET_FILTERS, pluck="name")
		]
		frappe.db.delete("Property Setter", TARGET_FILTERS)
		self.created = []
		self.addCleanup(self._cleanup)

	def _cleanup(self):
		for name in self.created:
			frappe.db.delete("Property Setter", {"name": name})
		for values in self.preexisting:
			if not frappe.db.exists("Property Setter", values.name):
				frappe.get_doc({**values, "doctype": "Property Setter"}).insert(
					ignore_permissions=True, set_name=values.name
				)
		frappe.clear_cache(doctype="Marketplace Order")

	def make_setter(self, doc_type, field_name, prop):
		setter = frappe.get_doc(
			{
				"doctype": "Property Setter",
				"doc_type": doc_type,
				"doctype_or_field": "DocField",
				"field_name": field_name,
				"property": prop,
				"property_type": "Check",
				"value": "1",
			}
		).insert()
		self.created.append(setter.name)
		return setter.name

	def test_deletes_only_marketplace_order_in_list_view_setters(self):
		target = self.make_setter("Marketplace Order", "order_code", "in_list_view")
		other_property = self.make_setter("Marketplace Order", "order_code", "in_standard_filter")
		other_doctype = self.make_setter("Marketplace Account", "account_name", "in_list_view")

		execute()

		self.assertFalse(frappe.db.exists("Property Setter", target))
		self.assertTrue(frappe.db.exists("Property Setter", other_property))
		self.assertTrue(frappe.db.exists("Property Setter", other_doctype))

	def test_second_run_is_a_no_op(self):
		other_property = self.make_setter("Marketplace Order", "order_code", "in_standard_filter")
		self.make_setter("Marketplace Order", "order_code", "in_list_view")

		execute()
		self.assertEqual(frappe.db.count("Property Setter", TARGET_FILTERS), 0)
		execute()

		self.assertEqual(frappe.db.count("Property Setter", TARGET_FILTERS), 0)
		self.assertTrue(frappe.db.exists("Property Setter", other_property))
