# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.patches.v0_0 import backfill_sales_order_bosta_delivery_status as patch_module
from marketplace_integrations.tests.bosta_fixtures import BostaFixtureMixin, created

FIELD = "custom_bosta_delivery_status"


class IntegrationTestBackfillSalesOrderBostaDeliveryStatus(BostaFixtureMixin, IntegrationTestCase):
	def _shipment(self):
		order = self.make_order()
		self.http.queue["create"].append(created())
		self.create(order)
		return order, self.shipment_for(order)

	def _value(self, order):
		return frappe.db.get_value("Sales Order", order.name, FIELD)

	def test_backfill_is_safe_to_run_twice_and_does_not_touch_modified(self):
		order, _shipment = self._shipment()
		frappe.db.set_value("Sales Order", order.name, FIELD, "", update_modified=False)
		modified = frappe.db.get_value("Sales Order", order.name, "modified")

		patch_module.execute()
		first = self._value(order)
		patch_module.execute()

		self.assertTrue(first)
		self.assertEqual(self._value(order), first)
		self.assertEqual(frappe.db.get_value("Sales Order", order.name, "modified"), modified)

	def test_a_canceled_shipment_is_backfilled_as_canceled_not_its_last_state_name(self):
		order, shipment = self._shipment()
		frappe.db.set_value(
			"Bosta Shipment", shipment.name, {"status": "Canceled", "bosta_state_code": 24}, update_modified=False
		)
		frappe.db.set_value("Sales Order", order.name, FIELD, "", update_modified=False)

		patch_module.execute()

		self.assertEqual(self._value(order), "Canceled")

	def test_a_missing_custom_field_is_created_from_the_fixture_before_backfilling(self):
		# Patches run before fixtures sync, so on a first migrate the field does not exist yet.
		real_exists = frappe.db.exists

		def exists(doctype, name=None, *args, **kwargs):
			if doctype == "Custom Field" and name == patch_module.FIELD_NAME:
				return None
			return real_exists(doctype, name, *args, **kwargs)

		with (
			patch.object(frappe.db, "exists", side_effect=exists),
			patch.object(patch_module, "create_custom_fields") as create,
		):
			patch_module.execute()

		((fields,), _kwargs) = create.call_args
		(field,) = fields["Sales Order"]
		self.assertEqual(field["fieldname"], FIELD)
		self.assertEqual(field["fieldtype"], "Data")
		self.assertEqual(field["insert_after"], "custom_marketplace_order")
		self.assertNotIn("name", field)
