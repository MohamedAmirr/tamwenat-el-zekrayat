# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""The bulk "Delivery Note" action of the Sales Order list (``services/bulk_delivery_notes.py``).

Real database; the carrier is mocked: only the Bosta HTTP layer is faked (``tests/bosta_fixtures.py``) and
``frappe.enqueue`` is patched, so the create job never runs by itself. Tests that need the job call
``carrier_shipments.run_create`` with the kwargs ``request_create`` queued. Never contacts Bosta, Noon or
Shopify."""

from unittest.mock import patch

import frappe
from frappe import _
from frappe.tests import IntegrationTestCase
from frappe.utils import nowdate, strip_html_tags

from marketplace_integrations.integrations.bosta import delivery_note_shipment as shipment
from marketplace_integrations.services import bulk_delivery_notes as bulk
from marketplace_integrations.services import carrier_log, carrier_shipments
from marketplace_integrations.tests.bosta_fixtures import BostaFixtureMixin, created
from marketplace_integrations.tests.utils import make_role_user

STAFF_PERMISSIONS = {
	"Sales Order": ["read"],
	"Delivery Note": ["create", "submit", "write"],
	"Sales Invoice": ["create", "submit", "write"],
	"Customer": ["read"],
	"Address": ["read"],
	"Item": ["read"],
	"Account": ["read"],
}


class IntegrationTestBulkDeliveryNotes(BostaFixtureMixin, IntegrationTestCase):
	def setUp(self):
		super().setUp()

		def fill_city(doc):
			values = {"custom_bosta_city": "EG-01", "custom_bosta_city_name": "Cairo"}
			frappe.db.set_value("Delivery Note", doc.name, values, update_modified=False)
			doc.update(values)
			return True

		patcher = patch.object(shipment, "autofill_city", side_effect=fill_city)
		patcher.start()
		self.addCleanup(patcher.stop)

		# Cancelling an invoice that a Payment Entry paid must unlink the entry, or the teardown could not
		# remove the fixtures (the mixin purges newest first, but links go both ways).
		previous = frappe.db.get_single_value(
			"Accounts Settings", "unlink_payment_on_cancellation_of_invoice"
		)
		frappe.db.set_single_value("Accounts Settings", "unlink_payment_on_cancellation_of_invoice", 1)
		frappe.db.commit()

		def restore():
			frappe.db.set_single_value(
				"Accounts Settings", "unlink_payment_on_cancellation_of_invoice", previous
			)
			frappe.db.commit()

		self.addCleanup(restore)

	# -- helpers ----------------------------------------------------------------------------

	def _order(self, rate=555.5, qty=1, platform="Shopify", phone="01000000000"):
		"""A submitted marketplace Sales Order that has a receiver, ready to ship."""
		order = self.make_order(rate=rate, qty=qty)
		frappe.db.set_value(
			"Sales Order",
			order.name,
			{
				"custom_marketplace_platform": platform,
				"shipping_address_name": self.address.name,
				"contact_mobile": phone,
			},
		)
		frappe.db.commit()
		return frappe.get_doc("Sales Order", order.name)

	def _pay(self, order, amount=None):
		from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

		entry = get_payment_entry("Sales Order", order.name)
		if amount is not None:
			entry.paid_amount = entry.received_amount = amount
			entry.references[0].allocated_amount = amount
		if not entry.paid_to:
			entry.paid_to = frappe.db.get_value(
				"Company", self.company, "default_cash_account"
			) or frappe.db.get_value("Company", self.company, "default_bank_account")
		entry.reference_no = f"BULK-{self.suffix}"
		entry.reference_date = nowdate()
		entry.insert()
		entry.submit()
		self._track(entry)
		frappe.db.commit()

	def _keep_invoices(self, order):
		"""Register the invoices a run made so ``tearDown`` removes them (after the entries, before the order)."""
		for name in bulk._invoices(order.name, 1) + bulk._invoices(order.name, 0):
			if not any(doc.doctype == "Sales Invoice" and doc.name == name for doc in self._created):
				self._track(frappe.get_doc("Sales Invoice", name))

	def _note_of(self, order):
		return bulk.live_note(order.name)

	def _queue_bosta_answer(self):
		self.http.queue["create"].append(
			created(tracking=f"TRK-{self.suffix}", delivery_id=f"D-{self.suffix}")
		)

	def _run_job(self, which=-1):
		"""The create job ``request_create`` queued (the ``which``-th), as the worker would run it."""
		jobs = [
			call for call in self.enqueue.call_args_list if call.args and call.args[0].endswith("run_create")
		]
		kwargs = jobs[which].kwargs
		self._queue_bosta_answer()
		carrier_shipments.run_create(
			kwargs["delivery_note"], kwargs["log"], kwargs["lookup_first"], kwargs["submit_as"]
		)

	def _events(self, publish):
		return [call.args[1] for call in publish.call_args_list if call.args[0] == bulk.EVENT]

	def _shipper(self):
		"""A job that runs every queued create job once when the bulk run waits (the Bosta worker)."""
		done = set()

		def sleep(_seconds):
			for call in self.enqueue.call_args_list:
				if not (call.args and call.args[0].endswith("run_create")) or id(call) in done:
					continue
				done.add(id(call))
				self._queue_bosta_answer()
				kwargs = call.kwargs
				carrier_shipments.run_create(
					kwargs["delivery_note"], kwargs["log"], kwargs["lookup_first"], kwargs["submit_as"]
				)

		return sleep

	# -- COD rule: the Sales Order grand total ------------------------------------------------

	def test_an_unpaid_order_ships_with_the_grand_total_as_a_confirmed_cod_and_the_note_is_submitted_by_bosta(
		self,
	):
		order = self._order(rate=555.5)

		result = bulk.process_order(order.name)

		self.assertEqual(result["status"], bulk.SENT, result)
		note = frappe.get_doc("Delivery Note", result["delivery_note"])
		self.assertEqual(note.transporter, self.supplier)
		# Shopify's total (grand_total), not the rounded one.
		self.assertEqual(note.custom_cod_amount, order.grand_total)
		self.assertEqual(note.custom_cod_amount, 555.5)
		self.assertEqual(note.custom_cod_confirmed, 1)
		self.assertEqual(note.custom_integration_status, "Creating")
		self.assertIsNone(result["sales_invoice"])
		# Still a draft: only the Bosta job submits it, after Bosta confirmed.
		self.assertEqual(note.docstatus, 0)
		self.assertEqual(self.enqueue.call_args.kwargs["submit_as"], frappe.session.user)
		self._run_job()
		submitted = frappe.db.get_value(
			"Delivery Note", note.name, ["docstatus", "custom_integration_status"], as_dict=True
		)
		self.assertEqual(submitted.docstatus, 1)
		self.assertEqual(submitted.custom_integration_status, "Created")
		self.assertEqual(len(self.http.calls_of("create")), 1)

	def test_a_fully_paid_order_gets_a_submitted_paid_invoice_and_cod_zero(self):
		order = self._order(rate=555.5)
		self._pay(order)

		result = bulk.process_order(order.name)
		self._keep_invoices(order)

		self.assertEqual(result["status"], bulk.SENT, result)
		invoice = frappe.get_doc("Sales Invoice", result["sales_invoice"])
		self.assertEqual(invoice.docstatus, 1)
		self.assertEqual(invoice.update_stock, 0)
		# Trusted after submit and reload: the advances are allocated and nothing is outstanding.
		self.assertGreater(invoice.total_advance, 0)
		self.assertEqual(round(invoice.outstanding_amount, 2), 0)
		note = frappe.get_doc("Delivery Note", result["delivery_note"])
		self.assertEqual(note.custom_cod_amount, 0)
		self.assertEqual(note.custom_cod_confirmed, 1)
		self.assertEqual(result["cod"], 0)

	def test_a_partly_paid_order_ships_with_the_rest_as_cod_and_no_invoice(self):
		order = self._order(rate=555.5)
		self._pay(order, amount=200)

		result = bulk.process_order(order.name)
		self._keep_invoices(order)

		self.assertEqual(result["status"], bulk.SENT, result)
		self.assertIsNone(result["sales_invoice"])
		self.assertEqual(bulk._invoices(order.name, 1), [])
		self.assertEqual(
			frappe.db.get_value("Delivery Note", result["delivery_note"], "custom_cod_amount"), 355.5
		)

	def test_the_payment_plan_basis_is_the_grand_total_and_a_paid_order_always_gets_an_invoice(self):
		order = self._order(rate=100)

		def plan(**values):
			frappe.db.set_value("Sales Order", order.name, values)
			return bulk.payment_plan(frappe.get_doc("Sales Order", order.name))

		unpaid = plan(advance_paid=0)
		self.assertEqual(
			(unpaid["cod"], unpaid["needs_invoice"], unpaid["paid_in_full"]), (100, False, False)
		)
		exact = plan(advance_paid=100)
		self.assertEqual((exact["cod"], exact["needs_invoice"], exact["paid_in_full"]), (0, True, True))
		# More recorded than the total: still paid in full, still an invoice, never COD 0 without one.
		more = plan(advance_paid=120)
		self.assertEqual((more["cod"], more["needs_invoice"]), (0, True))

	def test_fully_paid_is_judged_by_erpnexts_rounded_measure(self):
		order = self._order(rate=100)

		def plan(**values):
			frappe.db.set_value("Sales Order", order.name, values)
			return bulk.payment_plan(frappe.get_doc("Sales Order", order.name))

		# Rounds DOWN: grand 494.21, rounded 494; ERPNext caps the payment at 494, which is fully paid.
		down = {"grand_total": 494.21, "rounded_total": 494}
		paid = plan(**down, advance_paid=494)
		self.assertEqual((paid["cod"], paid["needs_invoice"], paid["paid_in_full"]), (0, True, True))
		partial = plan(**down, advance_paid=200)
		self.assertEqual((partial["cod"], partial["needs_invoice"]), (294.21, False))
		self.assertEqual(plan(**down, advance_paid=0)["cod"], 494.21)
		# Rounds UP: grand 555.5, rounded 556.
		up = {"grand_total": 555.5, "rounded_total": 556}
		self.assertEqual(plan(**up, advance_paid=556)["needs_invoice"], True)
		self.assertEqual(plan(**up, advance_paid=200)["cod"], 355.5)
		self.assertEqual(plan(**up, advance_paid=0)["cod"], 555.5)
		# Paid to the grand total only: just a rounding difference is left, and COD 0 without an invoice is never
		# shipped.
		with self.assertRaises(bulk.Refused):
			plan(**up, advance_paid=555.5)

	def test_a_paid_order_that_rounds_down_gets_an_invoice_and_cod_zero(self):
		order = self._order(rate=494.21)
		order.reload()
		if not order.rounded_total or order.rounded_total == order.grand_total:
			self.skipTest("Rounded totals are disabled on this site.")
		self._pay(order)

		result = bulk.process_order(order.name)
		self._keep_invoices(order)

		self.assertEqual(result["status"], bulk.SENT, result)
		self.assertTrue(result["sales_invoice"])
		self.assertEqual(
			frappe.db.get_value("Delivery Note", result["delivery_note"], "custom_cod_amount"), 0
		)
		self.assertEqual(
			round(frappe.db.get_value("Sales Invoice", result["sales_invoice"], "outstanding_amount"), 2), 0
		)

	def test_a_zero_total_order_is_refused(self):
		with self.assertRaises(bulk.Refused):
			bulk.payment_plan(frappe._dict(name="SO-ZERO", grand_total=0))

	# -- existing invoices ----------------------------------------------------------------------

	def test_an_existing_paid_invoice_is_reused_not_duplicated(self):
		order = self._order(rate=555.5)
		self._pay(order)
		first = bulk._make_paid_invoice(frappe.get_doc("Sales Order", order.name))
		frappe.db.commit()
		self._keep_invoices(order)

		result = bulk.process_order(order.name)

		self.assertEqual(result["status"], bulk.SENT, result)
		self.assertEqual(result["sales_invoice"], first)
		self.assertEqual(bulk._invoices(order.name, 1), [first])
		self.assertEqual(
			frappe.db.get_value("Delivery Note", result["delivery_note"], "custom_cod_amount"), 0
		)

	def test_more_than_one_submitted_invoice_blocks_the_order_with_a_reason(self):
		order = self._order()
		with patch.object(
			bulk,
			"_invoices",
			side_effect=lambda name, docstatus: ["SINV-A", "SINV-B"] if docstatus == 1 else [],
		):
			result = bulk.process_order(order.name)

		self.assertEqual(result["status"], bulk.SKIPPED)
		self.assertIn("more than one submitted Sales Invoice", result["message"])
		self.assertIsNone(self._note_of(order))

	def test_an_invoice_that_covers_only_part_of_the_order_blocks_it(self):
		from erpnext.selling.doctype.sales_order.sales_order import make_sales_invoice

		order = self._order(rate=100, qty=2)
		partial = make_sales_invoice(order.name)
		partial.items[0].qty = 1
		partial.insert()
		partial.submit()
		frappe.db.commit()
		self._keep_invoices(order)

		result = bulk.process_order(order.name)

		self.assertEqual(result["status"], bulk.SKIPPED, result)
		self.assertIn("does not cover the whole Sales Order", result["message"])
		self.assertIsNone(self._note_of(order))

	def test_an_invoice_with_a_credit_note_blocks_the_order(self):
		order = self._order(rate=555.5)
		self._pay(order)
		bulk._make_paid_invoice(frappe.get_doc("Sales Order", order.name))
		frappe.db.commit()
		self._keep_invoices(order)
		real = frappe.db.exists

		def exists(doctype, filters=None, *args, **kwargs):
			if doctype == "Sales Invoice" and isinstance(filters, dict) and "return_against" in filters:
				return "SINV-CREDIT-NOTE"
			return real(doctype, filters, *args, **kwargs)

		with patch.object(frappe.db, "exists", side_effect=exists):
			result = bulk.process_order(order.name)

		self.assertEqual(result["status"], bulk.SKIPPED, result)
		self.assertIn("has a credit note", result["message"])
		self.assertIsNone(self._note_of(order))

	def test_an_outstanding_invoice_with_a_payment_still_on_the_order_is_ambiguous_and_refused(self):
		order = self._order(rate=555.5)
		from erpnext.selling.doctype.sales_order.sales_order import make_sales_invoice

		invoice = make_sales_invoice(order.name)
		invoice.insert()
		invoice.submit()
		frappe.db.set_value("Sales Order", order.name, "advance_paid", 100)
		frappe.db.commit()
		self._keep_invoices(order)

		result = bulk.process_order(order.name)

		self.assertEqual(result["status"], bulk.SKIPPED, result)
		self.assertIn("Apply the payment to the invoice first", result["message"])

	def test_an_unpaid_invoice_that_covers_the_order_ships_with_the_grand_total_as_cod(self):
		from erpnext.selling.doctype.sales_order.sales_order import make_sales_invoice

		order = self._order(rate=555.5)
		invoice = make_sales_invoice(order.name)
		invoice.insert()
		invoice.submit()
		frappe.db.commit()
		self._keep_invoices(order)

		result = bulk.process_order(order.name)

		self.assertEqual(result["status"], bulk.SENT, result)
		self.assertEqual(result["sales_invoice"], invoice.name)
		self.assertEqual(
			frappe.db.get_value("Delivery Note", result["delivery_note"], "custom_cod_amount"), 555.5
		)

	# -- eligibility ------------------------------------------------------------------------

	def _skipped(self, order, expected):
		result = bulk.process_order(order.name)
		self.assertEqual(result["status"], bulk.SKIPPED, result)
		self.assertIn(expected, result["message"])
		self.assertIsNone(result["delivery_note"])
		self.assertIsNone(self._note_of(order))
		return result

	def test_ineligible_orders_are_skipped_with_a_reason_and_never_raise(self):
		self._skipped(self._order(platform=None), "is not a marketplace order")

		closed = self._order()
		frappe.db.set_value("Sales Order", closed.name, "status", "Closed")
		self._skipped(closed, "only To Deliver and To Deliver and Bill orders")

		self._skipped(self.make_order(submit=False), "is not submitted")
		self._skipped(frappe._dict(name=f"SO-MISSING-{self.suffix}"), "was not found")

	def test_noon_and_talabat_orders_are_refused_with_the_same_text_as_the_single_form(self):
		for platform in ("Noon", "Talabat"):
			order = self._order(platform=platform)
			expected = _(
				"Bosta shipments cannot be created for {0} orders: {0} fulfils its own orders."
			).format(platform)
			result = self._skipped(order, expected)
			self.assertEqual(result["message"], expected)
			# The single form refuses a Bosta note for such an order with this very text.
			with patch.object(shipment, "excluded_platform", return_value=platform):
				with self.assertRaises(frappe.ValidationError) as single:
					self.make_note(self.make_order())
			self.assertIn(expected, strip_html_tags(str(single.exception)))

	def test_an_order_with_a_live_delivery_note_is_skipped(self):
		order = self._order()
		note = self.make_note(order)

		result = bulk.process_order(order.name)

		self.assertEqual(result["status"], bulk.SKIPPED)
		self.assertIn(note.name, result["message"])
		self.assertEqual(self._note_of(order), note.name)

	def test_a_missing_bosta_supplier_skips_the_order(self):
		order = self._order()
		with patch.object(shipment, "bosta_supplier", return_value=None):
			result = bulk.process_order(order.name)

		self.assertEqual(result["status"], bulk.SKIPPED)
		self.assertIn("no Supplier named Bosta", result["message"])

	# -- nothing half-done ------------------------------------------------------------------

	def test_an_invoice_failure_ships_nothing_and_leaves_no_note(self):
		order = self._order()
		self._pay(order)
		with patch.object(
			bulk, "_make_paid_invoice", side_effect=frappe.ValidationError("No income account")
		):
			result = bulk.process_order(order.name)

		self.assertEqual(result["status"], bulk.FAILED)
		self.assertIn("No income account", result["message"])
		self.assertIsNone(self._note_of(order))
		self.assertEqual(bulk._invoices(order.name, 1), [])
		self.assertFalse(self.enqueue.called)

	def test_a_note_failure_rolls_the_invoice_back(self):
		order = self._order()
		self._pay(order)
		with patch.object(bulk, "_make_note", side_effect=frappe.ValidationError("No address")):
			result = bulk.process_order(order.name)
		self._keep_invoices(order)

		self.assertEqual(result["status"], bulk.FAILED)
		self.assertEqual(bulk._invoices(order.name, 1), [])
		self.assertIsNone(self._note_of(order))

	def test_a_paid_invoice_that_stays_unpaid_is_rolled_back(self):
		order = self._order()
		self._pay(order, amount=200)
		# Wrongly judged fully paid: the submitted invoice keeps an outstanding amount, so nothing is shipped.
		plan = {"cod": 0, "paid_in_full": True, "needs_invoice": True, "invoice": None}
		with patch.object(bulk, "payment_plan", return_value=plan):
			result = bulk.process_order(order.name)
		self._keep_invoices(order)

		self.assertEqual(result["status"], bulk.FAILED)
		self.assertIn("was not fully paid", result["message"])
		self.assertEqual(bulk._invoices(order.name, 1), [])
		self.assertIsNone(self._note_of(order))

	def test_a_cod_above_the_bosta_limit_fails_the_order_without_a_note(self):
		order = self._order(rate=30001)

		result = bulk.process_order(order.name)

		self.assertEqual(result["status"], bulk.FAILED)
		self.assertIn("exceeds Bosta's limit", result["message"])
		self.assertIsNone(self._note_of(order))

	# -- guards: the bulk path never submits and never bypasses the Submit flow ----------------

	def test_the_bulk_run_never_submits_a_delivery_note_itself(self):
		order = self._order()
		with patch(
			"erpnext.stock.doctype.delivery_note.delivery_note.DeliveryNote.submit", autospec=True
		) as submit:
			result = bulk.process_order(order.name)

		self.assertEqual(result["status"], bulk.SENT, result)
		submit.assert_not_called()
		self.assertEqual(frappe.db.get_value("Delivery Note", result["delivery_note"], "docstatus"), 0)

	def test_a_refused_shipment_leaves_the_draft_and_the_invoice_and_is_never_submitted(self):
		for answer in (carrier_log.Failed("Bosta refused the city"), carrier_log.Unknown("no answer")):
			order = self._order(rate=555.5)
			self._pay(order)
			result = bulk.process_order(order.name)
			self._keep_invoices(order)
			self.assertEqual(result["status"], bulk.SENT, result)

			with patch.object(shipment, "create_remote", side_effect=answer):
				self._run_job()

			values = frappe.db.get_value(
				"Delivery Note",
				result["delivery_note"],
				["docstatus", "custom_integration_status"],
				as_dict=True,
			)
			self.assertEqual(values.docstatus, 0)
			self.assertIn(values.custom_integration_status, ("Creation Failed", "Creation Unknown"))
			self.assertEqual(frappe.db.get_value("Sales Invoice", result["sales_invoice"], "docstatus"), 1)

	def test_when_bosta_cannot_be_started_the_draft_note_is_kept_with_the_reason(self):
		order = self._order()
		with patch.object(
			carrier_shipments,
			"request_create",
			side_effect=frappe.ValidationError("The receiver phone is missing."),
		):
			result = bulk.process_order(order.name)

		self.assertEqual(result["status"], bulk.FAILED)
		self.assertEqual(frappe.db.get_value("Delivery Note", result["delivery_note"], "docstatus"), 0)
		self.assertIn(result["delivery_note"], result["message"])
		self.assertIn("The receiver phone is missing.", result["message"])

	def test_a_missing_phone_is_refused_with_the_single_forms_text_and_the_draft_is_kept(self):
		order = self._order(phone="")
		self._pay(order)

		result = bulk.process_order(order.name)
		self._keep_invoices(order)

		self.assertEqual(result["status"], bulk.FAILED, result)
		draft = frappe.get_doc("Delivery Note", result["delivery_note"])
		self.assertEqual(draft.docstatus, 0)
		with self.assertRaises(frappe.ValidationError) as single:
			shipment.validate_ready(draft)
		self.assertIn(strip_html_tags(str(single.exception)), result["message"])
		# the paid order keeps its invoice
		self.assertTrue(result["sales_invoice"])
		self.assertEqual(frappe.db.get_value("Sales Invoice", result["sales_invoice"], "docstatus"), 1)

	def test_a_missing_city_leaves_a_named_draft_for_the_person(self):
		order = self._order()
		with patch.object(shipment, "autofill_city", return_value=False):
			result = bulk.process_order(order.name)

		self.assertEqual(result["status"], bulk.FAILED)
		self.assertIn("pick the city", result["message"])
		self.assertEqual(result["delivery_note"], self._note_of(order))
		self.assertEqual(frappe.db.get_value("Delivery Note", result["delivery_note"], "docstatus"), 0)

	# -- the job: progress, wait, summary -----------------------------------------------------

	def test_one_failing_order_does_not_stop_the_others(self):
		bad = self._order(platform=None)
		good = self._order()
		missing = f"SO-MISSING-{self.suffix}"

		with (
			patch("frappe.publish_realtime") as publish,
			patch.object(bulk, "WAIT_SECONDS", 0),
			patch.object(bulk, "enqueue_create_notification"),
		):
			results = bulk.run_bulk([missing, bad.name, good.name], "Bosta", "Administrator", run_id="r1")

		self.assertEqual(
			[row["status"] for row in results], [bulk.SKIPPED, bulk.SKIPPED, bulk.STILL_CREATING]
		)
		self.assertEqual([row["sales_order"] for row in results], [missing, bad.name, good.name])
		events = self._events(publish)
		self.assertEqual(events[-1]["stage"], "done")
		self.assertEqual(events[-1]["rows"], results)
		# The patch also catches Frappe's own realtime events (doc_update of each save): only the bulk events
		# are checked here.
		bulk_calls = [call for call in publish.call_args_list if call.args and call.args[0] == bulk.EVENT]
		self.assertTrue(bulk_calls)
		for call in bulk_calls:
			self.assertEqual(call.kwargs["user"], "Administrator")

	def test_progress_goes_from_creating_to_waiting_to_the_summary(self):
		first = self._order(rate=100)
		bad = self._order(platform=None)
		second = self._order(rate=200)

		with (
			patch("frappe.publish_realtime") as publish,
			patch.object(bulk.time, "sleep", side_effect=self._shipper()),
			patch.object(bulk, "enqueue_create_notification") as notify,
		):
			results = bulk.run_bulk(
				[first.name, bad.name, second.name], "Bosta", "Administrator", run_id="run-7"
			)

		self.assertEqual([row["status"] for row in results], [bulk.ACCEPTED, bulk.SKIPPED, bulk.ACCEPTED])
		events = self._events(publish)
		stages = [event["stage"] for event in events]
		self.assertEqual(stages[0], "creating")
		self.assertEqual(stages[-1], "done")
		self.assertLess(stages.index("waiting"), len(stages) - 1)
		self.assertTrue(all(event["run_id"] == "run-7" and event["total"] == 3 for event in events))
		processed = [event["processed"] for event in events if event["stage"] == "creating"]
		self.assertEqual(processed, sorted(processed))
		self.assertEqual(processed[0], 0)
		self.assertEqual(processed[-1], 3)
		# While an order is being handled it shows as creating
		self.assertTrue(any(row["status"] == bulk.CREATING for event in events for row in event["rows"]))
		waiting = [event for event in events if event["stage"] == "waiting"]
		self.assertEqual(waiting[0]["sent_total"], 2)
		self.assertEqual(waiting[0]["accepted"], 0)
		self.assertEqual(waiting[-1]["accepted"], 2)
		self.assertEqual(
			events[-1]["summary"], {"total": 3, "accepted": 2, "failed": 0, "skipped": 1, "still_creating": 0}
		)
		# Bosta confirmed: the existing flow submitted both notes
		for row in results:
			if row["delivery_note"]:
				self.assertEqual(frappe.db.get_value("Delivery Note", row["delivery_note"], "docstatus"), 1)
		# The summary is kept as a native notification
		users, doc = notify.call_args.args
		self.assertEqual(users, ["Administrator"])
		self.assertIn("2 accepted, 0 failed, 1 skipped, 0 still creating", doc["subject"])
		self.assertIn("is not a marketplace order", doc["email_content"])
		self.assertEqual(doc["document_type"], "Delivery Note")

	def test_a_shipment_bosta_refuses_is_a_failure_with_its_reason_and_the_draft_kept(self):
		order = self._order(rate=555.5)
		self._pay(order)

		with (
			patch("frappe.publish_realtime") as publish,
			patch.object(bulk.time, "sleep", side_effect=self._shipper()),
			patch.object(
				shipment, "create_remote", side_effect=carrier_log.Failed("Bosta refused the address")
			),
			patch.object(bulk, "enqueue_create_notification") as notify,
		):
			results = bulk.run_bulk([order.name], "Bosta", "Administrator", run_id="r")
		self._keep_invoices(order)

		self.assertEqual(results[0]["status"], bulk.FAILED)
		self.assertIn("Bosta did not accept the shipment: Bosta refused the address", results[0]["message"])
		self.assertEqual(frappe.db.get_value("Delivery Note", results[0]["delivery_note"], "docstatus"), 0)
		self.assertEqual(frappe.db.get_value("Sales Invoice", results[0]["sales_invoice"], "docstatus"), 1)
		summary = self._events(publish)[-1]["summary"]
		self.assertEqual((summary["accepted"], summary["failed"]), (0, 1))
		content = notify.call_args.args[1]["email_content"]
		self.assertIn("Bosta refused the address", content)
		self.assertIn(results[0]["delivery_note"], content)
		self.assertIn(results[0]["sales_invoice"], content)

	def test_an_order_bosta_has_not_answered_yet_is_reported_still_creating(self):
		order = self._order()

		with (
			patch("frappe.publish_realtime") as publish,
			patch.object(bulk, "WAIT_SECONDS", 0),
			patch.object(bulk.time, "sleep") as sleep,
			patch.object(bulk, "enqueue_create_notification") as notify,
		):
			results = bulk.run_bulk([order.name], "Bosta", "Administrator", run_id="r")

		self.assertEqual(results[0]["status"], bulk.STILL_CREATING)
		self.assertIn("Check the Delivery Note list later", results[0]["message"])
		self.assertEqual(self._events(publish)[-1]["summary"]["still_creating"], 1)
		self.assertIn("1 still creating", notify.call_args.args[1]["subject"])
		sleep.assert_not_called()

	def test_created_at_bosta_but_still_a_draft_is_not_accepted(self):
		order = self._order()
		result = bulk.process_order(order.name)
		frappe.db.set_value("Delivery Note", result["delivery_note"], "custom_integration_status", "Created")
		frappe.db.commit()
		rows = [result]

		with patch.object(bulk, "WAIT_SECONDS", 0):
			bulk.wait_for_bosta(rows, lambda: None)

		self.assertEqual(rows[0]["status"], bulk.FAILED)
		self.assertIn(result["delivery_note"], rows[0]["message"])
		self.assertIn("has not been submitted", rows[0]["message"])

	def test_a_draft_whose_automatic_submit_failed_is_a_failure_with_the_error(self):
		order = self._order()
		result = bulk.process_order(order.name)
		frappe.db.set_value(
			"Delivery Note",
			result["delivery_note"],
			{
				"custom_integration_status": "Created",
				"custom_last_integration_error": "Shipment created at Bosta; the Delivery Note could not be submitted: credit limit",
			},
		)
		frappe.db.commit()
		rows = [result]
		changes = []

		bulk.wait_for_bosta(rows, lambda: changes.append(True))

		self.assertEqual(rows[0]["status"], bulk.FAILED)
		self.assertIn("credit limit", rows[0]["message"])
		self.assertIn(result["delivery_note"], rows[0]["message"])
		self.assertEqual(len(changes), 1)

	def test_a_crash_outside_an_order_still_ends_with_a_done_event_and_a_notification(self):
		order = self._order()
		with (
			patch("frappe.publish_realtime") as publish,
			patch.object(bulk, "wait_for_bosta", side_effect=RuntimeError("worker lost")),
			patch.object(bulk, "enqueue_create_notification") as notify,
			patch("frappe.log_error") as log_error,
		):
			results = bulk.run_bulk([order.name], "Bosta", "Administrator", run_id="r")

		self.assertEqual(results[0]["status"], bulk.FAILED)
		self.assertIn("worker lost", results[0]["message"])
		last = self._events(publish)[-1]
		self.assertEqual(last["stage"], "done")
		self.assertEqual(last["summary"]["failed"], 1)
		notify.assert_called_once()
		log_error.assert_called_once()

	def test_a_failing_notification_never_breaks_the_run(self):
		order = self._order(platform=None)
		with (
			patch("frappe.publish_realtime"),
			patch.object(bulk, "enqueue_create_notification", side_effect=RuntimeError("down")),
			patch("frappe.log_error") as log_error,
		):
			results = bulk.run_bulk([order.name], "Bosta", "Administrator")

		self.assertEqual(results[0]["status"], bulk.SKIPPED)
		log_error.assert_called_once()

	def test_an_unexpected_error_in_one_order_is_a_failed_row_not_a_crash(self):
		first = self._order(platform=None)
		second = self._order(platform=None)
		real = bulk.ship_order

		def flaky(name, method):
			if name == first.name:
				raise RuntimeError("boom")
			return real(name, method)

		with (
			patch.object(bulk, "ship_order", side_effect=flaky),
			patch("frappe.publish_realtime"),
			patch("frappe.log_error") as log_error,
			patch.object(bulk, "enqueue_create_notification"),
		):
			results = bulk.run_bulk([first.name, second.name], "Bosta", "Administrator")

		self.assertEqual([row["status"] for row in results], [bulk.FAILED, bulk.SKIPPED])
		self.assertEqual(results[0]["message"], "boom")
		log_error.assert_called_once()

	# -- request and permissions ------------------------------------------------------------

	def test_the_request_queues_one_job_with_the_clean_names(self):
		order = self._order()

		answer = bulk.queue_bulk([order.name, order.name, " "], "Bosta")

		self.assertTrue(answer["queued"])
		self.assertEqual(answer["count"], 1)
		self.assertEqual(answer["orders"], [order.name])
		call = self.enqueue.call_args
		self.assertEqual(call.args[0], "marketplace_integrations.services.bulk_delivery_notes.run_bulk")
		self.assertEqual(call.kwargs["sales_orders"], [order.name])
		self.assertEqual(call.kwargs["shipping_method"], "Bosta")
		self.assertEqual(call.kwargs["requested_by"], frappe.session.user)
		self.assertEqual(call.kwargs["run_id"], answer["run_id"])
		self.assertTrue(call.kwargs["deduplicate"])

	def test_a_run_that_is_already_queued_is_answered_not_queued(self):
		self.enqueue.return_value = None  # frappe.enqueue(deduplicate=True) answers None for a live job id

		answer = bulk.queue_bulk(["SO-1"], "Bosta")

		self.assertFalse(answer["queued"])
		self.assertIn("still in progress", answer["message"])

	def test_the_batch_is_validated(self):
		self.assertEqual(bulk.parse_orders('["SO-1", "SO-2"]'), ["SO-1", "SO-2"])
		for bad in ([f"SO-{i}" for i in range(bulk.MAX_ORDERS + 1)], [], "not json", "{", None, 5):
			with self.assertRaises(frappe.ValidationError, msg=str(bad)[:20]):
				bulk.parse_orders(bad)
		with self.assertRaises(frappe.ValidationError):
			bulk.queue_bulk(["SO-1"], "Noon")

	def test_a_user_who_cannot_create_and_submit_the_documents_is_refused_up_front(self):
		email, _role = make_role_user(self, "Bulk Reader", {"Sales Order": ["read"]})
		# make_role_user's User/Role inserts reach the patched frappe.enqueue; only the request is checked.
		self.enqueue.reset_mock()
		frappe.set_user(email)
		try:
			with self.assertRaises(frappe.PermissionError):
				bulk.queue_bulk(["SO-1"], "Bosta")
		finally:
			frappe.set_user("Administrator")
		self.assertFalse(self.enqueue.called)

	def test_a_user_with_only_note_rights_is_refused_for_the_invoice(self):
		email, _role = make_role_user(
			self, "Bulk Notes", {"Delivery Note": ["create", "submit"], "Sales Order": ["read"]}
		)
		frappe.set_user(email)
		try:
			with self.assertRaises(frappe.PermissionError):
				bulk.check_permissions()
		finally:
			frappe.set_user("Administrator")

	def test_the_api_delegates(self):
		from marketplace_integrations.api import bulk_delivery_notes as api

		with patch.object(bulk, "queue_bulk", return_value={"queued": True, "count": 1}) as queue:
			self.assertEqual(api.create_delivery_notes(["SO-1"], "Bosta"), {"queued": True, "count": 1})
		queue.assert_called_once_with(["SO-1"], "Bosta")

	# -- a staff user, not Administrator ----------------------------------------------------------

	def _as(self, email, function, *args):
		frappe.set_user(email)
		try:
			return function(*args)
		finally:
			frappe.set_user("Administrator")

	def test_a_staff_user_with_the_rights_can_ship_an_unpaid_order(self):
		email, _role = make_role_user(self, "Bulk Staff", STAFF_PERMISSIONS)
		order = self._order()

		result = self._as(email, bulk.process_order, order.name)

		self.assertEqual(result["status"], bulk.SENT, result)
		self.assertEqual(self.enqueue.call_args.kwargs["submit_as"], email)

	def test_the_users_own_invoice_rights_apply_even_though_the_mapper_ignores_permissions(self):
		perms = {**STAFF_PERMISSIONS, "Sales Invoice": ["read"]}
		email, _role = make_role_user(self, "Bulk No Invoice", perms)
		order = self._order(rate=555.5)
		self._pay(order)

		result = self._as(email, bulk.process_order, order.name)
		self._keep_invoices(order)

		self.assertEqual(result["status"], bulk.FAILED, result)
		self.assertIn("permission", result["message"])
		self.assertEqual(bulk._invoices(order.name, 1), [])
		self.assertIsNone(self._note_of(order))

	def test_a_document_level_user_permission_hides_the_order(self):
		email, _role = make_role_user(self, "Bulk Restricted", STAFF_PERMISSIONS)
		other = self._track(
			frappe.get_doc(
				{
					"doctype": "Customer",
					"customer_name": f"Bulk Other {self.suffix}",
					"customer_group": self.customer.customer_group,
					"territory": self.customer.territory,
				}
			).insert()
		)
		restriction = frappe.get_doc(
			{
				"doctype": "User Permission",
				"user": email,
				"allow": "Customer",
				"for_value": other.name,
				"apply_to_all_doctypes": 1,
			}
		).insert()
		self.addCleanup(
			lambda: (frappe.delete_doc("User Permission", restriction.name, force=True), frappe.db.commit())
		)
		frappe.db.commit()
		order = self._order()

		result = self._as(email, bulk.process_order, order.name)

		self.assertEqual(result["status"], bulk.SKIPPED, result)
		self.assertIn("do not have permission to read", result["message"])
		self.assertIsNone(self._note_of(order))
