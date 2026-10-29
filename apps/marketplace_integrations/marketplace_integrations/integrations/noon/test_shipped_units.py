# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Noon SHIPPED units (#390): one Delivery Note per shipment, drafted as units become SHIPPED and
submitted when every active unit is SHIPPED.  A real submitted Sales Order with non-stock Items
(``tests/handover_fixture.py``); no Noon call is made.
"""

from unittest.mock import patch

import frappe

from marketplace_integrations.integrations.noon import fulfillment, handover_state
from marketplace_integrations.integrations.noon.test_fulfillment import IntegrationTestNoonFulfillmentBase
from marketplace_integrations.services.marketplace_attention import NOON_FULFILLMENT, get_attention_items
from marketplace_integrations.tests.handover_fixture import SubmittedOrderFixture

SHIPPED = "INTEGRATION_ITEM_STATUS_SHIPPED"
ACKNOWLEDGED = "INTEGRATION_ITEM_STATUS_ACKNOWLEDGED"
OUT_OF_STOCK = "INTEGRATION_ITEM_STATUS_OUT_OF_STOCK"
CONFIRMED = "MP_ITEM_STATUS_CONFIRMED"
CANCELLED = "MP_ITEM_STATUS_CANCELLED"


class IntegrationTestNoonShippedBase(IntegrationTestNoonFulfillmentBase):
	def setUp(self):
		super().setUp()
		self.fixtures = []

	def tearDown(self):
		frappe.set_user("Administrator")
		for fixture in self.fixtures:
			fixture.cleanup()
		frappe.db.commit()
		super().tearDown()

	def _shipped_order(self, rows, unit_skus, status="Created", **kwargs):
		"""A linked submitted Sales Order plus one shipment holding ``unit_skus`` (units 1..n)."""
		fixture = SubmittedOrderFixture(self.account, rows, **kwargs)
		self.fixtures.append(fixture)
		order = frappe.get_doc("Marketplace Order", fixture.marketplace_order.name)
		nr = f"{order.external_order_id}-1"
		order.append("noon_shipments", {"integration_shipment_nr": nr, "status": status, "awb_source": "Noon"})
		for index, sku in enumerate(unit_skus, start=1):
			order.append(
				"noon_shipment_units",
				{"integration_shipment_nr": nr, "mp_item_nr": str(index), "partner_sku": sku},
			)
		order.save()
		frappe.db.commit()
		return fixture, order.name, nr

	def _set_statuses(self, order_name, statuses, sku="A"):
		"""``statuses`` is ``{mp_item_nr: integration_status}`` or ``{nr: (integration_status, mp_status)}``;
		written as the order's ``latest_payload`` items."""
		items = []
		for nr, value in statuses.items():
			integration, mp_status = value if isinstance(value, tuple) else (value, CONFIRMED)
			items.append(
				{
					"mp_item_nr": nr,
					"partner_sku": sku,
					"mp_status": mp_status,
					"integration_status": integration,
				}
			)
		order = frappe.get_doc("Marketplace Order", order_name)
		order.latest_payload = frappe.as_json({"fbpi_order_nr": order.external_order_id, "items": items})
		order.flags.skip_sync_status_update = True
		order.save()
		frappe.db.commit()

	def _unit_notes(self, order_name):
		doc = frappe.get_doc("Marketplace Order", order_name)
		return [row.delivery_note for row in doc.noon_shipment_units]

	def _shipment(self, order_name, nr):
		order = frappe.get_doc("Marketplace Order", order_name)
		return next(row for row in order.noon_shipments if row.integration_shipment_nr == nr)

	def _note(self, name):
		return frappe.get_doc("Delivery Note", name)


