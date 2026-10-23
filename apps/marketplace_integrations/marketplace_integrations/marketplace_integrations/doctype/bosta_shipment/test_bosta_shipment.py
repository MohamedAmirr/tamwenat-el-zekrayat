# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

import frappe
from frappe.tests import IntegrationTestCase, UnitTestCase

from marketplace_integrations.marketplace_integrations.doctype.bosta_shipment.bosta_shipment import (
	save_as_system,
)

# The regression test inserts a shipment with ignore_links; skip auto-generated linked records.
IGNORE_TEST_RECORD_DEPENDENCIES = ["Sales Order", "Delivery Note"]


class UnitTestBostaShipment(UnitTestCase):
	def _document(self, **values):
		defaults = {
			"doctype": "Bosta Shipment",
			"sales_order": "SAL-ORD-00001",
			"receiver_name": "Ahmed",
			"receiver_phone": "01000000000",
			"city": "Cairo",
			"city_id": "city-cairo",
			"city_code": "EG-01",
			"address_first_line": "12 Example Street",
		}
		defaults.update(values)
		return frappe.get_doc(defaults)

	def test_accepts_a_city_without_any_district(self):
		self._document().run_method("validate")

	def test_city_is_required(self):
		with self.assertRaisesRegex(frappe.ValidationError, "City is required"):
			self._document(city="", city_id="").run_method("validate")

	def test_receiver_and_phone_are_required(self):
		with self.assertRaisesRegex(frappe.ValidationError, "Receiver Name is required"):
			self._document(receiver_name=" ").run_method("validate")
		with self.assertRaisesRegex(frappe.ValidationError, "Receiver Phone is required"):
			self._document(receiver_phone="").run_method("validate")

	def test_first_line_must_be_more_than_five_characters(self):
		with self.assertRaisesRegex(frappe.ValidationError, "more than 5 characters"):
			self._document(address_first_line="12345").run_method("validate")
		self._document(address_first_line="123456").run_method("validate")


class TestBostaShipmentTitle(IntegrationTestCase):
	def test_fulfillment_reservation_fields_replace_the_active_sales_order_constraint(self):
		meta = frappe.get_meta("Bosta Shipment")

		self.assertTrue(meta.get_field("company").reqd)
		self.assertTrue(meta.get_field("fulfillment_key").unique)
		self.assertTrue(meta.get_field("external_delivery_identity_key").unique)
		self.assertFalse(meta.get_field("active_sales_order").unique)
		self.assertEqual(meta.get_field("shipment_items").options, "Bosta Shipment Item")
		item_meta = frappe.get_meta("Bosta Shipment Item")
		self.assertTrue(item_meta.istable)
		for fieldname in ("so_detail", "item_code", "qty", "uom", "conversion_factor", "warehouse", "stock_qty", "fulfilled_qty"):
			self.assertTrue(item_meta.get_field(fieldname), fieldname)

	def test_title_is_the_tracking_number(self):
		"""#324: the subject is the tracking number; it is not a list column."""
		meta = frappe.get_meta("Bosta Shipment")

		self.assertEqual(meta.title_field, "tracking_number")
		self.assertFalse(meta.get_field("tracking_number").in_list_view)
		self.assertTrue(meta.get_field("status").in_list_view)
		self.assertIn("tracking_number", meta.get_search_fields())

	def test_title_field_has_no_default_or_mandatory_flag(self):
		"""Frappe skips static defaults for the title field on new docs (create_new.py),
		so a mandatory title field with a default breaks creation (#324)."""
		field = frappe.get_meta("Bosta Shipment").get_field("tracking_number")

		self.assertFalse(field.default)
		self.assertFalse(field.reqd)

	def test_new_shipment_keeps_the_pending_status_default(self):
		"""#324 regression: status is not the title field, so its default survives."""
		doc = frappe.new_doc("Bosta Shipment")

		self.assertEqual(doc.status, "Pending Bosta")

		doc.update(
			{
				"sales_order": f"SAL-ORD-{frappe.generate_hash(length=10)}",
				"company": frappe.defaults.get_global_default("company"),
				"receiver_name": "Ahmed",
				"receiver_phone": "01000000000",
				"city": "Cairo",
				"city_id": "city-cairo",
				"city_code": "EG-01",
				"address_first_line": "12 Example Street",
			}
		)
		doc.flags.ignore_links = True
		save_as_system(doc, insert=True)
		self.addCleanup(frappe.delete_doc, "Bosta Shipment", doc.name, force=True, ignore_permissions=True)

		self.assertEqual(doc.status, "Pending Bosta")
		# Document.get_title() has no docname fallback: it is "" for an empty tracking number.
		# The docname fallback happens in list and link display, not here.
		self.assertEqual(doc.get_title(), "")
