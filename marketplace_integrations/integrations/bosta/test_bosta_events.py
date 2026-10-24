# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Real-database tests of webhook/poll state application, cancellation and Needs Attention."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from erpnext.stock.doctype.delivery_note.delivery_note import make_sales_invoice
from frappe.tests import IntegrationTestCase, UnitTestCase
from frappe.utils import add_days, now_datetime

from marketplace_integrations.api import bosta_shipments, bosta_webhooks
from marketplace_integrations.integrations.bosta import deliveries, statuses
from marketplace_integrations.marketplace_integrations.doctype.bosta_shipment.bosta_shipment import (
	save_as_system,
)
from marketplace_integrations.services import marketplace_attention, marketplace_channels
from marketplace_integrations.tests.bosta_fixtures import (
	BostaFixtureMixin,
	FakeResponse,
	created,
)

SECRET = "bosta-webhook-secret"


class UnitTestBostaDeliveryFee(UnitTestCase):
	def test_fee_requires_a_nonnegative_non_boolean_number(self):
		for value in (0, 87, 87.5):
			self.assertEqual(statuses._shipment_fee({"data": {"shipmentFees": value}}), value)
		for value in (True, -1, float("nan"), float("inf"), "87", None, {}):
			self.assertIsNone(statuses._shipment_fee({"data": {"shipmentFees": value}}))


