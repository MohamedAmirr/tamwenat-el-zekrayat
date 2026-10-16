# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Real-database tests of webhook/poll state application, cancellation and Needs Attention."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, now_datetime

from marketplace_integrations.api import bosta_shipments, bosta_webhooks
from marketplace_integrations.integrations.bosta import statuses
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


class IntegrationTestBostaEvents(BostaFixtureMixin, IntegrationTestCase):
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

	def test_guest_webhook_cancels_the_delivery_note_and_allows_a_new_shipment(self):
		order, shipment = self._completed_shipment()
		delivery_note = shipment.delivery_note
		frappe.set_user("Guest")
		try:
			self.assertEqual(self._post_webhook(self._event(shipment, 49, 5000)), {"accepted": True})
			self.assertEqual(frappe.session.user, "Guest")
		finally:
			frappe.set_user("Administrator")

		shipment.reload()
		self.assertEqual((shipment.status, shipment.bosta_state_code), ("Canceled", 49))
		self.assertEqual(frappe.db.get_value("Delivery Note", delivery_note, "docstatus"), 2)
		self.assertFalse(shipment.delivery_note_error)
		self.assertIsNone(shipment.active_sales_order)

		self.http.queue["create"].append(created("TRK-NEW", "D-NEW"))
		self.create(order)
		new = self.shipment_for(order, status="Completed")
		self.assertEqual(new.tracking_number, "TRK-NEW")
		self.assertNotEqual(new.name, shipment.name)

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

	def test_a_failed_cancellation_is_a_needs_attention_problem_until_a_retry_succeeds(self):
		_, shipment = self._completed_shipment()
		delivery_note = shipment.delivery_note

		with patch(
			"erpnext.stock.doctype.delivery_note.delivery_note.DeliveryNote.cancel",
			side_effect=frappe.ValidationError("linked invoice"),
		):
			statuses.process_webhook_event(self._event(shipment, 49, 5000))

		shipment.reload()
		self.assertEqual((shipment.status, shipment.bosta_state_code), ("Completed", 49))
		self.assertIn("linked invoice", shipment.delivery_note_error)
		self.assertIn("could not cancel", shipment.last_error)
		self.assertEqual(frappe.db.get_value("Delivery Note", delivery_note, "docstatus"), 1)
		items = [
			item
			for item in marketplace_attention.get_attention_items({"platform": "Bosta"})
			if item["reference_name"] == shipment.name
		]
		self.assertEqual(len(items), 1)
		self.assertEqual(
			(items[0]["problem_type"], items[0]["platform"], items[0]["account"], items[0]["next_action"]),
			("Bosta Delivery Note", "Bosta", "", "Open Shipment"),
		)
		self.assertTrue(
			any(i["reference_name"] == shipment.name for i in marketplace_attention.get_attention_items())
		)
		self.assertFalse(marketplace_attention.get_attention_items({"account": "any"}))
		row = marketplace_channels._bosta_rows()[0]
		self.assertEqual((row["platform"], row["level"]), ("Bosta", "Error"))
		self.assertGreaterEqual(row["orders_needing_attention"], 1)

		statuses.retry_failed_cancellations()

		shipment.reload()
		self.assertEqual(
			(shipment.status, shipment.delivery_note_error, shipment.last_error), ("Canceled", "", "")
		)
		self.assertEqual(frappe.db.get_value("Delivery Note", delivery_note, "docstatus"), 2)
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
					"district_id": "d",
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

	def test_poll_cancels_the_delivery_note_on_state_49_and_ignores_unknown_codes(self):
		_, shipment = self._completed_shipment("T-49")
		_, unknown = self._completed_shipment("T-UNKNOWN")

		updated, _ = self._poll({"T-49": self._state_response(49), "T-UNKNOWN": self._state_response(999)})

		self.assertEqual(updated, 1)
		shipment.reload()
		unknown.reload()
		self.assertEqual(shipment.status, "Canceled")
		self.assertEqual(frappe.db.get_value("Delivery Note", shipment.delivery_note, "docstatus"), 2)
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
				"district_id": "d",
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
		self.assertEqual(frappe.db.get_value("Delivery Note", shipment.delivery_note, "docstatus"), 2)

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

	def test_a_repeated_cancel_failure_is_logged_once_and_keeps_its_detected_time(self):
		_, shipment = self._completed_shipment("T-REPEAT")
		with patch(
			"erpnext.stock.doctype.delivery_note.delivery_note.DeliveryNote.cancel",
			side_effect=frappe.ValidationError("linked invoice"),
		):
			statuses.process_webhook_event(self._event(shipment, 49, 5000))
			shipment.reload()
			first_seen = shipment.delivery_note_error_at
			self.assertTrue(first_seen)
			with patch.object(statuses.frappe, "log_error") as log_error:
				statuses.retry_failed_cancellations()
				statuses.retry_failed_cancellations()
			log_error.assert_not_called()

		shipment.reload()
		self.assertEqual(shipment.delivery_note_error_at, first_seen)
		item = next(
			i
			for i in marketplace_attention.get_attention_items({"platform": "Bosta"})
			if i["reference_name"] == shipment.name
		)
		self.assertEqual(item["detected_at"], first_seen)


class IntegrationTestBostaRefresh(BostaFixtureMixin, IntegrationTestCase):
	"""refresh_shipment_state (#271): one GET delivery, then the shared apply_state."""

	def _live(self, tracking="TRK-R"):
		order = self.make_order()
		self.http.queue["create"].append(created(tracking, f"D-{tracking}"))
		self.create(order)
		return order, self.shipment_for(order)

	def _state(self, code):
		return FakeResponse(200, {"success": True, "data": {"state": {"code": code, "value": "x"}}})

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

	def test_state_49_cancels_the_delivery_note_through_the_existing_path(self):
		_, shipment = self._live()
		self.http.queue["get"].append(self._state(49))

		bosta_shipments.refresh_shipment(shipment.name)

		shipment.reload()
		self.assertEqual(shipment.status, "Canceled")
		self.assertEqual(frappe.db.get_value("Delivery Note", shipment.delivery_note, "docstatus"), 2)
		self.assertIsNone(shipment.active_sales_order)

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
					"district_id": "d",
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
