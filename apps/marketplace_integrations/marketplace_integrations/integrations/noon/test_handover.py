# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Noon "Mark Handed Over" tests (#368): the manual action drafts one Delivery Note through the
shared service, records it on the shipment, is idempotent per shipment, and makes no Noon call.
"""

from unittest.mock import MagicMock, patch

import frappe

from marketplace_integrations.api import noon_fulfillment
from marketplace_integrations.integrations.noon import fulfillment, handover_state
from marketplace_integrations.integrations.noon.client import NoonClient
from marketplace_integrations.integrations.noon.test_fulfillment import IntegrationTestNoonFulfillmentBase
from marketplace_integrations.services import sales_order_sync, sync_status
from marketplace_integrations.services import sync_reason_codes as reasons
from marketplace_integrations.services.handover_fixture import SubmittedOrderFixture
from marketplace_integrations.services.sales_order_sync_debounce import bump_sync_debounce
from marketplace_integrations.tests.utils import make_role_user


class IntegrationTestNoonMarkHandedOver(IntegrationTestNoonFulfillmentBase):
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
		"""A linked submitted Sales Order plus one shipment (status ``status``) holding ``unit_skus``."""
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

	def _shipment(self, order_name, nr):
		order = frappe.get_doc("Marketplace Order", order_name)
		return next(row for row in order.noon_shipments if row.integration_shipment_nr == nr)

	def test_creates_one_draft_for_the_shipment_units_and_records_it(self):
		fx, order, nr = self._shipped_order([("A", 3), ("B", 2)], ["A", "A"])
		with patch.object(NoonClient, "request") as noon_call:
			result = fulfillment.mark_handed_over(order, nr)
		noon_call.assert_not_called()  # the Noon spec has no handover endpoint

		self.assertTrue(result["created"])
		dn = frappe.get_doc("Delivery Note", result["delivery_note"])
		self.assertEqual(dn.docstatus, 0)  # never submitted
		self.assertEqual([(row.item_code, row.qty) for row in dn.items], [(fx.items["A"].name, 2)])
		shipment = self._shipment(order, nr)
		self.assertEqual(shipment.delivery_note, dn.name)
		self.assertTrue(shipment.handed_over_on)

	def test_second_call_creates_no_second_delivery_note(self):
		fx, order, nr = self._shipped_order([("A", 3)], ["A", "A"])
		first = fulfillment.mark_handed_over(order, nr)
		second = fulfillment.mark_handed_over(order, nr)
		self.assertTrue(first["created"])
		self.assertFalse(second["created"])
		self.assertEqual(second["delivery_note"], first["delivery_note"])
		self.assertEqual(fx.delivery_notes(), [first["delivery_note"]])

	def test_a_deleted_draft_can_be_replaced_without_over_delivering(self):
		_fx, order, nr = self._shipped_order([("A", 2)], ["A", "A"])
		first = fulfillment.mark_handed_over(order, nr)
		frappe.delete_doc("Delivery Note", first["delivery_note"], force=True)
		frappe.db.commit()
		again = fulfillment.mark_handed_over(order, nr)
		self.assertTrue(again["created"])
		self.assertEqual([row.qty for row in frappe.get_doc("Delivery Note", again["delivery_note"]).items], [2])

	def test_only_a_created_shipment_can_be_marked(self):
		_fx, order, nr = self._shipped_order([("A", 1)], ["A"], status="Pending")
		with self.assertRaises(frappe.ValidationError):
			fulfillment.mark_handed_over(order, nr)
		with self.assertRaises(frappe.ValidationError):
			fulfillment.mark_handed_over(order, "NO-SUCH-SHIPMENT")

	# ---- unresolved units: persistent record and Needs Action -------------------------------

	def _status(self, order):
		return frappe.db.get_value(
			"Marketplace Order",
			order,
			["erpnext_sync_status", "sync_reason_code", "last_sync_error"],
			as_dict=True,
		)

	def _record(self, order, nr):
		return handover_state.load_entries(self._shipment(order, nr))

	def _save(self, order_name, **flags):
		order = frappe.get_doc("Marketplace Order", order_name)
		for key, value in flags.items():
			order.flags[key] = value
		return order

	def test_unmapped_unit_is_recorded_on_the_shipment_and_the_order_needs_action(self):
		_fx, order, nr = self._shipped_order([("A", 2)], ["A", "NO-SUCH-SKU"])
		result = fulfillment.mark_handed_over(order, nr)
		self.assertTrue(result["created"])  # the mapped unit is still drafted
		self.assertEqual([e["reason"] for e in result["unresolved"]], ["unmapped"])
		entries = self._record(order, nr)
		self.assertEqual([(e["external_sku"], e["final"]) for e in entries], [("NO-SUCH-SKU", True)])
		self.assertTrue(self._shipment(order, nr).skipped_units_on)
		row = self._status(order)
		self.assertEqual(row.erpnext_sync_status, sync_status.NEEDS_ACTION)
		self.assertEqual(row.sync_reason_code, reasons.DELIVERY_NOTE_UNITS_SKIPPED)
		self.assertIn("NO-SUCH-SKU", row.last_sync_error)

	def test_a_later_save_sync_or_recheck_keeps_the_state(self):
		_fx, order, nr = self._shipped_order([("A", 2)], ["A", "NO-SUCH-SKU"])
		fulfillment.mark_handed_over(order, nr)

		def assert_kept(label):
			row = self._status(order)
			self.assertEqual(row.erpnext_sync_status, sync_status.NEEDS_ACTION, label)
			self.assertEqual(row.sync_reason_code, reasons.DELIVERY_NOTE_UNITS_SKIPPED, label)

		doc = self._save(order)
		doc.save()  # an ordinary save (import, form edit) with the re-plan enabled
		assert_kept("plain save")

		doc = self._save(order, skip_sync_status_update=True)
		reasons.apply_synced(doc)  # what a sync that found nothing to do writes
		doc.save()
		assert_kept("sync ends Synced")

		# A re-check bumps the order to Syncing; the sync that follows ends Synced.
		bump_sync_debounce(order, "ERPNext Document Change")
		doc = self._save(order, skip_sync_status_update=True)
		reasons.apply_synced(doc)
		doc.save()
		assert_kept("recheck then sync")

	def test_another_reason_is_not_overwritten_and_the_state_returns_when_it_clears(self):
		_fx, order, nr = self._shipped_order([("A", 2)], ["A", "NO-SUCH-SKU"])
		doc = self._save(order, skip_sync_status_update=True)
		reasons.apply_sync_state(doc, reasons.SYNC_BLOCKED, "boom")
		doc.save()
		fulfillment.mark_handed_over(order, nr)
		row = self._status(order)
		self.assertEqual(row.erpnext_sync_status, sync_status.SYNC_ERROR)  # most severe shown
		self.assertEqual(row.sync_reason_code, reasons.SYNC_BLOCKED)
		self.assertEqual(len(self._record(order, nr)), 1)  # but never lost

		doc = self._save(order, skip_sync_status_update=True)
		reasons.apply_synced(doc)  # the other reason clears
		doc.save()
		row = self._status(order)
		self.assertEqual(row.erpnext_sync_status, sync_status.NEEDS_ACTION)
		self.assertEqual(row.sync_reason_code, reasons.DELIVERY_NOTE_UNITS_SKIPPED)

	def test_a_needs_action_of_another_reason_is_not_overwritten_either(self):
		_fx, order, nr = self._shipped_order([("A", 2)], ["A", "NO-SUCH-SKU"])
		doc = self._save(order, skip_sync_status_update=True)
		reasons.apply_sync_state(doc, reasons.HEADER_CHANGED, "header")
		doc.save()
		fulfillment.mark_handed_over(order, nr)
		self.assertEqual(self._status(order).sync_reason_code, reasons.HEADER_CHANGED)

	def test_a_mapping_save_retries_the_handover_and_clears_the_record(self):
		fx, order, nr = self._shipped_order([("A", 2), ("B", 1)], ["A", "B"], unmapped=("B",))
		fulfillment.mark_handed_over(order, nr)
		self.assertEqual(self._status(order).sync_reason_code, reasons.DELIVERY_NOTE_UNITS_SKIPPED)
		self.assertEqual(len(fx.delivery_notes()), 1)

		def run_now(method, *args, **kwargs):
			for key in ("queue", "enqueue_after_commit", "job_id", "deduplicate", "timeout"):
				kwargs.pop(key, None)
			function = frappe.get_attr(method) if isinstance(method, str) else method
			return function(**kwargs)

		with patch.object(frappe, "enqueue", side_effect=run_now):
			mapping = frappe.get_doc(
				{
					"doctype": "Marketplace Product Mapping",
					"marketplace_account": self.account.name,
					"external_reference": "B",
					"external_sku": "B",
					"item": fx.items["B"].name,
					"enabled": 1,
				}
			).insert()
		fx.mappings.append(mapping)

		self.assertEqual(self._record(order, nr), [])
		self.assertEqual(len(fx.delivery_notes()), 2)  # the retry drafted the leftover unit
		row = self._status(order)
		# No record left: the reason is dropped and a normal re-sync recomputes the status.
		self.assertNotEqual(row.sync_reason_code, reasons.DELIVERY_NOTE_UNITS_SKIPPED)
		self.assertEqual(row.erpnext_sync_status, sync_status.SYNCING)

	def test_discarding_or_cancelling_the_shipment_clears_the_record(self):
		for how in ("discard", "cancel"):
			with self.subTest(how=how):
				sku = f"{how.upper()}-SKU"  # one mapping per account and SKU, so one SKU per fixture
				_fx, order, nr = self._shipped_order(
					[(sku, 2)], [sku, "NO-SUCH-SKU"], platform_order_id=f"ORD-HO-{how}-{frappe.generate_hash(length=6)}"
				)
				fulfillment.mark_handed_over(order, nr)
				self.assertEqual(len(self._record(order, nr)), 1)
				if how == "discard":
					frappe.db.set_value(
						"Marketplace Order Shipment", self._shipment(order, nr).name, "status", "Cancel Pending"
					)
					fulfillment.discard_shipment(order, nr)
				else:
					client = MagicMock()
					client.request.return_value = MagicMock(status_code=200)
					with patch.object(fulfillment, "_account_and_client", return_value=(self.account, client)):
						fulfillment.run_cancel_shipment(order, nr)
				self.assertEqual(self._record(order, nr), [])
				row = self._status(order)
				self.assertNotEqual(row.sync_reason_code, reasons.DELIVERY_NOTE_UNITS_SKIPPED)
				self.assertNotEqual(row.erpnext_sync_status, sync_status.NEEDS_ACTION)

	# ---- excess: the Sales Order is behind the platform order ---------------------------------

	def test_excess_units_queue_a_sync_and_are_drafted_once_the_sales_order_has_the_rows(self):
		# "Excess": the shipment holds more units of an Item than the Sales Order row still has
		# undelivered (row qty - delivered - drafts).
		fx, order, nr = self._shipped_order([("A", 1)], ["A", "A"])
		result = fulfillment.mark_handed_over(order, nr)
		self.assertEqual([(e["reason"], e["qty"]) for e in result["unresolved"]], [("excess", 1)])
		row = self._status(order)
		self.assertEqual(row.erpnext_sync_status, sync_status.SYNCING)  # sync queued, not yet Needs Action
		self.assertFalse(self._record(order, nr)[0]["final"])
		self.assertEqual(len(fx.delivery_notes()), 1)

		# The planner brings the Sales Order up to date (simulated), the sync ends, the retry runs.
		frappe.db.set_value("Sales Order Item", fx.row_for("A").name, "qty", 2)
		doc = self._save(order, skip_sync_status_update=True)
		reasons.apply_synced(doc)
		doc.save()
		fulfillment.retry_unresolved_handovers(order, after_sync=True)

		self.assertEqual(self._record(order, nr), [])
		self.assertEqual(len(fx.delivery_notes()), 2)
		quantities = [
			frappe.get_doc("Delivery Note", name).items[0].qty for name in fx.delivery_notes()
		]
		self.assertEqual(sorted(quantities), [1, 1])  # never more than the Sales Order has

	def test_excess_that_is_still_unmatched_after_the_sync_needs_action(self):
		fx, order, nr = self._shipped_order([("A", 1)], ["A", "A"])
		fulfillment.mark_handed_over(order, nr)
		doc = self._save(order, skip_sync_status_update=True)
		reasons.apply_synced(doc)  # the sync ended without adding the row
		doc.save()
		self.assertNotEqual(self._status(order).erpnext_sync_status, sync_status.NEEDS_ACTION)

		fulfillment.retry_unresolved_handovers(order, after_sync=True)

		self.assertTrue(self._record(order, nr)[0]["final"])
		row = self._status(order)
		self.assertEqual(row.erpnext_sync_status, sync_status.NEEDS_ACTION)
		self.assertEqual(row.sync_reason_code, reasons.DELIVERY_NOTE_UNITS_SKIPPED)
		self.assertEqual(len(fx.delivery_notes()), 1)  # delivered quantity never exceeds the Sales Order

	def test_a_sync_run_queues_the_retry_for_an_order_with_a_record(self):
		_fx, order, nr = self._shipped_order([("A", 1)], ["A", "A"])
		fulfillment.mark_handed_over(order, nr)
		with patch.object(frappe, "enqueue") as enqueue, patch.object(
			sales_order_sync, "_sync_sales_order", return_value={}
		):
			sales_order_sync.sync_sales_order(order)
		methods = [call.args[0] for call in enqueue.call_args_list]
		self.assertIn("marketplace_integrations.integrations.noon.fulfillment.retry_unresolved_handovers", methods)

	def test_deleting_a_linked_draft_delivery_note_clears_the_shipment_link(self):
		_fx, order, nr = self._shipped_order([("A", 2)], ["A"])
		result = fulfillment.mark_handed_over(order, nr)
		self.assertEqual(self._shipment(order, nr).delivery_note, result["delivery_note"])
		frappe.delete_doc("Delivery Note", result["delivery_note"])  # no force: the link check applies
		frappe.db.commit()
		shipment = self._shipment(order, nr)
		self.assertFalse(shipment.delivery_note)
		self.assertFalse(shipment.handed_over_on)
		self.assertIn(
			"mark_handed_over", fulfillment.get_available_actions(frappe.get_doc("Marketplace Order", order))["actions"]
		)

	def test_when_nothing_could_be_delivered_the_shipment_stays_unmarked(self):
		fx, order, nr = self._shipped_order([("A", 2)], ["NO-SUCH-SKU"])
		result = fulfillment.mark_handed_over(order, nr)
		self.assertFalse(result["created"])
		self.assertIsNone(result["delivery_note"])
		shipment = self._shipment(order, nr)
		self.assertFalse(shipment.delivery_note)
		self.assertFalse(shipment.handed_over_on)
		self.assertEqual(fx.delivery_notes(), [])
		self.assertEqual(self._status(order).sync_reason_code, reasons.DELIVERY_NOTE_UNITS_SKIPPED)
		# Still offered, so it can be retried once the mapping exists.
		self.assertIn(
			"mark_handed_over", fulfillment.get_available_actions(frappe.get_doc("Marketplace Order", order))["actions"]
		)

	def test_action_is_offered_until_a_live_delivery_note_exists(self):
		_fx, order, nr = self._shipped_order([("A", 1)], ["A"])
		actions = lambda: fulfillment.get_available_actions(frappe.get_doc("Marketplace Order", order))["actions"]  # noqa: E731
		self.assertIn("mark_handed_over", actions())
		result = fulfillment.mark_handed_over(order, nr)
		self.assertNotIn("mark_handed_over", actions())
		frappe.delete_doc("Delivery Note", result["delivery_note"], force=True)
		self.assertIn("mark_handed_over", actions())

	def test_endpoint_needs_write_on_the_order(self):
		_fx, order, nr = self._shipped_order([("A", 1)], ["A"])
		email, _role = make_role_user(self, "Read Only Noon", {"Marketplace Order": ["read"]})
		frappe.set_user(email)
		try:
			with self.assertRaises(frappe.PermissionError):
				noon_fulfillment.mark_handed_over(order, nr)
		finally:
			frappe.set_user("Administrator")

	def test_endpoint_needs_create_on_delivery_note(self):
		fx, order, nr = self._shipped_order([("A", 1)], ["A"])
		email, _role = make_role_user(self, "Noon Writer", {"Marketplace Order": ["write"]})
		# The platform role lets the order check pass, so the refusal below is the Delivery Note one.
		frappe.get_doc("User", email).add_roles("Noon User")
		frappe.set_user(email)
		try:
			with self.assertRaises(frappe.PermissionError):
				noon_fulfillment.mark_handed_over(order, nr)
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(fx.delivery_notes(), [])