class IntegrationTestBostaEvents(BostaFixtureMixin, IntegrationTestCase):
	def test_send_state_21_posts_stock_once_and_duplicate_webhook_is_ignored(self):
		order = self.make_order()
		self.http.queue["create"].append(created())
		self.create(order, confirm_handover=False)
		shipment = self.shipment_for(order)
		self.assertEqual(shipment.stock_posting_status, "Not Posted")
		self.assertFalse(frappe.db.exists("Delivery Note Item", {"against_sales_order": order.name}))

		payload = self._event(shipment, 21, 5000)
		self.assertEqual(statuses.process_webhook_event(payload), statuses.APPLIED)
		self.assertEqual(statuses.process_webhook_event(payload), statuses.STALE)

		shipment.reload()
		self.assertEqual((shipment.handover_status, shipment.stock_posting_status), ("Confirmed", "Posted"))
		self.assertEqual(frappe.db.count("Delivery Note", {"name": shipment.delivery_note}), 1)

	def test_non_send_state_21_does_not_prove_handover_during_check(self):
		order = self.make_order()
		self.http.queue["create"].append(created())
		self.create(order, confirm_handover=False)
		shipment = self.shipment_for(order)
		self.assertEqual(
			statuses.process_webhook_event(self._event(shipment, 21, 5000, type="EXCHANGE")), statuses.APPLIED
		)

		result = deliveries.check_bosta_shipment(shipment.name)

		shipment.reload()
		self.assertTrue(result["found"])
		self.assertEqual(shipment.bosta_delivery_type, "EXCHANGE")
		self.assertEqual(shipment.handover_status, "Awaiting Handover")
		self.assertEqual(shipment.stock_posting_status, "Not Posted")
		self.assertFalse(shipment.delivery_note)

	def _completed_shipment(self, tracking="TRK-1", **order_args):
		order = self.make_order(**order_args)
		self.http.queue["create"].append(created(tracking, f"D-{tracking}"))
		self.create(order)
		return order, self.shipment_for(order)

	def _event(self, shipment, state, timestamp, **updates):
		payload = {
			"_id": shipment.bosta_delivery_id,
			"trackingNumber": shipment.tracking_number,
			"state": state,
			"type": "SEND",
			"timeStamp": timestamp,
		}
		payload.update(updates)
		return payload

	def _post_webhook(self, payload):
		with (
			patch.object(
				bosta_webhooks,
				"_get_bosta_settings",
				return_value=MagicMock(enabled=1, get_password=MagicMock(return_value=SECRET)),
			),
			patch.object(bosta_webhooks.frappe, "get_request_header", return_value=SECRET),
			patch.object(bosta_webhooks, "_webhook_payload", return_value=payload),
		):
			return bosta_webhooks.delivery_event()

	# -- webhook -------------------------------------------------------------------------

	def test_guest_webhook_records_carrier_cancel_without_reversing_posted_stock(self):
		_, shipment = self._completed_shipment()
		delivery_note = shipment.delivery_note
		frappe.set_user("Guest")
		try:
			self.assertEqual(self._post_webhook(self._event(shipment, 49, 5000)), {"accepted": True})
			self.assertEqual(frappe.session.user, "Guest")
		finally:
			frappe.set_user("Administrator")

		shipment.reload()
		self.assertEqual((shipment.status, shipment.bosta_state_code), ("Canceled", 49))
		self.assertEqual(frappe.db.get_value("Delivery Note", delivery_note, "docstatus"), 1)
		self.assertFalse(shipment.delivery_note_error)
		self.assertEqual(shipment.reservation_status, "Fulfilled")
		self.assertEqual(shipment.stock_posting_status, "Posted")

	def test_unknown_tracking_state_or_type_answers_200_and_logs_without_changes(self):
		_, shipment = self._completed_shipment()
		cases = [
			{"trackingNumber": "NOPE"},
			{"state": 999},
			{"type": "SOMETHING_NEW"},
			{"_id": "another-delivery"},
		]
		for updates in cases:
			with self.subTest(updates=updates), patch.object(statuses.frappe, "logger") as logger:
				result = self._post_webhook({**self._event(shipment, 24, 9000), **updates})
				self.assertEqual(result, {"accepted": True})
				logger.return_value.info.assert_called_once()
		shipment.reload()
		self.assertEqual(shipment.last_bosta_event_timestamp or "", "")

	def test_malformed_body_and_bad_auth_are_errors(self):
		with self.assertRaises(frappe.ValidationError):
			self._post_webhook({"state": 24})
		with (
			patch.object(
				bosta_webhooks,
				"_get_bosta_settings",
				return_value=MagicMock(enabled=1, get_password=lambda *a, **k: SECRET),
			),
			patch.object(bosta_webhooks.frappe, "get_request_header", return_value="wrong"),
			self.assertRaises(frappe.PermissionError),
		):
			bosta_webhooks.delivery_event()

	def test_stale_and_duplicate_events_do_not_overwrite_newer_state(self):
		_, shipment = self._completed_shipment()
		self.assertEqual(statuses.process_webhook_event(self._event(shipment, 24, 2000)), statuses.APPLIED)
		self.assertEqual(statuses.process_webhook_event(self._event(shipment, 47, 1000)), statuses.STALE)
		self.assertEqual(statuses.process_webhook_event(self._event(shipment, 47, 2000)), statuses.STALE)
		shipment.reload()
		self.assertEqual(
			(shipment.bosta_state_code, shipment.bosta_state_value), (24, "Received at warehouse")
		)
		self.assertEqual(
			statuses.process_webhook_event(self._event(shipment, 47, 3000, exceptionCode=3)), statuses.APPLIED
		)
		shipment.reload()
		self.assertEqual((shipment.bosta_state_code, shipment.bosta_exception_code), (47, 3))
		self.assertNotEqual(shipment.status, "Canceled")

	def test_carrier_cancel_does_not_attempt_to_cancel_delivery_note(self):
		_, shipment = self._completed_shipment()
		delivery_note = shipment.delivery_note

		with patch(
			"erpnext.stock.doctype.delivery_note.delivery_note.DeliveryNote.cancel",
			side_effect=frappe.ValidationError("linked invoice"),
		) as cancel:
			statuses.process_webhook_event(self._event(shipment, 49, 5000))

		shipment.reload()
		self.assertEqual((shipment.status, shipment.bosta_state_code), ("Canceled", 49))
		cancel.assert_not_called()
		self.assertFalse(shipment.delivery_note_error)
		self.assertEqual(frappe.db.get_value("Delivery Note", delivery_note, "docstatus"), 1)
		self.assertFalse(
			[
				item
				for item in marketplace_attention.get_attention_items({"platform": "Bosta"})
				if item["reference_name"] == shipment.name
			]
		)

	def test_a_failed_delivery_note_creation_is_a_problem_cleared_by_a_successful_resume(self):
		order = self.make_order()
		self.http.queue["create"].append(created())
		with (
			patch(
				"marketplace_integrations.integrations.bosta.deliveries.make_delivery_note",
				side_effect=frappe.ValidationError("boom"),
			),
			self.assertRaises(frappe.ValidationError),
		):
			self.create(order)
		shipment = self.shipment_for(order)
		names = {
			i["reference_name"] for i in marketplace_attention.get_attention_items({"platform": "Bosta"})
		}
		self.assertIn(shipment.name, names)

		self.create(order)

		names = {
			i["reference_name"] for i in marketplace_attention.get_attention_items({"platform": "Bosta"})
		}
		self.assertNotIn(shipment.name, names)

	def _attention_rows_for(self, shipment, problem_type):
		return [
			row
			for row in marketplace_attention.get_attention_items(
				{"platform": "Bosta", "problem_type": problem_type}
			)
			if row["reference_name"] == shipment.name
		]

	def test_carrier_exception_is_listed_in_needs_attention(self):
		_, shipment = self._completed_shipment(tracking=f"TRK-EX-{self.suffix}")
		frappe.db.set_value(
			"Bosta Shipment",
			shipment.name,
			{
				"bosta_state_code": 47,
				"bosta_state_value": statuses.BOSTA_STATE_NAMES[47],
				"bosta_exception_reason": "Receiver unavailable",
			},
		)

		rows = self._attention_rows_for(shipment, marketplace_attention.BOSTA_CARRIER_EXCEPTION)

		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["explanation"], "Receiver unavailable")

	def test_return_finding_clears_only_after_a_submitted_return_against_linked_delivery_note(self):
		from erpnext.controllers.sales_and_purchase_return import make_return_doc

		_, shipment = self._completed_shipment(tracking=f"TRK-RETURN-{self.suffix}")
		frappe.db.set_value(
			"Bosta Shipment",
			shipment.name,
			{
				"bosta_state_code": 46,
				"bosta_state_value": statuses.BOSTA_STATE_NAMES[46],
			},
		)
		self.assertEqual(len(self._attention_rows_for(shipment, marketplace_attention.BOSTA_RETURN)), 1)

		return_note = make_return_doc("Delivery Note", shipment.delivery_note)
		return_note.items[0].qty = -1
		self._track(return_note.insert())
		self.assertEqual(len(self._attention_rows_for(shipment, marketplace_attention.BOSTA_RETURN)), 1)

		return_note.submit()
		self.assertEqual(self._attention_rows_for(shipment, marketplace_attention.BOSTA_RETURN), [])

	def test_delivered_send_without_a_submitted_delivery_note_is_listed(self):
		order = self.make_order()
		self.http.queue["create"].append(created(f"TRK-DELIVERED-{self.suffix}"))
		self.create(order, confirm_handover=False)
		shipment = self.shipment_for(order)
		frappe.db.set_value(
			"Bosta Shipment",
			shipment.name,
			{
				"bosta_state_code": 45,
				"bosta_state_value": statuses.BOSTA_STATE_NAMES[45],
				"bosta_delivery_type": "SEND",
			},
		)

		rows = self._attention_rows_for(shipment, marketplace_attention.BOSTA_DELIVERED_WITHOUT_DELIVERY_NOTE)

		self.assertEqual(len(rows), 1)

	def test_tracked_shipment_without_any_event_uses_creation_for_stale_attention(self):
		order = self.make_order()
		self.http.queue["create"].append(created(f"TRK-STALE-{self.suffix}"))
		self.create(order, confirm_handover=False)
		shipment = self.shipment_for(order)
		frappe.db.set_value(
			"Bosta Shipment",
			shipment.name,
			{"creation": add_days(now_datetime(), -6), "last_bosta_status_on": None},
			update_modified=False,
		)

		rows = self._attention_rows_for(shipment, marketplace_attention.BOSTA_STATUS_STALE)

		self.assertEqual(len(rows), 1)
		self.assertEqual(
			rows[0]["detected_at"], frappe.db.get_value("Bosta Shipment", shipment.name, "creation")
		)

	def test_state_103_notification_escapes_tracking_text_and_keeps_shipment_link(self):
		recipient = self.make_user("Bosta User")
		_, shipment = self._completed_shipment(tracking=f"TRK-NOTIFY-{self.suffix}")
		tracking_number = 'TRK-<script>alert("x")</script>'
		frappe.db.set_value("Bosta Shipment", shipment.name, "tracking_number", tracking_number)
		shipment.reload()
		try:
			statuses.process_webhook_event(self._event(shipment, 103, 9000))
			logs = frappe.get_all(
				"Notification Log",
				filters={
					"for_user": recipient,
					"document_type": "Bosta Shipment",
					"document_name": shipment.name,
				},
				fields=["name", "description"],
				limit_page_length=0,
			)
			self.assertEqual(len(logs), 1)
			self.assertIn("TRK-&lt;script&gt;", logs[0].description)
			self.assertNotIn("<script>", logs[0].description)
			self.assertIn(f"/app/bosta-shipment/{shipment.name}", logs[0].description)
		finally:
			frappe.db.delete(
				"Notification Log",
				{"document_type": "Bosta Shipment", "document_name": shipment.name},
			)

	# -- polling -------------------------------------------------------------------------

	def _poll(self, responses):
		client = MagicMock()
		client.get_delivery.side_effect = lambda tracking: responses[tracking]
		with (
			patch.object(statuses.frappe, "get_single", return_value=SimpleNamespace(enabled=1)),
			patch.object(statuses, "BostaClient", return_value=client),
		):
			updated = statuses.poll_incomplete_shipments()
		return updated, client

	def _state_response(self, code):
		return FakeResponse(200, {"success": True, "data": {"state": {"code": code, "value": "<b>x</b>"}}})

	def test_poll_skips_terminal_canceled_and_old_shipments(self):
		shipments = {}
		for tracking, state_code, status, old in (
			("T-LIVE", 10, "Completed", False),
			("T-DELIVERED", 45, "Completed", False),
			("T-CANCELED-STATE", 49, "Completed", False),
			("T-CANCELED-STATUS", 24, "Canceled", False),
			("T-OLD", 10, "Completed", True),
			("T-NEVER-POLLED", 0, "Completed", False),
		):
			order = self.make_order(submit=False)
			shipment = frappe.get_doc(
				{
					"doctype": "Bosta Shipment",
					"sales_order": order.name,
					"receiver_name": self._receiver(),
					"receiver_phone": "0100",
					"city": "Cairo",
					"city_id": "city-cairo",
					"city_code": "EG-01",
					"address_first_line": "12 Example Street",
					"tracking_number": tracking,
					"bosta_delivery_id": f"D-{tracking}",
					"bosta_state_code": state_code,
					"status": status,
				}
			)
			save_as_system(shipment, insert=True)
			if old:
				frappe.db.set_value(
					"Bosta Shipment",
					shipment.name,
					"creation",
					add_days(now_datetime(), -(statuses.POLL_MAX_AGE_DAYS + 1)),
					update_modified=False,
				)
			shipments[tracking] = shipment
		frappe.db.commit()
		responses = {t: self._state_response(24) for t in ("T-LIVE", "T-NEVER-POLLED")}

		updated, client = self._poll(responses)

		polled = {call.args[0] for call in client.get_delivery.call_args_list}
		self.assertEqual(polled, {"T-LIVE", "T-NEVER-POLLED"})
		self.assertEqual(updated, 2)
		live = frappe.get_doc("Bosta Shipment", shipments["T-LIVE"].name)
		self.assertEqual((live.bosta_state_code, live.bosta_state_value), (24, "Received at warehouse"))
		self.assertIn("<b>x</b>", live.bosta_status_snapshot)

	def test_poll_marks_carrier_cancel_without_reversing_stock_and_ignores_unknown_codes(self):
		_, shipment = self._completed_shipment("T-49")
		_, unknown = self._completed_shipment("T-UNKNOWN")

		updated, _ = self._poll({"T-49": self._state_response(49), "T-UNKNOWN": self._state_response(999)})

		self.assertEqual(updated, 1)
		shipment.reload()
		unknown.reload()
		self.assertEqual(shipment.status, "Canceled")
		self.assertEqual(frappe.db.get_value("Delivery Note", shipment.delivery_note, "docstatus"), 1)
		self.assertEqual(shipment.reservation_status, "Fulfilled")
		self.assertEqual((unknown.status, unknown.bosta_state_code), ("Completed", 0))

	def test_a_stale_poll_cannot_overwrite_a_newer_webhook(self):
		_, shipment = self._completed_shipment("T-STALE")
		far_future = 4_000_000_000_000
		statuses.process_webhook_event(self._event(shipment, 47, far_future))

		updated, _ = self._poll({"T-STALE": self._state_response(10)})

		self.assertEqual(updated, 0)
		shipment.reload()
		self.assertEqual(shipment.bosta_state_code, 47)

	def test_one_poll_failure_does_not_stop_later_shipments(self):
		self._completed_shipment("T-FIRST")
		_, second = self._completed_shipment("T-SECOND")
		responses = {"T-SECOND": self._state_response(24)}

		def get_delivery(tracking):
			if tracking == "T-FIRST":
				raise RuntimeError("network failure")
			return responses[tracking]

		client = MagicMock()
		client.get_delivery.side_effect = get_delivery
		with (
			patch.object(statuses.frappe, "get_single", return_value=SimpleNamespace(enabled=1)),
			patch.object(statuses, "BostaClient", return_value=client),
			patch.object(statuses.frappe, "log_error") as log_error,
		):
			self.assertEqual(statuses.poll_incomplete_shipments(), 1)

		log_error.assert_called_once()
		self.assertEqual(frappe.db.get_value("Bosta Shipment", second.name, "bosta_state_code"), 24)

	def test_cancel_without_a_submitted_delivery_note_still_cancels_the_shipment(self):
		# No Delivery Note yet (Bosta Created) ...
		order = self.make_order(submit=True)
		shipment = frappe.get_doc(
			{
				"doctype": "Bosta Shipment",
				"sales_order": order.name,
				"receiver_name": self._receiver(),
				"receiver_phone": "0100",
				"city": "Cairo",
				"city_id": "city-cairo",
				"city_code": "EG-01",
				"address_first_line": "12 Example Street",
				"tracking_number": "T-NODN",
				"bosta_delivery_id": "D-T-NODN",
				"status": "Bosta Created",
			}
		)
		save_as_system(shipment, insert=True)
		self.assertEqual(statuses.process_webhook_event(self._event(shipment, 49, 5000)), statuses.APPLIED)
		self.assertEqual(frappe.db.get_value("Bosta Shipment", shipment.name, "status"), "Canceled")

		# ... and a Delivery Note that is not submitted.
		_, other = self._completed_shipment("T-DRAFT")
		frappe.db.set_value("Delivery Note", other.delivery_note, "docstatus", 0)
		self.assertEqual(statuses.process_webhook_event(self._event(other, 49, 5000)), statuses.APPLIED)
		self.assertEqual(frappe.db.get_value("Bosta Shipment", other.name, "status"), "Canceled")
		frappe.db.set_value("Delivery Note", other.delivery_note, "docstatus", 1)

	def test_a_later_49_on_a_canceled_shipment_changes_nothing_else(self):
		_, shipment = self._completed_shipment("T-TWICE")
		statuses.process_webhook_event(self._event(shipment, 49, 5000))

		self.assertEqual(statuses.process_webhook_event(self._event(shipment, 49, 6000)), statuses.APPLIED)

		shipment.reload()
		self.assertEqual((shipment.status, shipment.delivery_note_error), ("Canceled", ""))
		self.assertEqual(frappe.db.get_value("Delivery Note", shipment.delivery_note, "docstatus"), 1)
		self.assertEqual(shipment.reservation_status, "Fulfilled")

	def test_the_calling_user_is_restored_when_processing_raises(self):
		frappe.set_user("Guest")
		try:
			with patch.object(bosta_webhooks, "process_webhook_event", side_effect=RuntimeError("boom")):
				with self.assertRaises(RuntimeError):
					self._post_webhook({"anything": 1})
			self.assertEqual(frappe.session.user, "Guest")
		finally:
			frappe.set_user("Administrator")

	def test_an_unchanged_polled_state_is_not_saved_again(self):
		_, shipment = self._completed_shipment("T-SAME")
		self._poll({"T-SAME": self._state_response(24)})
		shipment.reload()
		checked_on, modified = shipment.last_bosta_status_on, shipment.modified

		updated, _ = self._poll({"T-SAME": self._state_response(24)})

		self.assertEqual(updated, 0)
		shipment.reload()
		self.assertEqual((shipment.last_bosta_status_on, shipment.modified), (checked_on, modified))

	def test_unchanged_state_can_still_update_a_changed_delivery_fee(self):
		order, shipment = self._completed_shipment("T-FEE")
		self.assertEqual(
			statuses.apply_state(
				shipment.tracking_number,
				state_code=45,
				timestamp=5000,
				snapshot={"code": 45},
				shipment_fee=50,
			),
			statuses.APPLIED,
		)
		shipment.reload()
		result = statuses.apply_state(
			shipment.tracking_number,
			state_code=45,
			timestamp=6000,
			snapshot={"code": 45},
			skip_if_unchanged=True,
			shipment_fee=87,
		)

		self.assertEqual(result, statuses.APPLIED)
		shipment.reload()
		self.assertEqual(shipment.shipment_fee, 87)
		self.assertEqual(frappe.db.get_value("Sales Order", order.name, "custom_bosta_delivery_status"), "Delivered")

	def test_sales_order_status_backfill_uses_latest_shipment_and_is_idempotent(self):
		order, shipment = self._completed_shipment("T-STATUS-BACKFILL")
		statuses.apply_state(
			shipment.tracking_number,
			state_code=45,
			timestamp=5000,
			snapshot={"code": 45},
		)
		frappe.db.set_value("Sales Order", order.name, "custom_bosta_delivery_status", "", update_modified=False)
		from marketplace_integrations.patches.v0_0.backfill_sales_order_bosta_delivery_status import execute

		execute()
		self.assertEqual(frappe.db.get_value("Sales Order", order.name, "custom_bosta_delivery_status"), "Delivered")
		modified = frappe.db.get_value("Sales Order", order.name, "modified")
		execute()
		self.assertEqual(frappe.db.get_value("Sales Order", order.name, "modified"), modified)

	def test_repeated_carrier_cancel_does_not_create_delivery_note_cancellation_errors(self):
		_, shipment = self._completed_shipment("T-REPEAT")
		with patch(
			"erpnext.stock.doctype.delivery_note.delivery_note.DeliveryNote.cancel",
			side_effect=frappe.ValidationError("linked invoice"),
		) as cancel:
			statuses.process_webhook_event(self._event(shipment, 49, 5000))
			statuses.process_webhook_event(self._event(shipment, 49, 6000))
		cancel.assert_not_called()

		shipment.reload()
		self.assertEqual((shipment.status, shipment.delivery_note_error), ("Canceled", ""))
		self.assertEqual(frappe.db.get_value("Delivery Note", shipment.delivery_note, "docstatus"), 1)


