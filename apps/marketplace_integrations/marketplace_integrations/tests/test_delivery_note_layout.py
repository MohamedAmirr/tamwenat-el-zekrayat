# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Always-on guard for the Delivery Note Shipping section layout.

The native Carrier (transporter), AWB / Tracking No (lr_no) and Transport Receipt Date (lr_date)
live in the Shipping section together with the carrier-neutral shipping fields. The order comes
from the DocType-level ``field_order`` Property Setter plus the Custom Fields, so this reads the
resolved ``frappe.get_meta`` order: it needs the fixtures synced on the site (migrate).
"""

import frappe
from frappe.tests import IntegrationTestCase

SECTION = "custom_shipping_section"
EXPECTED = [
	SECTION,
	"transporter",
	"lr_no",
	"lr_date",
	"custom_carrier_delivery_id",
	"custom_carrier_reference",
	"custom_shipping_status_column",
	"custom_carrier_status",
	"custom_carrier_error",
	"custom_integration_status",
	"custom_shipping_status",
	"custom_shipping_column",
	"custom_cod_amount",
	"custom_cod_confirmed",
]


class TestDeliveryNoteLayout(IntegrationTestCase):
	def setUp(self):
		self.order = [df.fieldname for df in frappe.get_meta("Delivery Note").fields]

	def test_shipping_section_holds_native_carrier_fields_in_order(self):
		start = self.order.index(SECTION)
		self.assertEqual(self.order[start : start + len(EXPECTED)], EXPECTED)

	def test_shipping_section_is_always_visible(self):
		# Carrier lives inside it: a section hidden until a carrier is chosen could never be filled.
		df = frappe.get_meta("Delivery Note").get_field(SECTION)
		self.assertFalse(df.depends_on)

	def test_cod_and_city_show_only_for_a_managed_carrier(self):
		meta = frappe.get_meta("Delivery Note")
		for fieldname in ("custom_cod_amount", "custom_cod_confirmed"):
			self.assertIn("marketplace_shipping_carriers", meta.get_field(fieldname).depends_on)
		for fieldname in ("custom_bosta_city", "custom_bosta_city_name"):
			self.assertEqual(meta.get_field(fieldname).depends_on, 'eval:doc.transporter_name=="Bosta"')
		for fieldname in ("transporter", "lr_no", "lr_date"):
			self.assertFalse(meta.get_field(fieldname).depends_on)

	def test_native_fields_left_the_header_and_keep_their_meaning(self):
		meta = frappe.get_meta("Delivery Note")
		header = self.order[: self.order.index("column_break1")]
		for fieldname in ("transporter", "lr_no", "lr_date", "custom_carrier_status", "custom_carrier_error"):
			self.assertNotIn(fieldname, header)
		self.assertEqual(meta.get_field("transporter").fieldtype, "Link")
		self.assertEqual(meta.get_field("lr_no").fieldtype, "Data")
		self.assertEqual(meta.get_field("lr_date").fieldtype, "Date")

	def test_no_field_appears_twice(self):
		self.assertEqual(len(self.order), len(set(self.order)))
