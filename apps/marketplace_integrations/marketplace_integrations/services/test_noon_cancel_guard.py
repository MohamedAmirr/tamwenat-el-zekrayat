# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Tests for cancelling a Noon Sales Order: wait for Noon (#376)."""

from unittest.mock import MagicMock, patch

import frappe

from marketplace_integrations.integrations.noon import fulfillment
from marketplace_integrations.integrations.noon.test_fulfillment import IntegrationTestNoonFulfillmentBase
from marketplace_integrations.tests.handover_fixture import SubmittedOrderFixture

GUARD = "marketplace_integrations.services.noon_cancel_guard.block_cancel_while_shipments_are_open"


class IntegrationTestNoonCancelGuard(IntegrationTestNoonFulfillmentBase):
	def setUp(self):
		super().setUp()
		self.fx = SubmittedOrderFixture(self.account, [("A", 3)])
		self.order_name = self.fx.marketplace_order.name
		frappe.db.commit()

	def tearDown(self):
		frappe.flags.pop("marketplace_syncing_orders", None)
		self.fx.cleanup()
		frappe.db.commit()
		super().tearDown()

	def _shipment(self, number, status, **extra):
		order = frappe.get_doc("Marketplace Order", self.order_name)
		nr = f"{order.external_order_id}-{number}"
		order.append("noon_shipments", {"integration_shipment_nr": nr, "status": status, "awb_source": "Noon", **extra})
		order.save()
		frappe.db.commit()
		return nr

	def _cancel_sales_order(self):
		frappe.get_doc("Sales Order", self.fx.name).cancel()

	def _order(self):
		return frappe.get_doc("Marketplace Order", self.order_name)

	def test_hook_is_registered_on_the_sales_order(self):
		events = frappe.get_hooks("doc_events", app_name="marketplace_integrations")["Sales Order"]
		self.assertIn(GUARD, events["before_cancel"])

	def test_an_order_without_shipments_can_be_cancelled(self):
		self._cancel_sales_order()
		self.assertEqual(frappe.db.get_value("Sales Order", self.fx.name, "docstatus"), 2)

	def test_every_open_shipment_status_refuses_the_cancel_and_names_the_shipment(self):
		for status in ("Pending", "Created", "Cancel Pending", "Error"):
			with self.subTest(status=status):
				order = self._order()
				for row in list(order.noon_shipments):
					order.remove(row)
				order.save()
				nr = self._shipment(1, status)
				with self.assertRaises(frappe.ValidationError) as caught:
					self._cancel_sales_order()
				self.assertIn(nr, str(caught.exception))
				self.assertIn(status, str(caught.exception))
				self.assertEqual(frappe.db.get_value("Sales Order", self.fx.name, "docstatus"), 1)

	def test_cancelled_and_discarded_shipments_never_block(self):
		self._shipment(1, "Cancelled")
		self._shipment(2, "Discarded")
		self.assertEqual(fulfillment.sales_order_cancel_blockers(self._order()), [])
		self._cancel_sales_order()
		self.assertEqual(frappe.db.get_value("Sales Order", self.fx.name, "docstatus"), 2)

	def test_a_handed_over_shipment_tells_the_user_to_cancel_on_noon_then_the_delivery_note(self):
		nr = self._shipment(1, "Created", handed_over_on=frappe.utils.now_datetime())
		with self.assertRaises(frappe.ValidationError) as caught:
			self._cancel_sales_order()
		message = str(caught.exception)
		self.assertIn(nr, message)
		self.assertIn("handed over", message)
		self.assertIn("Delivery Note", message)
		self.assertEqual(frappe.db.get_value("Sales Order", self.fx.name, "docstatus"), 1)

	def test_a_live_delivery_note_counts_as_handed_over(self):
		self._shipment(1, "Created")
		with patch.object(fulfillment, "_live_delivery_notes", return_value=["MAT-DN-1"]):
			blockers = fulfillment.sales_order_cancel_blockers(self._order())
		self.assertEqual(len(blockers), 1)
		self.assertIn("handed over", blockers[0])

	def test_a_cancelled_handed_over_shipment_does_not_block(self):
		self._shipment(1, "Cancelled", handed_over_on=frappe.utils.now_datetime())
		self.assertEqual(fulfillment.sales_order_cancel_blockers(self._order()), [])

	def test_the_sync_marker_lets_the_planners_cancel_through(self):
		self._shipment(1, "Created")
		frappe.flags.marketplace_syncing_orders = {self.order_name}
		self._cancel_sales_order()
		self.assertEqual(frappe.db.get_value("Sales Order", self.fx.name, "docstatus"), 2)

	def test_the_marker_is_per_order(self):
		self._shipment(1, "Created")
		frappe.flags.marketplace_syncing_orders = {"SOME-OTHER-ORDER"}
		with self.assertRaises(frappe.ValidationError):
			self._cancel_sales_order()

	# ---- cancel_created_shipments / preview -------------------------------------------------

	def test_cancel_created_shipments_queues_each_created_shipment_only(self):
		created = self._shipment(1, "Created")
		self._shipment(2, "Error")
		client = MagicMock()
		with patch.object(fulfillment, "_account_and_client", return_value=(self.account, client)), patch.object(
			frappe, "enqueue"
		) as enqueue:
			result = fulfillment.cancel_created_shipments(self.order_name)
		self.assertEqual(result, {"queued": [created], "skipped": [], "failed": []})
		enqueue.assert_called_once()
		self.assertEqual(
			next(row.status for row in self._order().noon_shipments if row.integration_shipment_nr == created),
			"Cancel Pending",
		)
		client.request.assert_not_called()  # no Noon call in the request

	def test_cancel_created_shipments_skips_a_live_delivery_note_and_reports_a_failure(self):
		first = self._shipment(1, "Created")
		second = self._shipment(2, "Created")
		third = self._shipment(3, "Created")

		def live(order, shipment):
			return ["MAT-DN-1"] if shipment.integration_shipment_nr == first else []

		def cancel(order_name, nr):
			if nr == second:
				frappe.throw("not cancellable")
			return {"queued": True}

		with patch.object(fulfillment, "_live_delivery_notes", side_effect=live), patch.object(
			fulfillment, "cancel_shipment", side_effect=cancel
		):
			result = fulfillment.cancel_created_shipments(self.order_name)
		self.assertEqual(result["skipped"], [first])
		self.assertEqual(result["queued"], [third])
		self.assertEqual(result["failed"][0]["integration_shipment_nr"], second)
		self.assertIn("not cancellable", result["failed"][0]["error"])

	def test_cancel_created_shipments_does_not_mark_units_out_of_stock(self):
		self._shipment(1, "Created")
		with patch.object(fulfillment, "_account_and_client", return_value=(self.account, MagicMock())), patch.object(
			frappe, "enqueue"
		), patch.object(fulfillment, "mark_out_of_stock") as oos:
			fulfillment.cancel_created_shipments(self.order_name)
		oos.assert_not_called()

	def test_preview_lists_created_skipped_and_blockers(self):
		first = self._shipment(1, "Created")
		second = self._shipment(2, "Created")
		order = self._order()
		with patch.object(
			fulfillment,
			"_live_delivery_notes",
			side_effect=lambda o, s: ["MAT-DN-1"] if s.integration_shipment_nr == second else [],
		):
			preview = fulfillment.get_cancel_on_noon_preview(order)
		self.assertEqual([entry["integration_shipment_nr"] for entry in preview["created"]], [first])
		self.assertEqual([entry["integration_shipment_nr"] for entry in preview["skipped"]], [second])
		self.assertEqual(len(preview["blockers"]), 2)

	def test_cancel_on_noon_is_an_available_action_while_a_created_shipment_can_be_cancelled(self):
		self._shipment(1, "Created")
		self.assertIn("cancel_on_noon", fulfillment.get_available_actions(self._order())["actions"])
		with patch.object(fulfillment, "_live_delivery_notes", return_value=["MAT-DN-1"]):
			self.assertNotIn("cancel_on_noon", fulfillment.get_available_actions(self._order())["actions"])
