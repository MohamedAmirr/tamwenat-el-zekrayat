# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Manual couriers (inDrive, Uber, Own driver): Suppliers with no adapter. A Delivery Note shipped with one
is a plain ERPNext Delivery Note: the carrier flow leaves it alone."""

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.patches.v0_0 import create_manual_courier_suppliers
from marketplace_integrations.services import carrier_shipments
from marketplace_integrations.services import marketplace_attention as attention
from marketplace_integrations.tests.bosta_fixtures import BostaFixtureMixin

COURIERS = create_manual_courier_suppliers.SUPPLIER_NAMES


class IntegrationTestManualCouriers(BostaFixtureMixin, IntegrationTestCase):
	def setUp(self):
		super().setUp()
		missing = [n for n in COURIERS if not frappe.db.get_value("Supplier", {"supplier_name": n}, "name")]
		create_manual_courier_suppliers.execute()
		# Only the Suppliers this test created are removed again by the fixture tearDown.
		for name in missing:
			self._track(frappe.get_doc("Supplier", {"supplier_name": name}))
		self.couriers = {n: frappe.db.get_value("Supplier", {"supplier_name": n}, "name") for n in COURIERS}
		frappe.db.commit()

	def _note(self, courier, **overrides):
		return self.make_note(transporter=self.couriers[courier], **overrides)

	def _items(self, note):
		return [
			item
			for item in attention._collect_carrier_items()
			if item["reference_name"] == note.name
		]

	def test_no_adapter_claims_a_manual_courier_note(self):
		for courier, supplier in self.couriers.items():
			for is_return, source in ((0, None), (1, None), (1, "Customer Return Pickup")):
				doc = {"is_return": is_return, "transporter": supplier, "custom_return_source": source}
				self.assertIsNone(carrier_shipments.adapter_for(doc), f"{courier} {doc}")
				for name, adapter in carrier_shipments.CARRIERS.items():
					self.assertFalse(adapter.applies_to(doc), f"{name} claims {courier} {doc}")

	def test_a_saved_note_is_left_alone_and_submit_and_cancel_call_nothing(self):
		for courier in COURIERS:
			note = self._note(courier)
			self.assertIsNone(carrier_shipments.adapter_for(frappe.get_doc("Delivery Note", note.name)))
			self.assertFalse(frappe.db.get_value("Delivery Note", note.name, "custom_integration_status"))

			note = frappe.get_doc("Delivery Note", note.name)
			note.submit()
			self.assertEqual(frappe.db.get_value("Delivery Note", note.name, "docstatus"), 1)
			note.cancel()
			self.assertEqual(frappe.db.get_value("Delivery Note", note.name, "docstatus"), 2)

			status = frappe.db.get_value("Delivery Note", note.name, "custom_integration_status")
			self.assertIn(status or "", ("", "Not Created"))
			self.assertEqual(self.http.sent(), [])
			self.enqueue.assert_not_called()
			self.assertEqual(
				frappe.db.count("Integration Request", {"reference_doctype": "Delivery Note", "reference_docname": note.name}),
				0,
			)

	def test_a_manual_courier_note_never_shows_in_needs_attention(self):
		note = self._note("inDrive")
		self.assertEqual(self._items(note), [])
		# Even with the fields a carrier failure writes, nobody owns the note, so no carrier problem is raised.
		frappe.db.set_value(
			"Delivery Note",
			note.name,
			{"custom_integration_status": "Creation Failed", "custom_last_integration_error": "boom"},
			update_modified=False,
		)
		self.assertEqual(self._items(note), [])

	def test_the_api_refuses_a_manual_courier_note(self):
		note = self._note("Uber")
		with self.assertRaises(frappe.ValidationError):
			carrier_shipments.request_create(note.name)
		self.enqueue.assert_not_called()