class IntegrationTestBostaRefresh(BostaFixtureMixin, IntegrationTestCase):
	"""refresh_shipment_state (#271): one GET delivery, then the shared apply_state."""

	def _live(self, tracking="TRK-R"):
		order = self.make_order()
		self.http.queue["create"].append(created(tracking, f"D-{tracking}"))
		self.create(order)
		return order, self.shipment_for(order)

	def _state(self, code):
		return FakeResponse(200, {"success": True, "data": {"state": {"code": code, "value": "x"}}})

	def test_refresh_reads_and_stores_a_numeric_delivery_fee(self):
		order, shipment = self._live("TRK-FEE")
		self.http.queue["get"].append(
			FakeResponse(200, {"success": True, "data": {"state": {"code": 45}, "shipmentFees": 87}})
		)

		bosta_shipments.refresh_shipment(shipment.name)

		shipment.reload()
		self.assertEqual(shipment.shipment_fee, 87)
		self.assertEqual(frappe.db.get_value("Sales Order", order.name, "custom_bosta_delivery_status"), "Delivered")

	def test_refresh_applies_the_state_and_updates_the_last_update_time(self):
		_, shipment = self._live()
		self.http.queue["get"].append(self._state(24))

		result = bosta_shipments.refresh_shipment(shipment.name)

		self.assertEqual(result, {"result": statuses.APPLIED})
		self.assertEqual(len(self.http.calls_of("get")), 1)
		shipment.reload()
		self.assertEqual(
			(shipment.bosta_state_code, shipment.bosta_state_value), (24, "Received at warehouse")
		)
		self.assertTrue(shipment.last_bosta_status_on)
		self.assertEqual(shipment.status, "Completed")

	def test_state_49_preserves_the_submitted_delivery_note_and_fulfilled_reservation(self):
		_, shipment = self._live()
		self.http.queue["get"].append(self._state(49))

		bosta_shipments.refresh_shipment(shipment.name)

		shipment.reload()
		self.assertEqual(shipment.status, "Canceled")
		self.assertEqual(frappe.db.get_value("Delivery Note", shipment.delivery_note, "docstatus"), 1)
		self.assertEqual(shipment.reservation_status, "Fulfilled")

	def test_a_bosta_error_or_network_failure_stores_nothing_and_is_escaped(self):
		_, shipment = self._live()
		before = shipment.modified
		for queued in (
			FakeResponse(400, {"success": False, "message": "<img src=x onerror=1>"}),
			frappe.ValidationError("Failed to reach Bosta."),
			self._state(999),
			FakeResponse(200, {"success": True, "data": {}}),
		):
			with self.subTest(queued=str(queued)):
				self.http.queue["get"].append(queued)
				with self.assertRaises(frappe.ValidationError) as caught:
					bosta_shipments.refresh_shipment(shipment.name)
				self.assertNotIn("<img", str(caught.exception))
				shipment.reload()
				self.assertEqual((shipment.bosta_state_code, shipment.modified), (0, before))

	def test_a_shipment_without_a_tracking_number_makes_no_request(self):
		order = self.make_order(submit=False)
		shipment = save_as_system(
			frappe.get_doc(
				{
					"doctype": "Bosta Shipment",
					"sales_order": order.name,
					"receiver_name": self._receiver(),
					"receiver_phone": "0100",
					"city": "Cairo",
					"city_id": "city-cairo",
					"city_code": "EG-01",
					"address_first_line": "12 Example Street",
					"status": "Bosta Failed",
				}
			),
			insert=True,
		)
		frappe.db.commit()
		with self.assertRaises(frappe.ValidationError):
			bosta_shipments.refresh_shipment(shipment.name)
		self.assertEqual(self.http.calls, [])

	def test_refresh_is_creator_only_and_post_only(self):
		_, shipment = self._live()
		for role, allowed in (("Sales Manager", True), ("Stock User", True), ("Sales User", False)):
			with self.subTest(role=role):
				user = self.make_user(role)
				if allowed:
					self.http.queue["get"].append(self._state(24))
				frappe.set_user(user)
				try:
					if allowed:
						bosta_shipments.refresh_shipment(shipment.name)
					else:
						with self.assertRaises(frappe.PermissionError):
							bosta_shipments.refresh_shipment(shipment.name)
				finally:
					frappe.set_user("Administrator")
		self.assertEqual(len(self.http.calls_of("get")), 2)
		self.assertIn(bosta_shipments.refresh_shipment, frappe.whitelisted)
		self.assertEqual(
			frappe.allowed_http_methods_for_whitelisted_func[bosta_shipments.refresh_shipment], ["POST"]
		)


