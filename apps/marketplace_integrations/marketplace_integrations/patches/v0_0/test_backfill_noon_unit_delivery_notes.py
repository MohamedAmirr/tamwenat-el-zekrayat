# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

import frappe

from marketplace_integrations.integrations.noon import fulfillment
from marketplace_integrations.integrations.noon.test_shipped_units import (
	SHIPPED,
	IntegrationTestNoonShippedBase,
)
from marketplace_integrations.patches.v0_0.backfill_noon_unit_delivery_notes import execute
from marketplace_integrations.services.delivery_note_drafts import create_draft_delivery_note


class IntegrationTestBackfillNoonUnitDeliveryNotes(IntegrationTestNoonShippedBase):
	def _legacy_shipment(self, rows, unit_skus, **kwargs):
		"""A shipment whose draft note was made before #390: recorded on the shipment row, not on the units."""
		fx, order, nr = self._shipped_order(rows, unit_skus, **kwargs)
		doc = frappe.get_doc("Marketplace Order", order)
		note = create_draft_delivery_note(
			doc, [{"external_sku": sku, "qty": 1} for sku in unit_skus if sku in fx.items]
		)["delivery_note"]
		doc.save()
		shipment = doc.noon_shipments[0]
		frappe.db.set_value(
			shipment.doctype,
			shipment.name,
			{"delivery_note": note, "delivery_notes": frappe.as_json([note])},
			update_modified=False,
		)
		frappe.db.commit()
		return fx, order, nr, note

	def test_fills_the_unit_rows_once_and_a_second_run_changes_nothing(self):
		fx, order, nr, note = self._legacy_shipment([("A", 2)], ["A", "A"])
		self.assertEqual(self._unit_notes(order), [None, None])

		execute()
		self.assertEqual(self._unit_notes(order), [note, note])

		before = frappe.db.get_value("Marketplace Order", order, "modified")
		execute()
		self.assertEqual(self._unit_notes(order), [note, note])
		self.assertEqual(frappe.db.get_value("Marketplace Order", order, "modified"), before)

	def test_units_listed_as_unresolved_are_left_out(self):
		fx, order, nr, note = self._legacy_shipment([("A", 1)], ["A", "B"], unmapped=())
		shipment = frappe.get_doc("Marketplace Order", order).noon_shipments[0]
		frappe.db.set_value(
			shipment.doctype,
			shipment.name,
			"skipped_units",
			frappe.as_json([{"external_sku": "B", "item": "", "qty": 1, "reason": "unmapped", "final": True}]),
			update_modified=False,
		)

		execute()

		self.assertEqual(self._unit_notes(order), [note, None])

	def test_a_dead_note_fills_nothing_and_a_site_without_handover_data_is_a_no_op(self):
		fx, order, nr, note = self._legacy_shipment([("A", 1)], ["A"])
		frappe.db.set_value("Delivery Note", note, "docstatus", 2)  # cancelled, no amendment
		execute()
		self.assertEqual(self._unit_notes(order), [None])
		frappe.db.set_value("Delivery Note", note, "docstatus", 0)
		execute()  # fills now
		self.assertEqual(self._unit_notes(order), [note])

	def test_a_backfilled_shipment_gets_no_duplicate_note_on_the_next_shipped_import(self):
		fx, order, nr, note = self._legacy_shipment([("A", 2)], ["A", "A"])
		execute()
		self._set_statuses(order, {"1": SHIPPED, "2": SHIPPED})

		fulfillment.process_shipped_units(order)

		self.assertEqual(fx.delivery_notes(), [note])
		self.assertEqual(frappe.db.get_value("Delivery Note", note, "docstatus"), 1)
