# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.patches.v0_0.backfill_bosta_fulfillment_reservations import execute
from marketplace_integrations.tests.bosta_fixtures import BostaFixtureMixin, created


class IntegrationTestBackfillBostaFulfillmentReservations(BostaFixtureMixin, IntegrationTestCase):
	def test_backfills_linked_delivery_note_allocations_without_changing_stock_documents(self):
		order = self.make_order(qty=2)
		self.http.queue["create"].append(created())
		result = self.create(order)
		shipment = frappe.get_doc("Bosta Shipment", result["shipment"])
		delivery_note = shipment.delivery_note
		fulfillment_key = shipment.fulfillment_key

		frappe.db.delete("Bosta Shipment Item", {"parent": shipment.name})
		frappe.db.set_value(
			"Bosta Shipment",
			shipment.name,
			{
				"fulfillment_key": "",
				"external_delivery_identity_key": "",
				"reservation_status": "",
			},
			update_modified=False,
		)
		frappe.clear_document_cache("Bosta Shipment", shipment.name)

		execute()
		shipment = frappe.get_doc("Bosta Shipment", shipment.name)
		self.assertTrue(shipment.fulfillment_key)
		self.assertNotEqual(shipment.fulfillment_key, fulfillment_key)
		self.assertTrue(shipment.external_delivery_identity_key)
		self.assertEqual(shipment.reservation_status, "Fulfilled")
		self.assertEqual(len(shipment.shipment_items), 1)
		self.assertEqual(shipment.shipment_items[0].delivery_note_item, frappe.db.get_value("Delivery Note Item", {"parent": delivery_note}, "name"))
		self.assertEqual(frappe.db.get_value("Delivery Note", delivery_note, "docstatus"), 1)

		backfilled_key = shipment.fulfillment_key
		execute()
		self.assertEqual(frappe.db.get_value("Bosta Shipment", shipment.name, "fulfillment_key"), backfilled_key)
		self.assertEqual(frappe.db.count("Bosta Shipment Item", {"parent": shipment.name}), 1)
		indexes = frappe.db.sql(
			"SHOW INDEX FROM `tabBosta Shipment` WHERE Column_name = %s",
			("active_sales_order",),
			as_dict=True,
		)
		self.assertFalse(any(index.Non_unique == 0 for index in indexes))

	def test_delivery_ids_differing_only_by_whitespace_are_quarantined(self):
		first = self._legacy_shipment(self.make_order())
		second = self._legacy_shipment(self.make_order())
		frappe.db.set_value("Bosta Shipment", first.name, "bosta_delivery_id", "\tDUPLICATE-OLD-ID")
		frappe.db.set_value("Bosta Shipment", second.name, "bosta_delivery_id", "DUPLICATE-OLD-ID\n")
		execute()
		for shipment in (first, second):
			shipment.reload()
			self.assertFalse(shipment.external_delivery_identity_key)
		execute()
		for shipment in (first, second):
			self.assertFalse(frappe.db.get_value("Bosta Shipment", shipment.name, "external_delivery_identity_key"))

	def _legacy_shipment(self, order):
		from marketplace_integrations.marketplace_integrations.doctype.bosta_shipment.bosta_shipment import (
			save_as_system,
		)

		return save_as_system(
			frappe.get_doc(
				{
					"doctype": "Bosta Shipment",
					"sales_order": order.name,
					"receiver_name": self._receiver(),
					"receiver_phone": "01000000000",
					"city": "Cairo",
					"city_id": "city-cairo",
					"city_code": "EG-01",
					"address_first_line": "12 Example Street",
				}
			),
			insert=True,
		)
