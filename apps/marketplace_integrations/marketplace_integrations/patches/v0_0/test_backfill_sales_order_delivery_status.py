# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.integrations.noon import orders
from marketplace_integrations.integrations.noon.orders import delivery_status_text
from marketplace_integrations.integrations.noon.test_shipped_units import IntegrationTestNoonShippedBase
from marketplace_integrations.patches.v0_0 import backfill_sales_order_delivery_status as patch_module

FIELD = "custom_delivery_status"


class IntegrationTestDeliveryStatusText(IntegrationTestCase):
	def test_counts_shipped_units_over_the_sales_order_eligible_ones(self):
		row = lambda status, integration, qty=1: frappe._dict(  # noqa: E731
			external_status=status, integration_status=integration, current_quantity=qty
		)
		rows = [
			row("CONFIRMED", "SHIPPED", 2),
			row("CONFIRMED", "ACKNOWLEDGED", 1),
			row("CONFIRMED", "OUT_OF_STOCK", 1),  # not eligible
			row("CANCELLED", "SHIPPED", 1),  # not eligible
			row("CONFIRMED", "SHIPPED", 0),  # zeroed stale group
		]
		self.assertEqual(delivery_status_text(rows), "Shipped 2/3")
		self.assertEqual(delivery_status_text([]), "")


class IntegrationTestBackfillSalesOrderDeliveryStatus(IntegrationTestNoonShippedBase):
	def _status(self, fixture):
		return frappe.db.get_value("Sales Order", fixture.name, FIELD)

	def test_backfills_a_noon_order_once_and_does_not_touch_modified(self):
		fx, _order, _nr = self._shipped_order([("A", 2)], ["A", "A"])
		frappe.db.set_value("Sales Order", fx.name, FIELD, "", update_modified=False)
		modified = frappe.db.get_value("Sales Order", fx.name, "modified")

		with patch.object(orders, "delivery_status_text", return_value="Shipped 1/2"):
			patch_module.execute()
			self.assertEqual(self._status(fx), "Shipped 1/2")
			patch_module.execute()  # nothing left to change

		self.assertEqual(self._status(fx), "Shipped 1/2")
		self.assertEqual(frappe.db.get_value("Sales Order", fx.name, "modified"), modified)

	def test_the_import_writes_the_status_on_the_linked_sales_order(self):
		fx, order, _nr = self._shipped_order([("A", 2)], ["A", "A"])
		with patch.object(orders, "delivery_status_text", return_value="Shipped 2/2"):
			orders.write_delivery_status(order, fx.name)
		self.assertEqual(self._status(fx), "Shipped 2/2")
		orders.write_delivery_status(order, None)  # no Sales Order yet: nothing happens

	def test_a_missing_custom_field_is_created_from_the_fixture_first(self):
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
		self.assertEqual((field["fieldname"], field["fieldtype"]), (FIELD, "Data"))
		self.assertEqual(field["in_list_view"], 1)
		self.assertNotIn("name", field)