class IntegrationTestNoonShippedUnits(IntegrationTestNoonShippedBase):
	def test_first_shipped_unit_drafts_the_note_later_ones_join_it_and_the_last_submits_it(self):
		fx, order, nr = self._shipped_order([("A", 3)], ["A", "A", "A"])

		self._set_statuses(order, {"1": SHIPPED, "2": ACKNOWLEDGED, "3": ACKNOWLEDGED})
		with patch.object(fulfillment.NoonClient, "request") as noon_call:
			fulfillment.process_shipped_units(order)
		noon_call.assert_not_called()
		note = fx.delivery_notes()[0]
		self.assertEqual(self._note(note).docstatus, 0)
		self.assertEqual([row.qty for row in self._note(note).items], [1])
		self.assertEqual(self._unit_notes(order), [note, None, None])

		self._set_statuses(order, {"1": SHIPPED, "2": SHIPPED, "3": ACKNOWLEDGED})
		fulfillment.process_shipped_units(order)
		self.assertEqual(fx.delivery_notes(), [note])  # the same draft
		self.assertEqual((self._note(note).docstatus, [r.qty for r in self._note(note).items]), (0, [2]))

		self._set_statuses(order, {"1": SHIPPED, "2": SHIPPED, "3": SHIPPED})
		fulfillment.process_shipped_units(order)
		self.assertEqual(fx.delivery_notes(), [note])
		self.assertEqual((self._note(note).docstatus, [r.qty for r in self._note(note).items]), (1, [3]))
		self.assertEqual(self._unit_notes(order), [note] * 3)

		fulfillment.process_shipped_units(order)  # an import that changes nothing
		self.assertEqual(fx.delivery_notes(), [note])
		self.assertEqual(self._note(note).docstatus, 1)

	def test_a_cancelled_or_out_of_stock_unit_is_left_out_of_the_submit_condition(self):
		for left_out in ((ACKNOWLEDGED, CANCELLED), (OUT_OF_STOCK, CONFIRMED)):
			with self.subTest(left_out=left_out):
				sku = f"S{left_out[0][-4:]}"
				fx, order, nr = self._shipped_order(
					[(sku, 3)], [sku, sku, sku], platform_order_id=f"ORD-SH-{frappe.generate_hash(length=6)}"
				)
				self._set_statuses(order, {"1": SHIPPED, "2": SHIPPED, "3": left_out}, sku=sku)
				fulfillment.process_shipped_units(order)
				note = fx.delivery_notes()[0]
				self.assertEqual((self._note(note).docstatus, [r.qty for r in self._note(note).items]), (1, [2]))
				self.assertEqual(self._unit_notes(order), [note, note, None])

	def test_a_deleted_draft_uncovers_its_units_and_the_next_import_drafts_them_again(self):
		fx, order, nr = self._shipped_order([("A", 2)], ["A", "A"])
		self._set_statuses(order, {"1": SHIPPED, "2": ACKNOWLEDGED})
		fulfillment.process_shipped_units(order)
		first = fx.delivery_notes()[0]
		frappe.delete_doc("Delivery Note", first)
		frappe.db.commit()
		self.assertEqual(self._unit_notes(order), [None, None])

		fulfillment.process_shipped_units(order)

		second = fx.delivery_notes()
		self.assertEqual(len(second), 1)
		self.assertNotEqual(second[0], first)
		self.assertEqual(self._unit_notes(order), [second[0], None])

	def test_a_draft_from_an_earlier_mark_handed_over_is_the_shipments_draft(self):
		fx, order, nr = self._shipped_order([("A", 2)], ["A", "A"])
		with patch.object(fulfillment, "submit_note", side_effect=RuntimeError("no stock")):
			draft = fulfillment.mark_handed_over(order, nr)["delivery_note"]
		self._set_statuses(order, {"1": SHIPPED, "2": SHIPPED})

		fulfillment.process_shipped_units(order)

		self.assertEqual(fx.delivery_notes(), [draft])  # no second note
		self.assertEqual(self._note(draft).docstatus, 1)  # all units SHIPPED: submitted

	def test_a_shipped_unit_after_the_submit_gets_no_new_note_and_a_finding(self):
		fx, order, nr = self._shipped_order([("A", 2)], ["A", "A"])
		self._set_statuses(order, {"1": SHIPPED, "2": SHIPPED})
		fulfillment.process_shipped_units(order)
		note = fx.delivery_notes()[0]
		doc = frappe.get_doc("Marketplace Order", order)
		doc.noon_shipment_units[1].delivery_note = None  # an uncovered SHIPPED unit
		doc.save()
		frappe.db.commit()

		fulfillment.process_shipped_units(order)

		self.assertEqual(fx.delivery_notes(), [note])
		rows = get_attention_items({"account": self.account.name, "problem_type": NOON_FULFILLMENT})
		self.assertEqual([row["reference_name"] for row in rows], [order])
		self.assertIn("A / 2", rows[0]["explanation"])
		self.assertIn(note, rows[0]["explanation"])

	def test_a_shipped_unit_outside_a_created_shipment_gets_no_note_and_a_finding_that_clears(self):
		fx, order, nr = self._shipped_order([("A", 1)], ["A"], status="Pending")
		self._set_statuses(order, {"1": SHIPPED})
		fulfillment.process_shipped_units(order)
		self.assertEqual(fx.delivery_notes(), [])
		rows = get_attention_items({"account": self.account.name, "problem_type": NOON_FULFILLMENT})
		self.assertIn("Shipped on Noon but not in a Created shipment: A / 1", rows[0]["explanation"])

		# No longer SHIPPED: the finding is gone.
		self._set_statuses(order, {"1": ACKNOWLEDGED})
		self.assertEqual(get_attention_items({"account": self.account.name, "problem_type": NOON_FULFILLMENT}), [])

	def test_a_submit_failure_keeps_the_draft_sets_the_error_and_the_next_run_submits(self):
		fx, order, nr = self._shipped_order([("A", 1)], ["A"])
		self._set_statuses(order, {"1": SHIPPED})
		with patch.object(fulfillment, "submit_note", side_effect=RuntimeError("not enough stock")):
			fulfillment.process_shipped_units(order)
		note = fx.delivery_notes()[0]
		self.assertEqual(self._note(note).docstatus, 0)
		self.assertIn("not enough stock", self._shipment(order, nr).delivery_note_error)
		rows = get_attention_items({"account": self.account.name, "problem_type": NOON_FULFILLMENT})
		self.assertIn("Delivery Note(s) could not be submitted", rows[0]["explanation"])

		fulfillment.process_shipped_units(order)

		self.assertEqual(self._note(note).docstatus, 1)
		self.assertFalse(self._shipment(order, nr).delivery_note_error)

	def test_nothing_happens_until_the_sales_order_is_submitted(self):
		fx, order, nr = self._shipped_order([("A", 1)], ["A"], submit=False)
		self._set_statuses(order, {"1": SHIPPED})
		fulfillment.process_shipped_units(order)
		self.assertEqual(fx.delivery_notes(), [])
		self.assertEqual(self._unit_notes(order), [None])

	def test_an_unmapped_shipped_unit_is_recorded_not_dropped(self):
		fx, order, nr = self._shipped_order([("A", 1), ("B", 1)], ["A", "B"], unmapped=("B",))
		self._set_statuses(order, {"1": SHIPPED, "2": SHIPPED})
		fulfillment.process_shipped_units(order)
		entries = handover_state.load_entries(self._shipment(order, nr))
		self.assertEqual([(e["external_sku"], e["reason"]) for e in entries], [("B", "unmapped")])
		self.assertEqual(self._unit_notes(order)[1], None)


