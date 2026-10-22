# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

import frappe
from frappe.tests import UnitTestCase


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
