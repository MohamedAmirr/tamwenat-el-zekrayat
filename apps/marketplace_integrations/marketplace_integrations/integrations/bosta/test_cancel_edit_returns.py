# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Cancel at Bosta, edit a created delivery, the draft return of a returned shipment and the cost receipt
stamp (epic #413, #410 / #411 / #412). Real database; only the HTTP layer is faked
(``tests/bosta_fixtures.py``) and ``frappe.enqueue`` is patched."""

import json

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_to_date, now_datetime

from marketplace_integrations.api import bosta_shipments
from marketplace_integrations.integrations.bosta import delivery_note_shipment as shipment
from marketplace_integrations.integrations.bosta import editing, returns, statuses
from marketplace_integrations.services import marketplace_attention as attention
from marketplace_integrations.tests.bosta_fixtures import BostaFixtureMixin, FakeResponse
from marketplace_integrations.tests.utils import make_role_user

OK = FakeResponse(200, {"success": True})


def stored(note, *fields):
	return frappe.db.get_value("Delivery Note", note.name, list(fields), as_dict=True)


def delivery(state, fee=None):
	data = {"state": {"code": state, "value": "x"}, "type": "SEND"}
	if fee is not None:
		data["shipmentFees"] = fee
	return FakeResponse(200, {"success": True, "data": data})


class IntegrationTestBostaCancelEditReturns(BostaFixtureMixin, IntegrationTestCase):
	def _live(self, order=None, **values):
		note = self.make_note(order)
		frappe.db.set_value(
			"Delivery Note",
			note.name,
			{
				"custom_integration_status": "Created",
				"custom_carrier_delivery_id": "DELIVERY-1",
				"custom_carrier_reference": note.name,
				"lr_no": "TRK-1",
				**values,
			},
		)
		frappe.db.commit()
		return note

	def _sent_kinds(self):
		return [kind for kind, _ in self.http.sent()]

	# -- cancel -------------------------------------------------------------------------------

	def test_a_documented_200_cancels_and_logs_success(self):
		note = self._live()
		self.http.queue["terminate"].append(OK)

		result = shipment.cancel_at_bosta(note.name)

		self.assertEqual(result["outcome"], "cancelled")
		self.assertEqual(stored(note, "custom_integration_status").custom_integration_status, "Cancelled")
		log = frappe.get_all(
			"Integration Request",
			filters={"reference_docname": note.name, "custom_operation_type": "Cancel"},
			fields=["custom_result"],
		)
		self.assertEqual([row.custom_result for row in log], ["Success"])
		self.assertEqual(self._sent_kinds(), ["terminate"])

	def test_a_refusal_is_resolved_by_one_read_that_shows_it_cancelled(self):
		note = self._live()
		self.http.queue["terminate"].append(FakeResponse(400, {"success": False, "message": "no"}))
		self.http.queue["get"].append(delivery(49))

		result = shipment.cancel_at_bosta(note.name)

		self.assertEqual(result["outcome"], "cancelled")
		self.assertEqual(stored(note, "custom_integration_status").custom_integration_status, "Cancelled")
		self.assertEqual(self._sent_kinds(), ["terminate", "get"])

	def test_a_refusal_with_a_live_state_goes_back_to_created_with_the_raw_error(self):
		note = self._live()
		self.http.queue["terminate"].append(FakeResponse(400, {"success": False, "message": "bad <b>x</b>"}))
		self.http.queue["get"].append(delivery(24))

		result = shipment.cancel_at_bosta(note.name)

		self.assertEqual(result["outcome"], "refused")
		values = stored(note, "custom_integration_status", "custom_last_integration_error")
		self.assertEqual(values.custom_integration_status, "Created")
		self.assertIn("<b>x</b>", values.custom_last_integration_error)

	def test_a_timeout_and_a_5xx_are_cancellation_unknown_without_a_read(self):
		for response in (TimeoutError("slow"), FakeResponse(503, {"success": False})):
			with self.subTest(str(response)):
				note = self._live()
				self.http.queue["terminate"].append(response)

				result = shipment.cancel_at_bosta(note.name)

				self.assertEqual(result["outcome"], "unknown")
				self.assertEqual(
					stored(note, "custom_integration_status").custom_integration_status,
					"Cancellation Unknown",
				)
				self.assertNotIn("get", self._sent_kinds())

	def test_an_unclear_terminate_answer_is_settled_by_one_read(self):
		unclear = (
			FakeResponse(404, {}),
			FakeResponse(400, {"success": False, "errorCode": 1066, "message": "Delivery not found"}),
			FakeResponse(400, {"success": False, "message": "Delivery already terminated"}),
		)
		for answer in unclear:
			with self.subTest(answer.text):
				cancelled = self._live()
				self.http.queue["terminate"].append(answer)
				self.http.queue["get"].append(delivery(48))
				result = shipment.cancel_at_bosta(cancelled.name)
				self.assertEqual(result["outcome"], "cancelled")
				self.assertEqual(
					stored(cancelled, "custom_integration_status").custom_integration_status, "Cancelled"
				)

				live = self._live()
				self.http.queue["terminate"].append(answer)
				self.http.queue["get"].append(delivery(24))
				result = shipment.cancel_at_bosta(live.name)
				self.assertEqual(result["outcome"], "refused")
				values = stored(live, "custom_integration_status", "custom_last_integration_error")
				self.assertEqual(values.custom_integration_status, "Created")
				self.assertIn("Bosta request failed", values.custom_last_integration_error)

				missing = self._live()
				self.http.queue["terminate"].append(answer)
				self.http.queue["get"].append(FakeResponse(404, {}))
				self.assertEqual(shipment.cancel_at_bosta(missing.name)["outcome"], "unknown")
				self.assertEqual(
					stored(missing, "custom_integration_status").custom_integration_status,
					"Cancellation Unknown",
				)

				failed = self._live()
				self.http.queue["terminate"].append(answer)
				self.http.queue["get"].append(TimeoutError("slow"))
				self.assertEqual(shipment.cancel_at_bosta(failed.name)["outcome"], "unknown")
				self.assertEqual(
					stored(failed, "custom_integration_status").custom_integration_status,
					"Cancellation Unknown",
				)

	def test_an_unclear_answer_makes_exactly_one_read(self):
		note = self._live()
		self.http.queue["terminate"].append(FakeResponse(404, {}))
		self.http.queue["get"].append(delivery(24))

		shipment.cancel_at_bosta(note.name)

		self.assertEqual(self._sent_kinds(), ["terminate", "get"])

	# -- Cancellation Unknown resolved by a later state ---------------------------------------

	def test_a_refresh_showing_a_live_state_returns_cancellation_unknown_to_created(self):
		note = self._live(custom_integration_status="Cancellation Unknown", custom_last_integration_error="x")
		self.http.queue["get"].append(delivery(24))

		statuses.refresh_note(note.name)

		values = stored(note, "custom_integration_status", "custom_last_integration_error")
		self.assertEqual(values.custom_integration_status, "Created")
		self.assertFalse(values.custom_last_integration_error)

	def test_a_webhook_showing_a_live_state_returns_cancellation_unknown_to_created(self):
		note = self._live(custom_integration_status="Cancellation Unknown")
		self.assertEqual(statuses._write_state(note.name, 24, 9000, event=True), "applied")
		self.assertEqual(stored(note, "custom_integration_status").custom_integration_status, "Created")

	def test_a_live_state_does_not_touch_a_cancellation_that_is_still_pending(self):
		note = self._live(custom_integration_status="Cancellation Pending")
		self.assertEqual(statuses._write_state(note.name, 24, 9000, event=True), "conflict")
		statuses._write_state(note.name, 24)
		self.assertEqual(
			stored(note, "custom_integration_status").custom_integration_status, "Cancellation Pending"
		)

	def test_a_refresh_showing_cancelled_still_cancels_cancellation_unknown(self):
		note = self._live(custom_integration_status="Cancellation Unknown")
		self.http.queue["get"].append(delivery(49))
		statuses.refresh_note(note.name)
		self.assertEqual(stored(note, "custom_integration_status").custom_integration_status, "Cancelled")

	def test_an_already_cancelled_note_makes_no_bosta_call(self):
		note = self._live(custom_integration_status="Cancelled")

		result = shipment.cancel_at_bosta(note.name)

		self.assertEqual(result["status"], "Cancelled")
		self.assertEqual(self.http.sent(), [])

	def test_a_fresh_pending_is_refused_and_a_stale_one_is_retried(self):
		note = self._live(custom_integration_status="Cancellation Pending")
		with self.assertRaises(frappe.ValidationError):
			shipment.cancel_at_bosta(note.name)
		self.assertEqual(self.http.sent(), [])

		frappe.db.set_value(
			"Delivery Note",
			note.name,
			"modified",
			add_to_date(now_datetime(), minutes=-5),
			update_modified=False,
		)
		frappe.db.commit()
		self.http.queue["terminate"].append(OK)
		self.assertEqual(shipment.cancel_at_bosta(note.name)["outcome"], "cancelled")

	def test_only_a_created_shipment_can_be_cancelled_at_bosta(self):
		note = self.make_note()
		with self.assertRaises(frappe.ValidationError):
			shipment.cancel_at_bosta(note.name)
		self.assertEqual(self.http.sent(), [])

	def test_cancel_and_delete_are_allowed_only_once_cancelled(self):
		note = self._live()
		with self.assertRaises(frappe.ValidationError):
			frappe.delete_doc("Delivery Note", note.name)
		frappe.db.set_value("Delivery Note", note.name, "custom_integration_status", "Cancelled")
		frappe.delete_doc("Delivery Note", note.name, force=False)
		self.assertFalse(frappe.db.exists("Delivery Note", note.name))

	def test_the_cancel_api_needs_permission_on_the_note(self):
		note = self._live()
		email, _role = make_role_user(self, "Bosta Cancel Reader", {"Delivery Note": ["read"]})
		frappe.set_user(email)
		with self.assertRaises(frappe.PermissionError):
			bosta_shipments.cancel_at_bosta(note.name)
		frappe.set_user("Administrator")
		self.assertEqual(self.http.sent(), [])

	def test_a_delivery_cancelled_from_here_is_not_a_needs_attention_item(self):
		note = self._live()
		self.http.queue["terminate"].append(OK)
		shipment.cancel_at_bosta(note.name)

		items = attention._collect_bosta_items(attention.BOSTA_CANCELLED)
		self.assertEqual([i for i in items if i["reference_name"] == note.name], [])

	# -- edit ---------------------------------------------------------------------------------

	def _editable(self, **values):
		values.setdefault("custom_shipping_status", statuses.BOSTA_STATE_NAMES[10])
		snapshot = {
			"receiver": {"phone": "01000000000"},
			"dropOffAddress": {"city": "EG-01", "firstLine": "12 Example Street"},
		}
		return self._live(
			custom_carrier_request_snapshot=json.dumps(snapshot),
			**values,
		)

	def test_only_the_changed_fields_are_sent_and_recorded(self):
		note = self._editable()
		self.http.queue["update"].append(OK)

		editing.edit_shipment(note.name, "01111111111", "city-cairo", "12 Example Street")

		self.assertEqual(self.http.calls_of("update"), [{"receiver": {"phone": "01111111111"}}])
		log = frappe.get_all(
			"Integration Request",
			filters={"reference_docname": note.name, "custom_operation_type": "Edit"},
			fields=["custom_result"],
		)
		self.assertEqual([row.custom_result for row in log], ["Success"])
		values = stored(note, "contact_mobile", "custom_carrier_request_snapshot")
		self.assertEqual(values.contact_mobile, "01111111111")
		snapshot = json.loads(values.custom_carrier_request_snapshot)
		self.assertEqual(snapshot["receiver"]["phone"], "01111111111")
		self.assertEqual(snapshot["dropOffAddress"]["firstLine"], "12 Example Street")

	def test_a_changed_city_sends_the_whole_address_object(self):
		note = self._editable()
		self.http.queue["update"].append(OK)

		editing.edit_shipment(note.name, "01000000000", "city-giza", "12 Example Street")

		self.assertEqual(
			self.http.calls_of("update"),
			[{"dropOffAddress": {"city": "EG-25", "firstLine": "12 Example Street"}}],
		)
		self.assertEqual(stored(note, "custom_bosta_city").custom_bosta_city, "EG-25")

	def test_nothing_changed_sends_nothing(self):
		note = self._editable()
		with self.assertRaises(frappe.ValidationError):
			editing.edit_shipment(note.name, "01000000000", "city-cairo", "12 Example Street")
		self.assertEqual(self.http.sent(), [])

	def test_an_edit_outside_the_editable_states_is_refused_without_a_call(self):
		note = self._editable(custom_shipping_status=statuses.BOSTA_STATE_NAMES[45])
		with self.assertRaises(frappe.ValidationError):
			editing.edit_shipment(note.name, "01111111111", "city-cairo", "12 Example Street")
		self.assertEqual(self.http.sent(), [])

	def test_a_bosta_refusal_changes_nothing_in_erpnext(self):
		note = self._editable()
		self.http.queue["update"].append(FakeResponse(400, {"success": False, "message": "nope"}))

		with self.assertRaises(frappe.ValidationError):
			editing.edit_shipment(note.name, "01111111111", "city-cairo", "12 Example Street")

		self.assertEqual(stored(note, "contact_mobile").contact_mobile, "01000000000")

	def test_a_network_failure_changes_nothing_in_erpnext_and_logs_an_edit(self):
		note = self._editable()
		self.http.queue["update"].append(TimeoutError("slow"))
		before = stored(note, "contact_mobile", "custom_carrier_request_snapshot", "modified")

		with self.assertRaises(frappe.ValidationError):
			editing.edit_shipment(note.name, "01111111111", "city-cairo", "12 Example Street")

		self.assertEqual(stored(note, "contact_mobile", "custom_carrier_request_snapshot", "modified"), before)
		log = frappe.get_all(
			"Integration Request",
			filters={"reference_docname": note.name, "custom_operation_type": "Edit"},
			fields=["custom_result"],
		)
		self.assertEqual([row.custom_result for row in log], ["Unknown"])

	def test_the_edit_api_needs_write_permission(self):
		note = self._editable()
		email, _role = make_role_user(self, "Bosta Edit Reader", {"Delivery Note": ["read"]})
		frappe.set_user(email)
		with self.assertRaises(frappe.PermissionError):
			bosta_shipments.edit_shipment(note.name, "01111111111", "city-cairo", "12 Example Street")
		frappe.set_user("Administrator")
		self.assertEqual(self.http.sent(), [])

	# -- returns and costs --------------------------------------------------------------------

	def _submitted(self):
		note = self._live()
		note = frappe.get_doc("Delivery Note", note.name)
		note.submit()
		frappe.db.commit()
		return note

	def test_a_returned_shipment_gets_exactly_one_never_submitted_draft_return(self):
		note = self._submitted()

		first = returns.create_returned_draft(note.name)
		second = returns.create_returned_draft(note.name)

		self.assertTrue(first)
		self.assertIsNone(second)
		values = frappe.db.get_value(
			"Delivery Note",
			first,
			["docstatus", "is_return", "return_against", "custom_return_source", "lr_no"],
			as_dict=True,
		)
		self.assertEqual(values.docstatus, 0)
		self.assertEqual(values.is_return, 1)
		self.assertEqual(values.return_against, note.name)
		self.assertEqual(values.custom_return_source, "Returned by Bosta")
		self.assertFalse(values.lr_no)

	def test_the_draft_return_is_a_needs_attention_row_for_stock_users_until_it_is_submitted(self):
		note = self._submitted()
		draft = returns.create_returned_draft(note.name)

		items = attention._collect_bosta_items(attention.BOSTA_RETURN_REVIEW)
		mine = [i for i in items if i["reference_name"] == draft]
		self.assertEqual(len(mine), 1)
		self.assertEqual(mine[0]["role"], "Stock User")
		self.assertEqual(mine[0]["reference_doctype"], "Delivery Note")
		self.assertEqual(mine[0]["platform"], "Bosta")
		self.assertIn(note.name, mine[0]["explanation"])

		frappe.db.set_value("Delivery Note", draft, "docstatus", 1)
		items = attention._collect_bosta_items(attention.BOSTA_RETURN_REVIEW)
		self.assertEqual([i for i in items if i["reference_name"] == draft], [])
		frappe.db.set_value("Delivery Note", draft, "docstatus", 0)

	def test_a_draft_note_gets_no_return(self):
		note = self.make_note()
		self.assertIsNone(returns.create_returned_draft(note.name))

	def test_a_return_note_never_matches_a_webhook(self):
		tracking = f"TRK-{self.suffix}"
		note = self._submitted()
		frappe.db.set_value("Delivery Note", note.name, "lr_no", tracking)
		draft = returns.create_returned_draft(note.name)
		frappe.db.set_value("Delivery Note", draft, "lr_no", tracking)

		self.assertEqual(statuses._note_for("", tracking), note.name)

	def test_a_returned_event_queues_the_draft_once(self):
		note = self._submitted()
		statuses.process_webhook_event(
			{"_id": "DELIVERY-1", "trackingNumber": "TRK-1", "state": 46, "type": "SEND", "timeStamp": 9000}
		)
		key = frappe.get_all(
			"Integration Request", filters={"custom_delivery_id": "DELIVERY-1"}, pluck="name"
		)[0]
		statuses.apply_event(key)

		jobs = [c.kwargs.get("job_id") for c in self.enqueue.call_args_list]
		self.assertIn(f"bosta-returned-draft|{note.name}", jobs)
		frappe.db.delete("Integration Request", {"custom_delivery_id": "DELIVERY-1"})
		frappe.db.commit()

	def test_a_received_fee_is_stamped_and_a_real_zero_is_told_apart(self):
		with_zero = self._live()
		self.http.queue["get"].append(delivery(24, fee=0))
		statuses.refresh_note(with_zero.name)
		values = stored(with_zero, "custom_actual_shipping_cost", "custom_actual_cost_received_on")
		self.assertEqual(values.custom_actual_shipping_cost, 0)
		self.assertTrue(values.custom_actual_cost_received_on)

		without = self._live()
		self.http.queue["get"].append(delivery(24))
		statuses.refresh_note(without.name)
		self.assertFalse(stored(without, "custom_actual_cost_received_on").custom_actual_cost_received_on)