class IntegrationTestNoonShippedTrigger(IntegrationTestNoonShippedBase):
	def _rows(self, order_name):
		doc = frappe.get_doc("Marketplace Order", order_name)
		return handover_state.payload_items(doc.latest_payload), doc.noon_shipments, doc.noon_shipment_units

	def test_pending_work_is_a_shipped_uncovered_unit_or_a_fully_shipped_draft(self):
		fx, order, nr = self._shipped_order([("A", 2)], ["A", "A"])
		self._set_statuses(order, {"1": ACKNOWLEDGED, "2": ACKNOWLEDGED})
		self.assertFalse(handover_state.shipped_work_pending(*self._rows(order)))

		self._set_statuses(order, {"1": SHIPPED, "2": ACKNOWLEDGED})
		self.assertTrue(handover_state.shipped_work_pending(*self._rows(order)))  # uncovered

		fulfillment.process_shipped_units(order)
		self.assertFalse(handover_state.shipped_work_pending(*self._rows(order)))  # covered, not all shipped

		self._set_statuses(order, {"1": SHIPPED, "2": SHIPPED})
		self.assertTrue(handover_state.shipped_work_pending(*self._rows(order)))
		fulfillment.process_shipped_units(order)
		self.assertFalse(handover_state.shipped_work_pending(*self._rows(order)))  # submitted

	def test_a_shipment_that_is_not_created_is_never_pending_work(self):
		_fx, order, _nr = self._shipped_order([("A", 1)], ["A"], status="Pending")
		self._set_statuses(order, {"1": SHIPPED})
		self.assertFalse(handover_state.shipped_work_pending(*self._rows(order)))

	def test_the_job_is_queued_once_per_order_after_commit(self):
		_fx, order, _nr = self._shipped_order([("A", 1)], ["A"])
		self._set_statuses(order, {"1": SHIPPED})
		doc = frappe.get_doc("Marketplace Order", order)
		with patch.object(frappe, "enqueue") as enqueue:
			fulfillment.queue_shipped_units_if_needed(doc)
		enqueue.assert_called_once()
		kwargs = enqueue.call_args.kwargs
		self.assertEqual(enqueue.call_args.args[0], fulfillment.process_shipped_units)
		self.assertEqual(kwargs["job_id"], f"{fulfillment.RQ_JOB_PREFIX}-{order}-shipped")
		self.assertTrue(kwargs["deduplicate"])
		self.assertTrue(kwargs["enqueue_after_commit"])

		self._set_statuses(order, {"1": ACKNOWLEDGED})
		with patch.object(frappe, "enqueue") as enqueue:
			fulfillment.queue_shipped_units_if_needed(frappe.get_doc("Marketplace Order", order))
		enqueue.assert_not_called()