class IntegrationTestBostaCancel(BostaFixtureMixin, IntegrationTestCase):
	"""cancel_bosta_shipment (#272)."""

	def _live(self, tracking="TRK-C"):
		order = self.make_order()
		self.http.queue["create"].append(created(tracking, f"D-{tracking}"))
		self.create(order)
		return order, self.shipment_for(order)

	def _ok(self):
		return FakeResponse(200, {"success": True, "message": "terminated", "data": {"_id": "x"}})

	def _state(self, code):
		return FakeResponse(200, {"success": True, "data": {"state": {"code": code, "value": "x"}}})

	def tearDown(self):
		# The invoice references the Delivery Note, so it goes first.
		frappe.set_user("Administrator")
		frappe.db.rollback()
		invoice = getattr(self, "_invoice", None)
		if invoice and frappe.db.exists("Sales Invoice", invoice):
			if frappe.db.get_value("Sales Invoice", invoice, "docstatus") == 1:
				frappe.get_doc("Sales Invoice", invoice).cancel()
			frappe.delete_doc("Sales Invoice", invoice, force=True, ignore_permissions=True)
			frappe.db.commit()
		super().tearDown()

	def test_blocked_cases_never_reach_bosta(self):
		_, shipment = self._live()
		blocked = []
		# No tracking number.
		no_tracking = save_as_system(
			frappe.get_doc(
				{
					"doctype": "Bosta Shipment",
					"sales_order": self.make_order(submit=False).name,
					"receiver_name": self._receiver(),
					"receiver_phone": "0100",
					"city": "Cairo",
					"city_id": "city-cairo",
					"city_code": "EG-01",
					"address_first_line": "12 Example Street",
					"status": "Bosta Failed",
				}
			),
			insert=True,
		)
		frappe.db.commit()
		blocked.append(no_tracking.name)
		# Delivered.
		frappe.db.set_value("Bosta Shipment", shipment.name, "bosta_state_code", 45)
		blocked.append(shipment.name)
		for name in blocked:
			with self.subTest(shipment=name), self.assertRaises(frappe.ValidationError):
				bosta_shipments.cancel_shipment(name)
		# Delivery Note on a submitted Sales Invoice.
		frappe.db.set_value("Bosta Shipment", shipment.name, "bosta_state_code", 24)
		invoice = make_sales_invoice(shipment.delivery_note)
		invoice.insert()
		invoice.submit()
		self._invoice = invoice.name
		frappe.db.commit()
		self.http.queue["terminate"].append(self._ok())
		result = bosta_shipments.cancel_shipment(shipment.name)
		self.assertTrue(result["canceled"])
		self.assertEqual(frappe.db.get_value("Delivery Note", shipment.delivery_note, "docstatus"), 1)
		self.assertEqual(frappe.db.get_value("Sales Invoice", invoice.name, "docstatus"), 1)
		# A manually repeated local cancellation is idempotent and never calls Bosta again.
		frappe.db.set_value("Bosta Shipment", shipment.name, "status", "Canceled")
		self.assertTrue(bosta_shipments.cancel_shipment(shipment.name)["canceled"])
		self.assertEqual(len(self.http.calls_of("terminate")), 1)
		self.assertEqual(frappe.db.get_value("Delivery Note", shipment.delivery_note, "docstatus"), 1)

	def test_every_other_state_is_sent_to_bosta_and_a_bosta_error_changes_nothing(self):
		_, shipment = self._live()
		for code in (0, 10, 24, 46, 47, 60, 100):
			with self.subTest(state=code):
				frappe.db.set_value("Bosta Shipment", shipment.name, "bosta_state_code", code)
				self.http.queue["terminate"].append(
					FakeResponse(400, {"success": False, "message": "<img src=x onerror=1> not allowed"})
				)
				with self.assertRaises(frappe.ValidationError) as caught:
					bosta_shipments.cancel_shipment(shipment.name)
				self.assertNotIn("<img", str(caught.exception))
		self.assertEqual(len(self.http.calls_of("terminate")), 7)
		shipment.reload()
		self.assertEqual((shipment.status, shipment.bosta_terminated_on), ("Completed", None))
		self.assertEqual(frappe.db.get_value("Delivery Note", shipment.delivery_note, "docstatus"), 1)

	def test_success_cancels_carrier_shipment_without_reversing_posted_stock(self):
		order, shipment = self._live()
		self.http.queue["terminate"].append(self._ok())

		result = bosta_shipments.cancel_shipment(shipment.name)

		self.assertEqual(result, {"canceled": True, "delivery_note_error": ""})
		self.assertTrue(self.http.calls_of("terminate")[0].endswith("/business/TRK-C/terminate"))
		shipment.reload()
		self.assertEqual(shipment.status, "Canceled")
		self.assertTrue(shipment.bosta_terminated_on)
		self.assertEqual(shipment.reservation_status, "Fulfilled")
		self.assertEqual(frappe.db.get_value("Delivery Note", shipment.delivery_note, "docstatus"), 1)
		self.assertEqual(frappe.db.count("Bosta Shipment", {"sales_order": order.name}), 1)

	def test_bosta_terminate_does_not_attempt_to_cancel_the_delivery_note(self):
		_, shipment = self._live()
		self.http.queue["terminate"].append(self._ok())
		with patch(
			"erpnext.stock.doctype.delivery_note.delivery_note.DeliveryNote.cancel",
			side_effect=frappe.ValidationError("linked invoice"),
		) as cancel:
			result = bosta_shipments.cancel_shipment(shipment.name)

		self.assertTrue(result["canceled"])
		cancel.assert_not_called()
		shipment.reload()
		self.assertEqual((shipment.status, shipment.bosta_state_code), ("Canceled", 0))
		self.assertTrue(shipment.bosta_terminated_on)
		self.assertFalse(shipment.delivery_note_error)
		self.assertEqual(shipment.reservation_status, "Fulfilled")
		self.assertEqual(frappe.db.get_value("Delivery Note", shipment.delivery_note, "docstatus"), 1)

	def test_carrier_termination_never_retries_or_cancels_posted_stock(self):
		_, shipment = self._live()
		self.http.queue["terminate"].append(self._ok())
		with (
			patch.object(statuses, "_cancel_linked_delivery_note", wraps=statuses._cancel_linked_delivery_note) as carrier_cancel,
		):
			result = bosta_shipments.cancel_shipment(shipment.name)
		self.assertTrue(result["canceled"])
		carrier_cancel.assert_called_once()
		shipment.reload()
		self.assertEqual(shipment.status, "Canceled")
		self.assertTrue(shipment.bosta_terminated_on)
		self.assertEqual(frappe.db.get_value("Delivery Note", shipment.delivery_note, "docstatus"), 1)
		self.assertEqual(shipment.reservation_status, "Fulfilled")

	def test_cancelling_again_after_bosta_terminated_does_not_send_a_second_delete(self):
		_, shipment = self._live()
		self.http.queue["terminate"].append(self._ok())
		with patch(
			"erpnext.stock.doctype.delivery_note.delivery_note.DeliveryNote.cancel",
			side_effect=frappe.ValidationError("linked"),
		) as cancel:
			self.assertTrue(bosta_shipments.cancel_shipment(shipment.name)["canceled"])
		cancel.assert_not_called()
		self.assertEqual(len(self.http.calls_of("terminate")), 1)

		# Second click (and a stored 48/49): straight to the local finish, no DELETE.
		result = bosta_shipments.cancel_shipment(shipment.name)

		self.assertTrue(result["canceled"])
		self.assertEqual(len(self.http.calls_of("terminate")), 1)
		shipment.reload()
		self.assertEqual(shipment.status, "Canceled")

		_, other = self._live("TRK-48")
		frappe.db.set_value("Bosta Shipment", other.name, "bosta_state_code", 48)
		self.assertTrue(bosta_shipments.cancel_shipment(other.name)["canceled"])
		self.assertEqual(len(self.http.calls_of("terminate")), 1)

	def test_the_retry_ignores_a_failed_note_without_49_or_a_terminate(self):
		_, shipment = self._live()
		frappe.db.set_value("Bosta Shipment", shipment.name, "delivery_note_error", "some failure")
		statuses.retry_failed_cancellations()
		shipment.reload()
		self.assertEqual(shipment.status, "Completed")
		self.assertEqual(frappe.db.get_value("Delivery Note", shipment.delivery_note, "docstatus"), 1)

	def test_a_network_error_changes_nothing_then_reads_the_delivery_back(self):
		for reported, finished in ((10, False), (48, True), (49, True)):
			with self.subTest(reported=reported):
				_, shipment = self._live(f"TRK-N{reported}")
				self.http.queue["terminate"].append(frappe.ValidationError("Failed to reach Bosta."))
				self.http.queue["get"].append(self._state(reported))
				if finished:
					bosta_shipments.cancel_shipment(shipment.name)
				else:
					with self.assertRaises(frappe.ValidationError) as caught:
						bosta_shipments.cancel_shipment(shipment.name)
					self.assertIn("Check the delivery in Bosta", str(caught.exception))
				shipment.reload()
				self.assertEqual(shipment.status == "Canceled", finished)
				self.assertEqual(
					frappe.db.get_value("Delivery Note", shipment.delivery_note, "docstatus"),
					1,
				)
				if reported == 48:
					self.assertTrue(shipment.bosta_terminated_on)
				if not finished:
					self.assertIsNone(shipment.bosta_terminated_on)
					self.assertEqual(shipment.bosta_state_code, 10)

	def test_a_network_error_and_a_failed_read_back_asks_the_user_to_verify(self):
		_, shipment = self._live()
		self.http.queue["terminate"].append(frappe.ValidationError("Failed to reach Bosta."))
		self.http.queue["get"].append(frappe.ValidationError("Failed to reach Bosta."))
		with self.assertRaises(frappe.ValidationError) as caught:
			bosta_shipments.cancel_shipment(shipment.name)
		self.assertIn("Check the delivery in Bosta", str(caught.exception))
		shipment.reload()
		self.assertEqual((shipment.status, shipment.bosta_terminated_on), ("Completed", None))

	def test_cancel_is_creator_only_and_post_only(self):
		_, shipment = self._live()
		frappe.set_user(self.make_user("Sales User"))
		try:
			with self.assertRaises(frappe.PermissionError):
				bosta_shipments.cancel_shipment(shipment.name)
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(self.http.calls_of("terminate"), [])
		self.http.queue["terminate"].append(self._ok())
		frappe.set_user(self.make_user("Stock User"))
		try:
			self.assertTrue(bosta_shipments.cancel_shipment(shipment.name)["canceled"])
		finally:
			frappe.set_user("Administrator")
		self.assertIn(bosta_shipments.cancel_shipment, frappe.whitelisted)
		self.assertEqual(
			frappe.allowed_http_methods_for_whitelisted_func[bosta_shipments.cancel_shipment], ["POST"]
		)
