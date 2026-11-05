# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Platform naming series (``services/platform_naming.py``): Sales Order, Delivery Note and Sales Invoice of a
marketplace order are named per platform. Real database; never contacts Bosta, Noon or Shopify."""

import re
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.services import bulk_delivery_notes as bulk
from marketplace_integrations.services import platform_naming
from marketplace_integrations.tests.bosta_fixtures import BostaFixtureMixin

PLATFORMS = {
	"Shopify": ("SO-SHP", "DN-SHP", "SI-SHP"),
	"Noon": ("SO-NOON", "DN-NOON", "SI-NOON"),
	"Talabat": ("SO-TLB", "DN-TLB", "SI-TLB"),
}


class IntegrationTestPlatformNamingMapping(IntegrationTestCase):
	def test_every_platform_has_a_series_for_each_document(self):
		for platform, (order, note, invoice) in PLATFORMS.items():
			self.assertEqual(platform_naming.series_for(platform, "Sales Order"), f"{order}-.YYYY.-")
			self.assertEqual(platform_naming.series_for(platform, "Delivery Note"), f"{note}-.YYYY.-")
			self.assertEqual(platform_naming.series_for(platform, "Sales Invoice"), f"{invoice}-.YYYY.-")

	def test_an_unknown_platform_or_document_has_no_series(self):
		self.assertIsNone(platform_naming.series_for(None, "Sales Order"))
		self.assertIsNone(platform_naming.series_for("", "Sales Order"))
		self.assertIsNone(platform_naming.series_for("InstaShop", "Sales Order"))
		self.assertIsNone(platform_naming.series_for("Noon", "Payment Entry"))

	def test_every_series_is_an_option_of_the_naming_series_field(self):
		for doctype in ("Sales Order", "Delivery Note", "Sales Invoice"):
			options = frappe.get_meta(doctype).get_naming_series_options()
			for platform in PLATFORMS:
				self.assertIn(platform_naming.series_for(platform, doctype), options)

	def test_the_default_series_stays_first(self):
		first = {
			"Sales Order": "SAL-ORD-.YYYY.-",
			"Delivery Note": "MAT-DN-.YYYY.-",
			"Sales Invoice": "ACC-SINV-.YYYY.-",
		}
		for doctype, series in first.items():
			self.assertEqual(frappe.get_meta(doctype).get_naming_series_options()[0], series)

	def test_the_hook_sets_the_series_of_a_new_sales_order(self):
		for platform, (order, _note, _invoice) in PLATFORMS.items():
			doc = frappe.new_doc("Sales Order")
			doc.custom_marketplace_platform = platform
			platform_naming.set_naming_series(doc)
			self.assertEqual(doc.naming_series, f"{order}-.YYYY.-")

	def test_a_sales_order_without_a_platform_is_left_alone(self):
		doc = frappe.new_doc("Sales Order")
		doc.naming_series = "SAL-ORD-.YYYY.-"
		platform_naming.set_naming_series(doc)
		self.assertEqual(doc.naming_series, "SAL-ORD-.YYYY.-")

	def test_a_series_chosen_on_a_non_marketplace_order_is_respected(self):
		doc = frappe.new_doc("Sales Order")
		doc.naming_series = "SO-NOON-.YYYY."  # any explicit choice
		platform_naming.set_naming_series(doc)
		self.assertEqual(doc.naming_series, "SO-NOON-.YYYY.")

	def test_the_platform_comes_from_the_marketplace_order_when_the_field_is_empty(self):
		doc = frappe.new_doc("Sales Order")
		doc.custom_marketplace_order = "MO-1"
		with patch.object(frappe.db, "get_value", return_value="Shopify") as get_value:
			platform_naming.set_naming_series(doc)
		get_value.assert_called_once_with("Marketplace Order", "MO-1", "platform")
		self.assertEqual(doc.naming_series, "SO-SHP-.YYYY.-")

	def test_a_note_or_invoice_uses_the_marketplace_order_when_its_sales_order_has_no_platform(self):
		def get_value(doctype, name, fieldname):
			if doctype == "Sales Order":
				return (None, "MO-1")
			self.assertEqual((doctype, name, fieldname), ("Marketplace Order", "MO-1", "platform"))
			return "Noon"

		for doctype, link_field, series in (
			("Delivery Note", "against_sales_order", "DN-NOON-.YYYY.-"),
			("Sales Invoice", "sales_order", "SI-NOON-.YYYY.-"),
		):
			doc = frappe.new_doc(doctype)
			doc.append("items", {link_field: "SO-X"})
			with patch.object(frappe.db, "get_value", side_effect=get_value):
				platform_naming.set_naming_series(doc)
			self.assertEqual(doc.naming_series, series)

	def test_a_failure_keeps_the_default_series_and_never_raises(self):
		doc = frappe.new_doc("Sales Order")
		doc.naming_series = "SAL-ORD-.YYYY.-"
		doc.custom_marketplace_platform = "Noon"
		with (
			patch.object(platform_naming, "series_for", side_effect=RuntimeError("boom")),
			patch.object(frappe, "log_error") as log_error,
		):
			platform_naming.set_naming_series(doc)
		self.assertEqual(doc.naming_series, "SAL-ORD-.YYYY.-")
		log_error.assert_called_once()

	def test_a_return_document_keeps_its_series(self):
		doc = frappe.new_doc("Delivery Note")
		doc.naming_series = "MAT-DN-RET-.YYYY.-"
		doc.is_return = 1
		doc.append("items", {"against_sales_order": "SO-X"})
		with patch.object(frappe.db, "get_value", return_value="Noon"):
			platform_naming.set_naming_series(doc)
		self.assertEqual(doc.naming_series, "MAT-DN-RET-.YYYY.-")


class IntegrationTestPlatformNamingDocuments(BostaFixtureMixin, IntegrationTestCase):
	def _marketplace_order(self, platform):
		order = self.make_order()
		frappe.db.set_value("Sales Order", order.name, "custom_marketplace_platform", platform)
		frappe.db.commit()
		return frappe.get_doc("Sales Order", order.name)

	def _note(self, order):
		from erpnext.selling.doctype.sales_order.sales_order import make_delivery_note

		note = make_delivery_note(order.name)
		note.insert()
		return note

	def _invoice(self, order):
		from erpnext.selling.doctype.sales_order.sales_order import make_sales_invoice

		invoice = make_sales_invoice(order.name)
		invoice.insert()
		self._track(invoice)
		return invoice

	def test_a_delivery_note_and_an_invoice_made_from_a_platform_order_get_its_series(self):
		for platform, (_order, note_prefix, invoice_prefix) in PLATFORMS.items():
			order = self._marketplace_order(platform)
			note = self._note(order)
			invoice = self._invoice(order)
			self.assertRegex(note.name, rf"^{note_prefix}-\d{{4}}-\d{{5}}$")
			self.assertRegex(invoice.name, rf"^{invoice_prefix}-\d{{4}}-\d{{5}}$")

	def test_documents_of_an_order_without_a_platform_keep_the_default_series(self):
		order = self.make_order()
		note = self._note(order)
		invoice = self._invoice(order)
		self.assertTrue(order.name.startswith("SAL-ORD-"))
		self.assertTrue(note.name.startswith("MAT-DN-"))
		self.assertTrue(invoice.name.startswith("ACC-SINV-"))

	def test_a_new_sales_order_with_a_platform_is_named_by_it(self):
		order = frappe.get_doc(
			{
				"doctype": "Sales Order",
				"customer": self.customer.name,
				"company": self.company,
				"delivery_date": frappe.utils.add_days(frappe.utils.nowdate(), 3),
				"custom_marketplace_platform": "Talabat",
				"items": [{"item_code": self.item.name, "qty": 1, "rate": 10}],
			}
		).insert()
		self._track(order)
		self.assertRegex(order.name, r"^SO-TLB-\d{4}-\d{5}$")

	def test_the_bulk_path_names_the_note_by_platform(self):
		"""The paid invoice of the bulk path is made by the same ERPNext mapper as ``_invoice`` above (its
		naming is covered there); this one proves the bulk note."""
		# Noon fulfils its own orders, so the Bosta bulk path only accepts a Shopify order here.
		order = self._marketplace_order("Shopify")
		frappe.db.set_value(
			"Sales Order",
			order.name,
			{"shipping_address_name": self.address.name, "contact_mobile": "01000000000"},
		)
		frappe.db.commit()
		result = bulk.process_order(order.name)
		for name in bulk._invoices(order.name, 1) + bulk._invoices(order.name, 0):
			self._track(frappe.get_doc("Sales Invoice", name))
		self.assertTrue(re.match(r"^DN-SHP-\d{4}-\d{5}$", result["delivery_note"] or ""), result)
