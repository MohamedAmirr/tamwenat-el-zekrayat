# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.patches.v0_0.backfill_bosta_shipment_fee_received_on import execute
from marketplace_integrations.tests.bosta_fixtures import BostaFixtureMixin, created


class IntegrationTestBackfillBostaShipmentFeeReceivedOn(BostaFixtureMixin, IntegrationTestCase):
	def _shipment(self, fee):
		order = self.make_order()
		tracking = f"TRK-{frappe.generate_hash(length=6)}"
		self.http.queue["create"].append(created(tracking, f"D-{tracking}"))
		self.create(order)
		shipment = self.shipment_for(order)
		frappe.db.set_value("Bosta Shipment", shipment.name, {"shipment_fee": fee, "shipment_fee_received_on": None})
		return shipment.name

	def test_only_a_stored_fee_above_zero_is_marked_received_and_a_rerun_changes_nothing(self):
		paid, zero = self._shipment(87), self._shipment(0)

		execute()
		stamped = frappe.db.get_value("Bosta Shipment", paid, "shipment_fee_received_on")
		execute()

		self.assertTrue(stamped)
		self.assertEqual(frappe.db.get_value("Bosta Shipment", paid, "shipment_fee_received_on"), stamped)
		self.assertFalse(frappe.db.get_value("Bosta Shipment", zero, "shipment_fee_received_on"))
