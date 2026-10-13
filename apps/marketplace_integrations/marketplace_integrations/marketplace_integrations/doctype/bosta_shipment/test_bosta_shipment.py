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
			"district_id": "district-id",
			"address_first_line": "12 Example Street",
		}
		defaults.update(values)
		return frappe.get_doc(defaults)

	def test_accepts_district_id_address_variant(self):
		self._document().run_method("validate")

	def test_accepts_city_id_and_district_name_address_variant(self):
		self._document(district_id="", city_id="city-id", district_name="Maadi").run_method("validate")

	def test_rejects_missing_or_ambiguous_address_variant(self):
		with self.assertRaises(frappe.ValidationError):
			self._document(district_id="", city_id="", district_name="").run_method("validate")
		with self.assertRaises(frappe.ValidationError):
			self._document(city_id="city-id", district_name="Maadi").run_method("validate")
