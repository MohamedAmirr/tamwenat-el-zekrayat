# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Bosta states on the Delivery Note: webhook record/apply, Refresh, nightly reconciliation, Sales Order
counts (epic #413, #409). Real database; only the HTTP layer is faked and ``frappe.enqueue`` patched."""

import hashlib
import json
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.integrations.bosta import delivery_note_shipment as shipment
from marketplace_integrations.integrations.bosta import statuses
from marketplace_integrations.tests.bosta_fixtures import BostaFixtureMixin, FakeResponse, created


def event(note_tracking="TRK-1", state=24, ts=1000, delivery_id="DELIVERY-1", **extra):
	return {
		"_id": delivery_id,
		"trackingNumber": note_tracking,
		"state": state,
		"type": "SEND",
		"timeStamp": ts,
		**extra,
	}


def delivery_body(state, fee=None):
	data = {"state": {"code": state, "value": "x"}, "type": "SEND"}
	if fee is not None:
		data["shipmentFees"] = fee
	return FakeResponse(200, {"success": True, "data": data})


class IntegrationTestBostaStatuses(BostaFixtureMixin, IntegrationTestCase):
	def _created_note(self, order=None, tracking="TRK-1", delivery_id="DELIVERY-1"):
		note = self.make_note(order)
		frappe.db.set_value(
			"Delivery Note",
			note.name,
			{
				"custom_integration_status": "Created",
				"custom_carrier_delivery_id": delivery_id,
				"custom_carrier_reference": note.name,
				"lr_no": tracking,
			},
		)
		frappe.db.commit()
		return note

	def _apply(self, payload):
		statuses.process_webhook_event(payload)
		key = hashlib.sha256(
			f"{payload['trackingNumber']}|{payload['state']}|{payload['timeStamp']}".encode()
		).hexdigest()
		return statuses.apply_event(frappe.db.get_value("Integration Request", {"custom_dedupe_key": key}))

	def _clean_logs(self):
		frappe.db.delete(
			"Integration Request",
			{"custom_operation_type": "Webhook", "custom_delivery_id": ["like", "DELIVERY-%"]},
		)
		frappe.db.commit()

	def setUp(self):
		super().setUp()
		self._clean_logs()

	def tearDown(self):
		super().tearDown()
		self._clean_logs()

	# -- webhook -----------------------------------------------------------------------------

	def test_a_webhook_is_recorded_before_anything_is_applied_and_queued(self):
		note = self._created_note()

		self.assertEqual(statuses.process_webhook_event(event()), "queued")

		log = frappe.get_all("Integration Request", filters={"custom_delivery_id": "DELIVERY-1"}, fields=["*"])[0]
		self.assertEqual(log.custom_operation_type, "Webhook")
		self.assertEqual(log.is_remote_request, 1)
		self.assertEqual(
			set(json.loads(log.data)), {"_id", "trackingNumber", "state", "timeStamp", "type", "exceptionCode"}
		)
		self.assertEqual(self.enqueue.call_args.kwargs["job_id"], f"bosta-event|{log.name}")
		self.assertFalse(frappe.db.get_value("Delivery Note", note.name, "custom_shipping_status"))

	def test_the_same_event_twice_is_applied_once(self):
		self._created_note()

		self.assertEqual(statuses.process_webhook_event(event()), "queued")
		self.assertEqual(statuses.process_webhook_event(event()), "duplicate")

		self.assertEqual(self.enqueue.call_count, 1)
		self.assertEqual(frappe.db.count("Integration Request", {"custom_delivery_id": "DELIVERY-1"}), 1)

	def test_apply_writes_the_state_and_the_sales_order_counts(self):
		order = self.make_order()
		note = self._created_note(order)

		self.assertEqual(self._apply(event(state=24, ts=1000)), "applied")

		values = frappe.db.get_value(
			"Delivery Note", note.name, ["custom_shipping_status", "custom_bosta_last_event_at"], as_dict=True
		)
		self.assertEqual(values.custom_shipping_status, "Received at warehouse")
		self.assertEqual(values.custom_bosta_last_event_at, "1000")
		summary = frappe.db.get_value("Sales Order", order.name, "custom_shipping_status_summary")
		self.assertEqual(json.loads(summary), {"Received at warehouse": 1})

	def test_an_older_or_equal_event_is_ignored(self):
		note = self._created_note()
		self._apply(event(state=24, ts=2000))

		self.assertEqual(self._apply(event(state=41, ts=1500, delivery_id="DELIVERY-1")), "ignored")

		self.assertEqual(
			frappe.db.get_value("Delivery Note", note.name, "custom_shipping_status"), "Received at warehouse"
		)

	def test_an_event_for_an_unknown_delivery_is_logged_and_ignored(self):
		self.assertEqual(self._apply(event("NOPE", delivery_id="DELIVERY-NOPE")), "ignored")

	def test_a_delivery_matches_by_exact_tracking_number_not_a_case_variant(self):
		note = self._created_note(tracking="TRKabc", delivery_id="DELIVERY-A")

		self.assertEqual(self._apply(event("TRKABC", delivery_id="DELIVERY-B")), "ignored")
		self.assertFalse(frappe.db.get_value("Delivery Note", note.name, "custom_shipping_status"))

	def test_bosta_cancelling_a_created_note_marks_it_cancelled_and_leaves_docstatus(self):
		note = self._created_note()

		self._apply(event(state=49, ts=3000))

		values = frappe.db.get_value(
			"Delivery Note", note.name, ["custom_integration_status", "custom_shipping_status", "docstatus"], as_dict=True
		)
		self.assertEqual(values.custom_integration_status, "Cancelled")
		self.assertEqual(values.custom_shipping_status, "Canceled")
		self.assertEqual(values.docstatus, 0)

	def test_an_event_that_contradicts_the_note_asks_bosta_and_applies_its_answer(self):
		note = self.make_note()
		frappe.db.set_value(
			"Delivery Note",
			note.name,
			{"custom_integration_status": "Cancelled", "custom_carrier_delivery_id": "DELIVERY-1", "lr_no": "TRK-1"},
		)
		self.http.queue["get"].append(delivery_body(45))

		self._apply(event(state=45, ts=4000))

		self.assertEqual(len([c for c in self.http.calls if c[0] == "get"]), 1)
		self.assertEqual(frappe.db.get_value("Delivery Note", note.name, "custom_shipping_status"), "Delivered")

	def test_a_late_event_writes_nothing_even_when_it_was_read_before_a_newer_one_was_written(self):
		note = self._created_note()
		self._apply(event(state=24, ts=2000))
		before = frappe.db.get_value(
			"Delivery Note", note.name, ["custom_shipping_status", "custom_bosta_last_event_at", "modified"], as_dict=True
		)

		# The compare happens inside the row lock, in _write_state itself.
		outcome = statuses._write_state(note.name, 41, 1500, event=True)

		self.assertEqual(outcome, "older")
		after = frappe.db.get_value(
			"Delivery Note", note.name, ["custom_shipping_status", "custom_bosta_last_event_at", "modified"], as_dict=True
		)
		self.assertEqual(after, before)

	def test_events_arriving_out_of_order_leave_the_newest_state(self):
		note = self._created_note()

		for state, ts in ((45, 5000), (41, 3000), (24, 1000)):
			self._apply(event(state=state, ts=ts))

		values = frappe.db.get_value(
			"Delivery Note", note.name, ["custom_shipping_status", "custom_bosta_last_event_at"], as_dict=True
		)
		self.assertEqual(values.custom_shipping_status, "Delivered")
		self.assertEqual(values.custom_bosta_last_event_at, "5000")

	def test_an_event_for_a_note_still_creating_stays_pending_and_is_applied_after_created(self):
		note = self.make_note()
		frappe.db.set_value("Delivery Note", note.name, "custom_integration_status", "Creating")
		frappe.db.commit()

		self.assertEqual(self._apply(event(state=24, ts=1000)), "pending")

		log = frappe.get_value("Integration Request", {"custom_delivery_id": "DELIVERY-1"}, ["name", "status"], as_dict=True)
		self.assertEqual(log.status, "Queued")
		self.assertFalse(frappe.db.get_value("Delivery Note", note.name, "custom_shipping_status"))

		# The worker writes Created, then replays what arrived meanwhile.
		frappe.db.set_value(
			"Delivery Note",
			note.name,
			{"custom_integration_status": "Created", "custom_carrier_delivery_id": "DELIVERY-1", "lr_no": "TRK-1"},
		)
		frappe.db.commit()
		shipment.after_created(note.name, {"_id": "DELIVERY-1"})

		self.assertEqual(
			frappe.db.get_value("Delivery Note", note.name, "custom_shipping_status"), "Received at warehouse"
		)
		self.assertEqual(frappe.db.get_value("Integration Request", log.name, "status"), "Completed")

	def test_a_matched_event_for_a_note_creating_stays_pending(self):
		note = self.make_note()
		frappe.db.set_value(
			"Delivery Note",
			note.name,
			{"custom_integration_status": "Creating", "custom_carrier_delivery_id": "DELIVERY-1", "lr_no": "TRK-1"},
		)
		frappe.db.commit()

		self.assertEqual(self._apply(event(state=24, ts=1000)), "pending")

		self.assertEqual(
			frappe.db.get_value("Integration Request", {"custom_delivery_id": "DELIVERY-1"}, "status"), "Queued"
		)

	def test_the_nightly_job_applies_a_pending_event_and_expires_an_old_one(self):
		note = self._created_note()
		keep = self.make_note()
		frappe.db.set_value("Delivery Note", keep.name, "custom_integration_status", "Creating")
		frappe.db.commit()
		self._apply(event("TRK-OLD", state=24, ts=10, delivery_id="DELIVERY-OLD"))
		old = frappe.db.get_value("Integration Request", {"custom_delivery_id": "DELIVERY-OLD"}, "name")
		frappe.db.set_value("Integration Request", old, "creation", frappe.utils.add_days(frappe.utils.now_datetime(), -4), update_modified=False)
		# An event of the Created note that was left pending by an earlier failure of the apply job.
		statuses.process_webhook_event(event(state=41, ts=2000))
		frappe.db.commit()

		statuses.run_reconcile()

		self.assertEqual(frappe.db.get_value("Delivery Note", note.name, "custom_shipping_status"), "Picked up")
		row = frappe.db.get_value("Integration Request", old, ["status", "output"], as_dict=True)
		self.assertEqual(row.status, "Completed")
		self.assertIn("ignored", row.output)

	def test_a_conflict_whose_refresh_fails_is_a_failed_event_not_a_successful_one(self):
		note = self.make_note()
		frappe.db.set_value(
			"Delivery Note",
			note.name,
			{"custom_integration_status": "Cancelled", "custom_carrier_delivery_id": "DELIVERY-1", "lr_no": "TRK-1"},
		)
		self.http.queue["get"].append(FakeResponse(500, {"success": False, "message": "down"}))

		self.assertEqual(self._apply(event(state=45, ts=4000)), "failed")

		log = frappe.db.get_value(
			"Integration Request", {"custom_delivery_id": "DELIVERY-1"}, ["custom_result", "status", "error"], as_dict=True
		)
		self.assertEqual(log.custom_result, "Failed")
		self.assertEqual(log.status, "Failed")
		self.assertIn("conflict", log.error)
		self.assertFalse(frappe.db.get_value("Delivery Note", note.name, "custom_shipping_status"))

	def test_a_rate_limited_conflict_refresh_is_also_a_failed_event(self):
		note = self.make_note()
		frappe.db.set_value(
			"Delivery Note",
			note.name,
			{"custom_integration_status": "Cancelled", "custom_carrier_delivery_id": "DELIVERY-1", "lr_no": "TRK-1"},
		)
		self.http.queue["get"].append(FakeResponse(429, {"success": False}))

		self.assertEqual(self._apply(event(state=45, ts=4000)), "failed")

		self.assertEqual(
			frappe.db.get_value("Integration Request", {"custom_delivery_id": "DELIVERY-1"}, "custom_result"), "Failed"
		)

	def test_malformed_events_are_an_error(self):
		for payload in (None, {}, event(state="x"), event(ts=None)):
			with self.subTest(payload=payload), self.assertRaises(frappe.ValidationError):
				statuses.process_webhook_event(payload)

	# -- refresh -----------------------------------------------------------------------------

	def test_refresh_reads_the_state_and_the_fee_after_the_http_call(self):
		note = self._created_note()
		self.http.queue["get"].append(delivery_body(41, fee=55.5))

		state = statuses.refresh_note(note.name)

		self.assertEqual(state, "Picked up")
		values = frappe.db.get_value(
			"Delivery Note", note.name, ["custom_shipping_status", "custom_actual_shipping_cost", "custom_last_sync_at"], as_dict=True
		)
		self.assertEqual(values.custom_actual_shipping_cost, 55.5)
		self.assertTrue(values.custom_last_sync_at)

	def test_refresh_stops_on_rate_limit_without_writing(self):
		note = self._created_note()
		self.http.queue["get"].append(FakeResponse(429, {"success": False}))

		with self.assertRaises(statuses.RateLimited):
			statuses.refresh_note(note.name)

		self.assertFalse(frappe.db.get_value("Delivery Note", note.name, "custom_shipping_status"))

	def _refresh_log(self, note):
		self.addCleanup(
			lambda: (
				frappe.db.delete("Integration Request", {"reference_docname": note.name, "custom_operation_type": "Refresh"}),
				frappe.db.commit(),
			)
		)
		return frappe.db.get_value(
			"Integration Request",
			{"reference_docname": note.name, "custom_operation_type": "Refresh"},
			["custom_result", "error", "custom_http_status"],
			as_dict=True,
		)

	def test_refresh_of_an_already_cancelled_note_reading_not_found_is_a_quiet_success(self):
		note = self._created_note()
		frappe.db.set_value("Delivery Note", note.name, "custom_integration_status", "Cancelled")
		self.http.queue["get"].append(FakeResponse(400, {"success": False, "message": "Delivery not found.", "errorCode": 1066}))

		with patch(
			"marketplace_integrations.services.carrier_shipments.cancel_after_carrier_cancelled"
		) as auto:
			state = statuses.refresh_note(note.name)

		self.assertEqual(state, "Terminated")
		auto.assert_not_called()
		self.assertEqual(frappe.db.get_value("Delivery Note", note.name, "custom_integration_status"), "Cancelled")
		self.assertEqual(self._refresh_log(note).custom_result, "Success")

	def test_a_refresh_failure_shows_a_plain_sentence_and_keeps_bostas_text_in_the_log(self):
		note = self._created_note()
		self.http.queue["get"].append(FakeResponse(400, {"success": False, "message": "Something odd", "errorCode": 1234}))

		with self.assertRaises(frappe.ValidationError) as caught:
			statuses.refresh_note(note.name)

		self.assertIn("could not return this shipment's status", str(caught.exception))
		self.assertNotIn("errorCode", str(caught.exception))
		log = self._refresh_log(note)
		self.assertEqual(log.custom_result, "Failed")
		self.assertIn("Something odd", log.error)

	def test_refresh_of_a_note_without_a_tracking_number_says_so(self):
		note = self._created_note(tracking="")

		with self.assertRaises(frappe.ValidationError) as caught:
			shipment.refresh(note.name)

		self.assertIn("no Bosta tracking number", str(caught.exception))
		self.assertEqual(self.http.sent(), [])

	# -- nightly reconciliation --------------------------------------------------------------

	def test_the_nightly_job_resolves_an_unknown_create_by_lookup_and_never_creates(self):
		note = self.make_note()
		frappe.db.set_value("Delivery Note", note.name, "custom_integration_status", "Creation Unknown")
		body = {
			"success": True,
			"data": {"count": 1, "deliveries": [{"_id": "D-7", "trackingNumber": "T-7", "businessReference": note.name}]},
		}
		self.http.queue["search"].append(FakeResponse(200, body))

		statuses.run_reconcile()

		self.assertEqual(frappe.db.get_value("Delivery Note", note.name, "custom_integration_status"), "Created")
		# The run polls every open note on the site, so a Created note an older run left behind is
		# read too (a GET of its own tracking number). This note is only searched, never created or read.
		kinds = [
			kind
			for kind, url in self.http.urls
			if not url.endswith(("/cities", "/getAllDistricts")) and (kind != "get" or url.endswith("/T-7"))
		]
		self.assertEqual(kinds, ["search"])

	def test_one_failing_note_does_not_stop_the_run(self):
		first = self._created_note(tracking="TRK-A", delivery_id="DELIVERY-A")
		second = self._created_note(tracking="TRK-B", delivery_id="DELIVERY-B")
		self.http.queue["get"] += [FakeResponse(500, {"success": False}), delivery_body(45)]

		statuses.run_reconcile()

		states = {
			frappe.db.get_value("Delivery Note", n.name, "custom_shipping_status") for n in (first, second)
		}
		self.assertIn("Delivered", states)

	def test_a_rate_limit_stops_the_run_early(self):
		self._created_note(tracking="TRK-A", delivery_id="DELIVERY-A")
		self._created_note(tracking="TRK-B", delivery_id="DELIVERY-B")
		self.http.queue["get"] += [FakeResponse(429, {"success": False}), delivery_body(45)]

		statuses.run_reconcile()

		self.assertEqual(len(self.http.calls_of("get")), 1)

	def test_delivered_and_canceled_notes_are_not_polled_again(self):
		note = self._created_note()
		frappe.db.set_value("Delivery Note", note.name, "custom_shipping_status", "Delivered")

		statuses.run_reconcile()

		# The run polls every open note on the site, so a Created note an older run left behind is
		# read too; only this note's tracking number must stay unread.
		self.assertEqual([url for url in self.http.urls_of("get") if url.endswith("/TRK-1")], [])
		self.assertNotIn(note.name, statuses._open_notes())

	def test_the_nightly_entry_is_registered_and_the_half_hour_poll_is_gone(self):
		hooks = frappe.get_hooks("scheduler_events")
		self.assertIn(
			"marketplace_integrations.services.carrier_shipments.reconcile_open_notes", hooks["cron"]["59 23 * * *"]
		)
		for jobs in hooks["cron"].values():
			self.assertNotIn("marketplace_integrations.integrations.bosta.statuses.poll_incomplete_shipments", jobs)
		self.assertNotIn("*/30 * * * *", hooks["cron"])

	def test_a_status_write_to_a_draft_note_does_not_change_modified_so_an_open_form_can_still_submit(self):
		note = self._created_note()
		stale = frappe.get_doc("Delivery Note", note.name)
		before = frappe.db.get_value("Delivery Note", note.name, "modified")

		self.assertEqual(self._apply(event(state=24, ts=2000)), "applied")

		self.assertEqual(frappe.db.get_value("Delivery Note", note.name, "modified"), before)
		stale.submit()
		values = frappe.db.get_value("Delivery Note", note.name, ["docstatus", "custom_shipping_status"], as_dict=True)
		self.assertEqual(values.docstatus, 1)
		self.assertTrue(values.custom_shipping_status)

	def test_an_older_form_copy_cannot_overwrite_the_sales_order_summary(self):
		order = self.make_order(submit=False)
		frappe.db.set_value("Sales Order", order.name, "custom_shipping_status_summary", None)
		stale = frappe.get_doc("Sales Order", order.name)
		frappe.db.set_value(
			"Sales Order", order.name, "custom_shipping_status_summary", '{"Delivered": 1}', update_modified=False
		)
		stale.custom_shipping_status_summary = '{"Old": 9}'

		stale.save()

		self.assertEqual(
			frappe.db.get_value("Sales Order", order.name, "custom_shipping_status_summary"), '{"Delivered": 1}'
		)

	def test_the_summary_guard_is_registered_for_both_sales_order_saves(self):
		events = frappe.get_hooks("doc_events")["Sales Order"]
		path = "marketplace_integrations.integrations.bosta.statuses.keep_stored_summary"
		for name in ("validate", "before_update_after_submit"):
			self.assertIn(path, [events[name]] if isinstance(events[name], str) else events[name])

	# -- summary -----------------------------------------------------------------------------

	def test_two_delivered_and_one_in_transit_note_are_counted(self):
		order = self.make_order(qty=3)
		notes = [self._created_note(order, f"T{i}", f"DELIVERY-{i}") for i in range(3)]
		for note, state in zip(notes, ("Delivered", "Delivered", "In transit between Hubs"), strict=True):
			frappe.db.set_value("Delivery Note", note.name, "custom_shipping_status", state)

		statuses.update_sales_order_summary(order.name)

		summary = frappe.db.get_value("Sales Order", order.name, "custom_shipping_status_summary")
		self.assertEqual(json.loads(summary), {"Delivered": 2, "In transit between Hubs": 1})

	def test_the_summary_counts_only_bosta_notes(self):
		order = self.make_order(qty=2)
		bosta_note = self._created_note(order, "T1", "DELIVERY-1")
		other = self._created_note(order, "T2", "DELIVERY-2")
		frappe.db.set_value("Delivery Note", bosta_note.name, "custom_shipping_status", "Delivered")
		frappe.db.set_value("Delivery Note", other.name, {"custom_shipping_status": "Delivered", "transporter": None})

		statuses.update_sales_order_summary(order.name)

		summary = frappe.db.get_value("Sales Order", order.name, "custom_shipping_status_summary")
		self.assertEqual(json.loads(summary), {"Delivered": 1})

	def test_the_summary_commits_then_locks_the_sales_order_before_counting(self):
		order = self.make_order()
		self._created_note(order)
		events = []
		real_get_value = frappe.db.get_value
		real_commit = frappe.db.commit

		def spy_get_value(*args, **kwargs):
			if kwargs.get("for_update"):
				events.append(("lock", args[0], args[1]))
			return real_get_value(*args, **kwargs)

		def spy_commit(*args, **kwargs):
			events.append(("commit",))
			return real_commit(*args, **kwargs)

		with patch.object(frappe.db, "get_value", side_effect=spy_get_value), patch.object(
			frappe.db, "commit", side_effect=spy_commit
		):
			statuses.update_sales_order_summary(order.name)

		self.assertEqual(events[:2], [("commit",), ("lock", "Sales Order", order.name)])

	# -- Bosta jobs act on Bosta notes only --------------------------------------------------

	def _other_carrier_note(self, status, tracking="NOON-AWB-1", shipping_status="Shipped", **values):
		"""A note of another carrier (Noon-like): its own AWB in ``lr_no``, a status Bosta does not know."""
		other = self._track(
			frappe.get_doc(
				{
					"doctype": "Supplier",
					"supplier_name": f"Noon Test {frappe.generate_hash(length=6)}",
					"supplier_group": "All Supplier Groups",
					"supplier_type": "Company",
					"is_transporter": 1,
				}
			).insert()
		)
		note = self.make_note()
		frappe.db.set_value(
			"Delivery Note",
			note.name,
			{
				"transporter": other.name,
				"custom_integration_status": status,
				"custom_shipping_status": shipping_status,
				"lr_no": tracking,
				**values,
			},
		)
		frappe.db.commit()
		return note

	def test_open_notes_exclude_another_carriers_note_in_every_status_bucket(self):
		bosta = {
			"creating": self.make_note(),
			"unknown": self.make_note(),
			"created": self._created_note(),
			"cancelled": self.make_note(),
		}
		frappe.db.set_value("Delivery Note", bosta["creating"].name, "custom_integration_status", "Creating")
		frappe.db.set_value(
			"Delivery Note", bosta["unknown"].name, "custom_integration_status", "Creation Unknown"
		)
		frappe.db.set_value(
			"Delivery Note",
			bosta["cancelled"].name,
			{"custom_integration_status": "Cancelled", "custom_last_integration_error": "blocked"},
		)
		other = [
			self._other_carrier_note("Creating", "N-1"),
			self._other_carrier_note("Creation Unknown", "N-2"),
			self._other_carrier_note("Cancellation Pending", "N-3"),
			self._other_carrier_note("Cancellation Unknown", "N-4"),
			self._other_carrier_note("Created", "N-5"),
			self._other_carrier_note("Cancelled", "N-6", custom_last_integration_error="blocked"),
		]
		# Marked submitted for the query only and never committed: the fixture teardown rolls it back.
		for name in (bosta["cancelled"].name, other[-1].name):
			frappe.db.set_value("Delivery Note", name, "docstatus", 1)

		open_notes = statuses._open_notes()

		for note in bosta.values():
			self.assertIn(note.name, open_notes)
		for note in other:
			self.assertNotIn(note.name, open_notes)

	def test_open_notes_are_empty_when_there_is_no_bosta_supplier(self):
		self._created_note()

		with patch.object(shipment, "bosta_supplier", return_value=None):
			self.assertEqual(statuses._open_notes(), [])

	def test_refresh_of_another_carriers_note_makes_no_bosta_call_and_writes_nothing(self):
		note = self._other_carrier_note("Created")
		before = frappe.db.get_value(
			"Delivery Note",
			note.name,
			["custom_integration_status", "custom_shipping_status", "custom_last_integration_error"],
			as_dict=True,
		)
		self.http.queue["get"].append(
			FakeResponse(400, {"success": False, "message": "Delivery not found.", "errorCode": 1066})
		)

		with patch.object(statuses, "_write_state") as write:
			self.assertIsNone(statuses.refresh_note(note.name))
			self.assertIsNone(statuses.refresh_note(note.name, held=[]))

		write.assert_not_called()
		self.assertEqual(self.http.urls_of("get"), [])
		self.assertEqual(
			frappe.db.get_value(
				"Delivery Note",
				note.name,
				["custom_integration_status", "custom_shipping_status", "custom_last_integration_error"],
				as_dict=True,
			),
			before,
		)

	def test_refresh_of_another_carriers_cancelled_note_with_an_error_does_not_cancel_it(self):
		note = self._other_carrier_note("Cancelled", custom_last_integration_error="blocked")
		frappe.db.set_value("Delivery Note", note.name, "docstatus", 1)

		with patch(
			"marketplace_integrations.services.carrier_shipments.cancel_after_carrier_cancelled"
		) as auto:
			self.assertIsNone(statuses.refresh_note(note.name))

		auto.assert_not_called()

	def test_a_webhook_never_matches_another_carriers_note(self):
		note = self._other_carrier_note("Created", "SHARED-TRK", custom_carrier_delivery_id="DELIVERY-9")

		self.assertIsNone(statuses._note_for("DELIVERY-9", "SHARED-TRK"))
		self.assertEqual(frappe.db.get_value("Delivery Note", note.name, "custom_shipping_status"), "Shipped")

	def test_the_nightly_run_touches_only_bosta_notes(self):
		bosta = self._created_note(tracking="TRK-B", delivery_id="DELIVERY-B")
		noons = [self._other_carrier_note("Created", f"NOON-{i}") for i in range(5)]
		self.http.queue["get"].append(delivery_body(41))

		with patch.object(statuses.carrier_shipments, "check") as check:
			statuses.run_reconcile()

		check.assert_not_called()
		reads = [url for url in self.http.urls_of("get") if "NOON-" in url]
		self.assertEqual(reads, [])
		self.assertEqual(
			frappe.db.get_value("Delivery Note", bosta.name, "custom_shipping_status"), "Picked up"
		)
		for note in noons:
			values = frappe.db.get_value(
				"Delivery Note",
				note.name,
				["custom_integration_status", "custom_shipping_status", "custom_last_integration_error"],
				as_dict=True,
			)
			self.assertEqual(values.custom_integration_status, "Created")
			self.assertEqual(values.custom_shipping_status, "Shipped")
			self.assertFalse(values.custom_last_integration_error)

	def test_the_nightly_run_never_writes_state_onto_another_carriers_note(self):
		noons = [self._other_carrier_note("Created", f"NOON-{i}") for i in range(2)]
		not_found = FakeResponse(400, {"message": "Delivery not found.", "errorCode": 1066})
		self.http.queue["get"] += [not_found] * 10

		with patch.object(statuses, "_write_state") as write:
			statuses.run_reconcile()

		for call in write.call_args_list:
			self.assertNotIn(call.args[0], [note.name for note in noons])

	def test_a_not_found_answer_for_bosta_notes_still_follows_the_mass_limit_with_other_carriers_present(
		self,
	):
		for i in range(2):
			self._other_carrier_note("Created", f"NOON-{i}")
		# The test site can hold open Bosta notes committed by earlier runs: fill only the room left under the limit.
		room = statuses.MASS_1066_LIMIT - len(statuses._open_notes())
		few = [self._created_note(tracking=f"TRK-F{i}", delivery_id=f"DELIVERY-F{i}") for i in range(room)]
		self.assertTrue(few)
		not_found = FakeResponse(400, {"message": "Delivery not found.", "errorCode": 1066})
		self.http.queue["get"] += [not_found] * 50

		with patch.object(statuses.carrier_shipments, "cancel_after_carrier_cancelled"):
			statuses.run_reconcile()

		for note in few:
			self.assertEqual(
				frappe.db.get_value("Delivery Note", note.name, "custom_integration_status"), "Cancelled"
			)

		many = [
			self._created_note(tracking=f"TRK-M{i}", delivery_id=f"DELIVERY-M{i}")
			for i in range(statuses.MASS_1066_LIMIT + 1)
		]
		with patch("marketplace_integrations.services.carrier_shipments.notify_stock_managers"):
			statuses.run_reconcile()

		for note in many:
			self.assertEqual(
				frappe.db.get_value("Delivery Note", note.name, "custom_integration_status"), "Created"
			)
		self.assertTrue(
			any(frappe.db.get_value("Delivery Note", n.name, "custom_last_integration_error") for n in many)
		)
