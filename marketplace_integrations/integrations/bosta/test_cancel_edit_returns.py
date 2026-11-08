# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Cancel at Bosta, edit a created delivery, the draft return of a returned shipment and the cost receipt
stamp (epic #413, #410 / #411 / #412). Real database; only the HTTP layer is faked
(``tests/bosta_fixtures.py``) and ``frappe.enqueue`` is patched."""

import json
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_to_date, now_datetime

from marketplace_integrations.api import bosta_shipments
from marketplace_integrations.api import carrier_shipments as carrier_api
from marketplace_integrations.integrations.bosta import delivery_note_shipment as shipment
from marketplace_integrations.integrations.bosta import editing, returns, statuses
from marketplace_integrations.services import carrier_shipments
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

		result = carrier_shipments.cancel(note.name)

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

		result = carrier_shipments.cancel(note.name)

		self.assertEqual(result["outcome"], "cancelled")
		self.assertEqual(stored(note, "custom_integration_status").custom_integration_status, "Cancelled")
		self.assertEqual(self._sent_kinds(), ["terminate", "get"])

	def test_a_refusal_with_a_live_state_goes_back_to_created_with_the_raw_error(self):
		note = self._live()
		self.http.queue["terminate"].append(FakeResponse(400, {"success": False, "message": "bad <b>x</b>"}))
		self.http.queue["get"].append(delivery(24))

		result = carrier_shipments.cancel(note.name)

		self.assertEqual(result["outcome"], "refused")
		values = stored(note, "custom_integration_status", "custom_last_integration_error")
		self.assertEqual(values.custom_integration_status, "Created")
		self.assertIn("<b>x</b>", values.custom_last_integration_error)

	def test_a_timeout_and_a_5xx_are_cancellation_unknown_without_a_read(self):
		for response in (TimeoutError("slow"), FakeResponse(503, {"success": False})):
			with self.subTest(str(response)):
				note = self._live()
				self.http.queue["terminate"].append(response)

				result = carrier_shipments.cancel(note.name)

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
				result = carrier_shipments.cancel(cancelled.name)
				self.assertEqual(result["outcome"], "cancelled")
				self.assertEqual(
					stored(cancelled, "custom_integration_status").custom_integration_status, "Cancelled"
				)

				live = self._live()
				self.http.queue["terminate"].append(answer)
				self.http.queue["get"].append(delivery(24))
				result = carrier_shipments.cancel(live.name)
				self.assertEqual(result["outcome"], "refused")
				values = stored(live, "custom_integration_status", "custom_last_integration_error")
				self.assertEqual(values.custom_integration_status, "Created")
				self.assertIn("Bosta request failed", values.custom_last_integration_error)

				missing = self._live()
				self.http.queue["terminate"].append(answer)
				self.http.queue["get"].append(FakeResponse(404, {}))
				self.assertEqual(carrier_shipments.cancel(missing.name)["outcome"], "unknown")
				self.assertEqual(
					stored(missing, "custom_integration_status").custom_integration_status,
					"Cancellation Unknown",
				)

				failed = self._live()
				self.http.queue["terminate"].append(answer)
				self.http.queue["get"].append(TimeoutError("slow"))
				self.assertEqual(carrier_shipments.cancel(failed.name)["outcome"], "unknown")
				self.assertEqual(
					stored(failed, "custom_integration_status").custom_integration_status,
					"Cancellation Unknown",
				)

	def test_an_unclear_answer_makes_exactly_one_read(self):
		note = self._live()
		self.http.queue["terminate"].append(FakeResponse(404, {}))
		self.http.queue["get"].append(delivery(24))

		carrier_shipments.cancel(note.name)

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

	def _submit(self, note):
		doc = frappe.get_doc("Delivery Note", note.name)
		doc.submit()
		frappe.db.commit()
		return doc

	def test_a_submitted_note_is_cancelled_after_the_carrier_cancel_and_the_guard_does_not_block_it(self):
		note = self._live()
		self._submit(note)
		self.http.queue["terminate"].append(OK)

		carrier_shipments.cancel(note.name)
		doc = frappe.get_doc("Delivery Note", note.name)
		doc.cancel()

		values = stored(note, "docstatus", "custom_integration_status")
		self.assertEqual((values.docstatus, values.custom_integration_status), (2, "Cancelled"))

	def test_a_submitted_note_resolved_by_delivery_not_found_is_cancelled_automatically(self):
		note = self._live(custom_integration_status="Cancellation Unknown")
		self._submit_unknown(note)
		self.http.queue["get"].append(FakeResponse(400, {"message": "Delivery not found.", "errorCode": 1066}))

		statuses.refresh_note(note.name)

		# The native cancel is a queued job (cancel_after_carrier_cancelled); run it as the worker would.
		jobs = [c for c in self.enqueue.call_args_list if "run_auto_cancel" in str(c)]
		self.assertEqual(len(jobs), 1)
		carrier_shipments.run_auto_cancel(note.name)

		self.assertEqual(stored(note, "docstatus").docstatus, 2)
		self.assertEqual(self._sent_kinds(), ["get"])

	# -- automatic native cancel after the carrier cancelled -----------------------------------

	_NOTIFY = "frappe.desk.doctype.notification_log.notification_log.enqueue_create_notification"

	def test_a_carrier_cancel_found_by_a_refresh_cancels_the_submitted_note_and_notifies_once(self):
		from unittest.mock import patch

		note = self._live()
		self._submit(note)
		manager = self.make_user("Stock Manager")
		self.http.queue["get"].append(delivery(48))

		with patch(self._NOTIFY) as notify, patch.object(frappe, "enqueue") as enqueue:
			statuses.refresh_note(note.name)
			# the native cancel is a short job (it runs as the system), queued once per note
			jobs = [c for c in enqueue.call_args_list if "run_auto_cancel" in str(c)]
			self.assertEqual(len(jobs), 1)
			self.assertEqual(jobs[0].kwargs["delivery_note"], note.name)
			self.assertTrue(jobs[0].kwargs["enqueue_after_commit"])
			carrier_shipments.run_auto_cancel(note.name)
			carrier_shipments.run_auto_cancel(note.name)  # idempotent: a cancelled note is left alone

		values = stored(note, "docstatus", "custom_integration_status")
		self.assertEqual((values.docstatus, values.custom_integration_status), (2, "Cancelled"))
		self.assertEqual(self._sent_kinds(), ["get"], "the carrier is never called to cancel")
		self.assertEqual(notify.call_count, 1)
		users, info = notify.call_args.args
		self.assertEqual((info["document_type"], info["document_name"]), ("Delivery Note", note.name))
		self.assertEqual(len(users), len(set(users)))
		self.assertIn(manager, users)
		self.assertIn("Administrator", users)  # the creator submitted it

	def test_a_blocked_cancel_keeps_the_note_submitted_records_the_reason_and_notifies_once(self):
		from unittest.mock import patch

		note = self._live()
		self._submit(note)

		with (
			patch.object(
				carrier_shipments, "_check_native_cancel_not_blocked", side_effect=frappe.ValidationError("Linked <b>SINV-1</b>")
			),
			patch(self._NOTIFY) as notify,
		):
			frappe.db.set_value("Delivery Note", note.name, "custom_integration_status", "Cancelled")
			carrier_shipments.run_auto_cancel(note.name)
			carrier_shipments.run_auto_cancel(note.name)  # the same reason: no second notification

		values = stored(note, "docstatus", "custom_integration_status", "custom_last_integration_error")
		self.assertEqual((values.docstatus, values.custom_integration_status), (1, "Cancelled"))
		self.assertIn("SINV-1", values.custom_last_integration_error)
		self.assertNotIn("<b>", values.custom_last_integration_error)
		self.assertEqual(notify.call_count, 1)
		self.assertIn("by hand", notify.call_args.args[1]["subject"])
		rows = attention._collect_carrier_items(attention.CARRIER_EXCEPTION)
		self.assertIn(note.name, [row.get("reference_name") or row.get("name") for row in rows])

	def test_only_a_confirmed_cancelled_note_is_cancelled(self):
		from unittest.mock import patch

		for status in ("Created", "Cancellation Pending", "Cancellation Unknown", "Creation Unknown"):
			with self.subTest(status):
				note = self._live()
				self._submit(note)
				frappe.db.set_value("Delivery Note", note.name, "custom_integration_status", status)
				with patch(self._NOTIFY) as notify:
					carrier_shipments.run_auto_cancel(note.name)
				self.assertEqual(stored(note, "docstatus").docstatus, 1)
				notify.assert_not_called()

	def test_a_draft_note_is_not_cancelled_by_the_carrier_cancel(self):
		note = self._live(custom_integration_status="Cancelled")
		carrier_shipments.run_auto_cancel(note.name)
		self.assertEqual(stored(note, "docstatus").docstatus, 0)

	def _invoice_of(self, note):
		from erpnext.stock.doctype.delivery_note.delivery_note import make_sales_invoice

		invoice = make_sales_invoice(note.name)
		invoice.insert()
		invoice.submit()
		frappe.db.commit()
		return invoice

	def _drop_invoice(self, invoice, *payments, journals=()):
		"""Tear-down for the tests that bill a note: the invoice (and its payments and journals) block deleting
		the note. Removes everything they leave behind: ledger rows, Version and Comment rows."""
		documents = [
			*[("Payment Entry", p) for p in payments],
			*[("Journal Entry", j) for j in journals],
			("Sales Invoice", invoice.name),
		]
		for doctype, name in documents:
			if frappe.db.get_value(doctype, name, "docstatus") == 1:
				frappe.get_doc(doctype, name).cancel()
			frappe.delete_doc(doctype, name, force=True, ignore_permissions=True, delete_permanently=True)
		for doctype, name in documents:
			frappe.db.delete("GL Entry", {"voucher_no": name})
			frappe.db.delete("Payment Ledger Entry", {"voucher_no": name})
			frappe.db.delete("Payment Ledger Entry", {"against_voucher_no": name})
			frappe.db.delete("Version", {"ref_doctype": doctype, "docname": name})
			frappe.db.delete("Comment", {"reference_doctype": doctype, "reference_name": name})
		# delete_doc also leaves a nameless "Deleted" comment per document type.
		for doctype in {doctype for doctype, _ in documents}:
			filters = {"comment_type": "Deleted", "reference_doctype": doctype}
			if self._error_log_floor:
				filters["creation"] = [">=", self._error_log_floor]
			frappe.db.delete("Comment", filters)
		frappe.db.commit()

	def _run_cancel_refused(self, note, invoice, *needles):
		"""The automatic cancel is refused: nothing is cancelled, the reason is stored and named."""
		frappe.db.set_value("Delivery Note", note.name, "custom_integration_status", "Cancelled")
		with patch(self._NOTIFY) as notify:
			carrier_shipments.run_auto_cancel(note.name)

		values = stored(note, "docstatus", "custom_last_integration_error")
		self.assertEqual(values.docstatus, 1)
		self.assertEqual(frappe.db.get_value("Sales Invoice", invoice.name, "docstatus"), 1)
		for needle in needles:
			self.assertIn(needle, values.custom_last_integration_error)
		self.assertEqual(notify.call_count, 1)

	def test_a_carrier_cancelled_note_cancels_its_submitted_sales_invoice_first_without_a_carrier_call(self):
		note = self._live()
		self._submit(note)
		invoice = self._invoice_of(note)
		frappe.db.set_value("Delivery Note", note.name, "custom_integration_status", "Cancelled")
		try:
			with patch(self._NOTIFY) as notify:
				carrier_shipments.run_auto_cancel(note.name)

			self.assertEqual(stored(note, "docstatus").docstatus, 2)
			self.assertEqual(frappe.db.get_value("Sales Invoice", invoice.name, "docstatus"), 2)
			self.assertEqual(self._sent_kinds(), [], "the carrier is never called")
			# accounts are told: a comment on the invoice and the invoice named in the notification
			comments = frappe.get_all(
				"Comment",
				filters={"reference_doctype": "Sales Invoice", "reference_name": invoice.name, "comment_type": "Comment"},
				pluck="content",
			)
			self.assertEqual(len(comments), 1)
			self.assertIn(note.name, comments[0])
			self.assertIn(invoice.name, notify.call_args.args[1]["subject"])
		finally:
			self._drop_invoice(invoice)

	def test_a_manual_cancel_of_a_note_with_a_linked_invoice_is_still_refused(self):
		note = self._live()
		self._submit(note)
		invoice = self._invoice_of(note)
		try:
			with self.assertRaises(frappe.ValidationError):
				frappe.get_doc("Delivery Note", note.name).cancel()
			frappe.db.rollback()
			self.assertEqual(frappe.db.get_value("Sales Invoice", invoice.name, "docstatus"), 1)
			self.assertEqual(stored(note, "docstatus").docstatus, 1)
		finally:
			self._drop_invoice(invoice)

	def test_the_credit_note_of_a_return_pickup_is_cancelled_with_the_return_note(self):
		from erpnext.controllers.sales_and_purchase_return import make_return_doc

		original = self._live()
		self._submit(original)
		ret = make_return_doc("Delivery Note", original.name)
		ret.custom_return_source = shipment.RETURN_PICKUP
		ret.transporter = self.supplier
		ret.custom_cod_confirmed = 1
		ret.insert()
		ret.submit()
		frappe.db.set_value(
			"Delivery Note",
			ret.name,
			{"custom_integration_status": "Cancelled", "custom_carrier_delivery_id": "DELIVERY-2"},
		)
		frappe.db.commit()
		credit_note = self._invoice_of(ret)
		self.assertEqual(frappe.db.get_value("Sales Invoice", credit_note.name, "is_return"), 1)
		try:
			with patch(self._NOTIFY) as notify:
				carrier_shipments.run_auto_cancel(ret.name)

			self.assertEqual(frappe.db.get_value("Delivery Note", ret.name, "docstatus"), 2)
			self.assertEqual(frappe.db.get_value("Sales Invoice", credit_note.name, "docstatus"), 2)
			self.assertIn(credit_note.name, notify.call_args.args[1]["subject"])
		finally:
			self._drop_invoice(credit_note)

	def test_an_invoice_that_also_bills_another_note_or_a_line_without_one_is_refused(self):
		note = self._live()
		self._submit(note)
		other = self.make_note()
		invoice = self._invoice_of(note)
		try:
			for value in (other.name, None):
				with self.subTest(value):
					# a second line, billing another note or none, next to the line that bills this note
					extra = frappe.copy_doc(invoice.items[0])
					extra.update({"parent": invoice.name, "parenttype": "Sales Invoice", "parentfield": "items"})
					extra.delivery_note = value
					extra.dn_detail = None
					extra.idx = 2
					extra.docstatus = 1
					extra.db_insert()
					frappe.db.commit()
					try:
						self._run_cancel_refused(note, invoice, "also bills other Delivery Notes")
					finally:
						frappe.db.delete("Sales Invoice Item", {"name": extra.name})
						frappe.db.set_value("Delivery Note", note.name, "custom_last_integration_error", None)
						frappe.db.commit()
		finally:
			self._drop_invoice(invoice)

	def test_an_invoice_with_a_journal_entry_against_it_is_refused(self):
		note = self._live()
		self._submit(note)
		invoice = self._invoice_of(note)
		journal = frappe.get_doc(
			{
				"doctype": "Journal Entry",
				"company": self.company,
				"posting_date": frappe.utils.nowdate(),
				"voucher_type": "Credit Note",
				"accounts": [
					{"account": invoice.items[0].income_account, "debit_in_account_currency": 10},
					{
						"account": invoice.debit_to,
						"party_type": "Customer",
						"party": invoice.customer,
						"credit_in_account_currency": 10,
						"reference_type": "Sales Invoice",
						"reference_name": invoice.name,
					},
				],
			}
		).insert()
		journal.submit()
		frappe.db.commit()
		try:
			self._run_cancel_refused(note, invoice, journal.name)
		finally:
			self._drop_invoice(invoice, journals=[journal.name])

	def test_a_pos_or_not_fully_unpaid_invoice_is_refused(self):
		note = self._live()
		self._submit(note)
		invoice = self._invoice_of(note)
		total = frappe.db.get_value("Sales Invoice", invoice.name, "grand_total")
		try:
			for field, value in (("is_pos", 1), ("outstanding_amount", total - 1)):
				with self.subTest(field):
					frappe.db.set_value("Sales Invoice", invoice.name, field, value)
					frappe.db.commit()
					self._run_cancel_refused(note, invoice, "not fully unpaid")
					frappe.db.set_value("Sales Invoice", invoice.name, {"is_pos": 0, "outstanding_amount": total})
					frappe.db.set_value("Delivery Note", note.name, "custom_last_integration_error", None)
					frappe.db.commit()
		finally:
			self._drop_invoice(invoice)

	def test_an_invoice_with_a_payment_entry_blocks_everything_and_records_the_reason(self):
		from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

		note = self._live()
		self._submit(note)
		invoice = self._invoice_of(note)
		payment = get_payment_entry("Sales Invoice", invoice.name)
		payment.reference_no = "TEST-REF"
		payment.reference_date = frappe.utils.nowdate()
		payment.insert()
		payment.submit()
		frappe.db.commit()
		frappe.db.set_value("Delivery Note", note.name, "custom_integration_status", "Cancelled")
		try:
			with patch(self._NOTIFY) as notify:
				carrier_shipments.run_auto_cancel(note.name)

			values = stored(note, "docstatus", "custom_last_integration_error")
			self.assertEqual(values.docstatus, 1)
			self.assertEqual(frappe.db.get_value("Sales Invoice", invoice.name, "docstatus"), 1)
			self.assertIn(payment.name, values.custom_last_integration_error)
			self.assertEqual(notify.call_count, 1)
		finally:
			self._drop_invoice(invoice, payment.name)

	def test_a_failing_note_cancel_rolls_the_invoice_cancel_back(self):
		note = self._live()
		self._submit(note)
		invoice = self._invoice_of(note)
		frappe.db.set_value("Delivery Note", note.name, "custom_integration_status", "Cancelled")
		try:
			with (
				patch.object(carrier_shipments, "_check_native_cancel_not_blocked", side_effect=frappe.ValidationError("Return")),
				patch(self._NOTIFY),
			):
				carrier_shipments.run_auto_cancel(note.name)

			self.assertEqual(stored(note, "docstatus").docstatus, 1)
			self.assertEqual(frappe.db.get_value("Sales Invoice", invoice.name, "docstatus"), 1)
		finally:
			self._drop_invoice(invoice)

	def test_the_recipients_are_the_submitter_and_enabled_stock_managers_once_each(self):
		note = self._live()
		doc = self._submit(note)
		manager = self.make_user("Stock Manager")
		disabled = self.make_user("Stock Manager", "Sales User")
		frappe.db.set_value("User", disabled, "enabled", 0)

		users = carrier_shipments._notify_recipients(doc)

		self.assertIn(manager, users)
		self.assertNotIn(disabled, users)
		self.assertIn("Administrator", users)
		self.assertEqual(len(users), len(set(users)))

	def _submit_unknown(self, note):
		"""Submit needs Created: submit first, then put the cancel state back."""
		frappe.db.set_value("Delivery Note", note.name, "custom_integration_status", "Created")
		doc = self._submit(note)
		frappe.db.set_value("Delivery Note", note.name, "custom_integration_status", "Cancellation Unknown")
		return doc

	def test_a_linked_document_refuses_the_carrier_cancel_before_anything_is_sent(self):
		from unittest.mock import patch

		note = self._live()
		self._submit(note)

		with patch.object(
			carrier_shipments, "_check_native_cancel_not_blocked", side_effect=frappe.ValidationError("linked")
		):
			with self.assertRaises(frappe.ValidationError):
				carrier_shipments.cancel(note.name)

		self.assertEqual(self._sent_kinds(), [])
		self.assertEqual(stored(note, "custom_integration_status").custom_integration_status, "Created")

	def test_the_link_check_ignores_what_the_native_cancel_reverses_or_cancels_first(self):
		from unittest.mock import patch

		note = self._live()
		doc = frappe.get_doc("Delivery Note", note.name)
		seen = []

		def record(checked, method=None):
			seen.append(set(checked.ignore_linked_doctypes))

		with (
			patch("frappe.model.delete_doc.check_if_doc_is_linked", side_effect=record),
			patch("frappe.model.delete_doc.check_if_doc_is_dynamically_linked", side_effect=record),
		):
			carrier_shipments._check_native_cancel_not_blocked(doc)

		expected = {"GL Entry", "Stock Ledger Entry", "Repost Item Valuation", "Serial and Batch Bundle", "Packing Slip"}
		self.assertEqual(seen, [expected, expected])

	def test_a_read_leaves_a_note_the_app_cancelled_to_the_person_but_retries_a_blocked_automatic_cancel(self):
		from unittest.mock import patch

		note = self._live()
		self._submit(note)
		frappe.db.set_value("Delivery Note", note.name, "custom_integration_status", "Cancelled")
		with patch.object(carrier_shipments, "cancel_after_carrier_cancelled") as auto:
			statuses._write_state(note.name, 49)
		auto.assert_not_called()  # cancelled by the app's own Cancel (no error): the person cancels the note

		frappe.db.set_value("Delivery Note", note.name, "custom_last_integration_error", "blocked earlier")
		with patch.object(carrier_shipments, "cancel_after_carrier_cancelled") as auto:
			statuses._write_state(note.name, 49)
		auto.assert_called_once_with(note.name)

	def test_a_read_that_moves_the_note_into_cancelled_cancels_it_automatically(self):
		from unittest.mock import patch

		note = self._live()
		self._submit(note)
		with patch.object(carrier_shipments, "cancel_after_carrier_cancelled") as auto:
			statuses._write_state(note.name, 49)
		auto.assert_called_once_with(note.name)

	def test_the_label_is_queued_by_the_submit_not_by_the_create(self):
		from unittest.mock import patch

		note = self._live()
		with patch.object(frappe, "enqueue") as enqueue, patch.object(statuses, "replay_pending_events"):
			shipment.after_created(note.name, {"_id": "DELIVERY-1"})
		self.assertFalse([c for c in enqueue.call_args_list if "run_fetch_label" in str(c)])

		with patch.object(frappe, "enqueue") as enqueue:
			self._submit(note)
		self.assertEqual(len([c for c in enqueue.call_args_list if "run_fetch_label" in str(c)]), 1)

	def test_a_terminated_delivery_reading_not_found_resolves_the_note_to_cancelled(self):
		"""Real Bosta: a terminated delivery reads back HTTP 400, errorCode 1066 "Delivery not found."."""
		not_found = FakeResponse(400, {"message": "Delivery not found.", "errorCode": 1066})
		for status in ("Created", "Cancellation Pending", "Cancellation Unknown"):
			with self.subTest(status):
				note = self._live(custom_integration_status=status)
				self.http.queue["get"].append(not_found)

				statuses.refresh_note(note.name)

				values = stored(note, "custom_integration_status", "custom_shipping_status")
				self.assertEqual(values.custom_integration_status, "Cancelled")
				self.assertEqual(values.custom_shipping_status, "Terminated")

		# A note that is not a live shipment (Creating) is not cancelled by the same answer: the read stays a failure.
		creating = self._live(custom_integration_status="Creating")
		self.http.queue["get"].append(not_found)
		with self.assertRaises(frappe.ValidationError):
			statuses.refresh_note(creating.name)
		self.assertEqual(stored(creating, "custom_integration_status").custom_integration_status, "Creating")

	def test_a_submitted_created_note_whose_delivery_reads_not_found_is_cancelled_automatically(self):
		note = self._live()
		self._submit(note)
		self.http.queue["get"].append(FakeResponse(400, {"message": "Delivery not found.", "errorCode": 1066}))

		with patch(self._NOTIFY), patch.object(frappe, "enqueue"):
			statuses.refresh_note(note.name)
			carrier_shipments.run_auto_cancel(note.name)

		self.assertEqual(stored(note, "docstatus").docstatus, 2)
		self.assertEqual(self._sent_kinds(), ["get"])

	def test_a_blocked_automatic_cancel_of_a_terminated_delivery_is_retried_by_a_refresh_without_a_bosta_call(self):
		"""A terminated delivery reads back 1066 (not found): the retry of a Cancelled note must not depend on that read."""
		note = self._live()
		self._submit(note)
		frappe.db.set_value(
			"Delivery Note",
			note.name,
			{"custom_integration_status": "Cancelled", "custom_last_integration_error": "blocked earlier"},
		)
		self.http.queue["get"].append(FakeResponse(400, {"message": "Delivery not found.", "errorCode": 1066}))

		with patch(self._NOTIFY), patch.object(frappe, "enqueue") as enqueue:
			statuses.refresh_note(note.name)
			self.assertEqual(len([c for c in enqueue.call_args_list if "run_auto_cancel" in str(c)]), 1)
			carrier_shipments.run_auto_cancel(note.name)

		self.assertEqual(self.http.sent(), [])
		self.assertEqual(stored(note, "docstatus").docstatus, 2)

	def test_the_nightly_run_retries_a_submitted_cancelled_note_with_a_blocked_cancel_error(self):
		note = self._live()
		self._submit(note)
		frappe.db.set_value(
			"Delivery Note",
			note.name,
			{"custom_integration_status": "Cancelled", "custom_last_integration_error": "blocked earlier"},
		)
		self.assertIn(note.name, statuses._open_notes())

	# -- a wrong API key answers every read "not found" (nightly safety valve) ----------------------

	_NOT_FOUND = FakeResponse(400, {"message": "Delivery not found.", "errorCode": 1066})

	def _run_nightly(self, notes):
		for _ in notes:
			self.http.queue["get"].append(self._NOT_FOUND)
		with (
			patch.object(statuses, "_open_notes", return_value=[note.name for note in notes]),
			patch.object(statuses, "replay_pending_events"),
			patch.object(frappe, "enqueue"),
			patch.object(carrier_shipments, "notify_stock_managers") as notify,
		):
			statuses.run_reconcile()
		return notify

	def test_up_to_the_limit_the_nightly_run_applies_every_not_found_answer(self):
		notes = [self._live() for _ in range(statuses.MASS_1066_LIMIT)]

		notify = self._run_nightly(notes)

		for note in notes:
			self.assertEqual(stored(note, "custom_integration_status").custom_integration_status, "Cancelled")
		notify.assert_not_called()

	def test_over_the_limit_the_nightly_run_applies_none_and_raises_one_row_and_one_notification(self):
		notes = [self._live() for _ in range(statuses.MASS_1066_LIMIT + 1)]

		notify = self._run_nightly(notes)

		for note in notes:
			self.assertEqual(stored(note, "custom_integration_status").custom_integration_status, "Created")
		notify.assert_called_once()
		flagged = [
			note.name
			for note in notes
			if stored(note, "custom_last_integration_error").custom_last_integration_error == statuses._mass_1066_message()
		]
		self.assertEqual(flagged, [notes[0].name])
		listed = [row.get("reference_name") for row in attention._collect_carrier_items(attention.CARRIER_EXCEPTION)]
		self.assertEqual([name for name in listed if name in {n.name for n in notes}], [notes[0].name])

		# the same night again: the row is already there, nobody is told twice
		again = self._run_nightly(notes)
		again.assert_not_called()

	def test_a_single_refresh_still_applies_a_not_found_answer(self):
		note = self._live()
		self.http.queue["get"].append(self._NOT_FOUND)
		with patch.object(frappe, "enqueue"):
			statuses.refresh_note(note.name)
		self.assertEqual(stored(note, "custom_integration_status").custom_integration_status, "Cancelled")

	def test_a_closed_sales_order_refuses_the_carrier_cancel_before_anything_is_sent(self):
		note = self._live()
		doc = self._submit(note)
		order = doc.items[0].against_sales_order
		frappe.db.set_value("Sales Order", order, "status", "Closed")

		try:
			with self.assertRaises(frappe.ValidationError):
				carrier_shipments.cancel(note.name)
		finally:
			# tearDown cancels the note, which ERPNext refuses against a Closed Sales Order.
			frappe.db.set_value("Sales Order", order, "status", "To Deliver and Bill")
			frappe.db.commit()  # tearDown rolls back first

		self.assertEqual(self._sent_kinds(), [])
		self.assertEqual(stored(note, "custom_integration_status").custom_integration_status, "Created")

	def test_a_bosta_200_cancel_sets_the_terminated_shipping_status_and_leaves_the_order_summary(self):
		note = self._live()
		self.http.queue["terminate"].append(OK)

		carrier_shipments.cancel(note.name)

		values = stored(note, "custom_shipping_status", "custom_integration_status")
		self.assertEqual((values.custom_integration_status, values.custom_shipping_status), ("Cancelled", "Terminated"))

	def test_a_cancel_whose_read_shows_delivery_not_found_is_cancelled(self):
		note = self._live()
		self.http.queue["terminate"].append(FakeResponse(400, {"success": False, "errorCode": 1066}))
		self.http.queue["get"].append(FakeResponse(400, {"message": "Delivery not found.", "errorCode": 1066}))

		result = carrier_shipments.cancel(note.name)

		self.assertEqual(result["outcome"], "cancelled")
		self.assertEqual(stored(note, "custom_integration_status").custom_integration_status, "Cancelled")

	def test_an_already_cancelled_note_makes_no_bosta_call(self):
		note = self._live(custom_integration_status="Cancelled")

		result = carrier_shipments.cancel(note.name)

		self.assertEqual(result["status"], "Cancelled")
		self.assertEqual(self.http.sent(), [])

	def test_a_fresh_pending_is_refused_and_a_stale_one_is_retried(self):
		note = self._live(custom_integration_status="Cancellation Pending")
		with self.assertRaises(frappe.ValidationError):
			carrier_shipments.cancel(note.name)
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
		self.assertEqual(carrier_shipments.cancel(note.name)["outcome"], "cancelled")

	def test_only_a_created_shipment_can_be_cancelled_at_bosta(self):
		note = self.make_note()
		with self.assertRaises(frappe.ValidationError):
			carrier_shipments.cancel(note.name)
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
			carrier_api.cancel_shipment(note.name)
		frappe.set_user("Administrator")
		self.assertEqual(self.http.sent(), [])

	def test_a_delivery_cancelled_from_here_is_not_a_needs_attention_item(self):
		note = self._live()
		self.http.queue["terminate"].append(OK)
		carrier_shipments.cancel(note.name)

		items = attention._collect_carrier_items(attention.CARRIER_CANCELLED)
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

		items = attention._collect_carrier_items(attention.CARRIER_RETURN_REVIEW)
		mine = [i for i in items if i["reference_name"] == draft]
		self.assertEqual(len(mine), 1)
		self.assertEqual(mine[0]["role"], "Stock User")
		self.assertEqual(mine[0]["reference_doctype"], "Delivery Note")
		self.assertEqual(mine[0]["platform"], "Bosta")
		self.assertIn(note.name, mine[0]["explanation"])

		frappe.db.set_value("Delivery Note", draft, "docstatus", 1)
		items = attention._collect_carrier_items(attention.CARRIER_RETURN_REVIEW)
		self.assertEqual([i for i in items if i["reference_name"] == draft], [])
		frappe.db.set_value("Delivery Note", draft, "docstatus", 0)

	def test_a_cancelled_draft_return_is_not_made_again(self):
		note = self._submitted()
		draft = returns.create_returned_draft(note.name)
		frappe.db.set_value("Delivery Note", draft, "docstatus", 2)
		try:
			self.assertIsNone(returns.create_returned_draft(note.name))
		finally:
			frappe.db.set_value("Delivery Note", draft, "docstatus", 0)

	def test_a_manual_return_of_any_source_stops_the_draft(self):
		from erpnext.stock.doctype.delivery_note.delivery_note import make_sales_return

		note = self._submitted()
		manual = make_sales_return(note.name)
		manual.insert(ignore_permissions=True)
		frappe.db.commit()

		self.assertIsNone(returns.create_returned_draft(note.name))
		self.assertFalse(
			frappe.db.exists("Delivery Note", {"return_against": note.name, "custom_return_source": "Returned by Bosta"})
		)

	def test_goods_already_returned_stop_the_draft(self):
		note = self._submitted()
		frappe.db.set_value("Delivery Note", note.name, "per_returned", 50)
		self.assertIsNone(returns.create_returned_draft(note.name))

	def test_a_repeated_returned_read_queues_the_draft_only_on_the_change(self):
		note = self._submitted()
		queued = lambda: [  # noqa: E731
			c for c in self.enqueue.call_args_list if c.kwargs.get("job_id") == f"bosta-returned-draft|{note.name}"
		]

		self.http.queue["get"].append(delivery(46))
		statuses.refresh_note(note.name)
		self.assertEqual(len(queued()), 1)

		self.http.queue["get"].append(delivery(46))
		statuses.refresh_note(note.name)
		self.assertEqual(len(queued()), 1)

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
		# Ids of this test only: the webhook matches a note by delivery id or tracking number and ignores an
		# ambiguous match, so a leftover "DELIVERY-1" row in the shared test site must not be able to hide it.
		delivery_id, tracking = f"DELIVERY-{self.suffix}", f"TRK-{self.suffix}"
		frappe.db.set_value("Delivery Note", note.name, {"custom_carrier_delivery_id": delivery_id, "lr_no": tracking})
		frappe.db.commit()
		statuses.process_webhook_event(
			{"_id": delivery_id, "trackingNumber": tracking, "state": 46, "type": "SEND", "timeStamp": 9000}
		)
		key = frappe.get_all("Integration Request", filters={"custom_delivery_id": delivery_id}, pluck="name")[0]
		statuses.apply_event(key)

		jobs = [c.kwargs.get("job_id") for c in self.enqueue.call_args_list]
		self.assertIn(f"bosta-returned-draft|{note.name}", jobs)
		frappe.db.delete("Integration Request", {"custom_delivery_id": delivery_id})
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
