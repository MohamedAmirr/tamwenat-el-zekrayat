# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""The Sales Order cancel cascade (``services/order_cancel_cascade.py``): blocks, partial results, the loop guard,
the stale-copy refresh, the Shopify push guards and the Noon job. Mock based: no carrier or platform is called."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.services import carrier_shipments
from marketplace_integrations.services import order_cancel_cascade as cascade


def _order(name="SO-1"):
	return SimpleNamespace(name=name, flags=frappe._dict())


class IntegrationTestCascadePlan(IntegrationTestCase):
	def _plan(self, notes=(), invoices=(), by_doctype=None, blocker=None, ignore=True):
		by_doctype = by_doctype or {}

		def get_all(doctype, **kwargs):
			return by_doctype.get(doctype, [])

		with (
			patch.object(cascade.order_edit, "_notes", return_value=list(notes)),
			patch.object(cascade, "_invoice_rows", return_value=list(invoices)),
			patch.object(frappe, "get_all", side_effect=get_all),
			patch.object(carrier_shipments, "invoice_blocker", return_value=blocker),
			patch.object(cascade, "_check_note"),
		):
			return cascade._plan(_order(), ignore)

	def _note(self, name="DN-1", docstatus=1, is_return=0):
		return frappe._dict(name=name, docstatus=docstatus, is_return=is_return, custom_integration_status="")

	def _invoice(self, name="SINV-1", is_return=0):
		return frappe._dict(name=name, docstatus=1, is_return=is_return)

	def test_a_clean_order_lists_its_invoices_and_notes(self):
		plan = self._plan(notes=[self._note()], invoices=[self._invoice()])
		self.assertEqual(plan, {"invoices": ["SINV-1"], "notes": ["DN-1"]})

	def test_a_return_delivery_note_blocks_before_anything_is_cancelled(self):
		with self.assertRaises(frappe.ValidationError) as caught:
			self._plan(notes=[self._note(), self._note("DN-R", is_return=1)])
		self.assertIn("DN-R", str(caught.exception))
		self.assertIn("Nothing was cancelled", str(caught.exception))

	def test_a_credit_note_blocks(self):
		with self.assertRaises(frappe.ValidationError) as caught:
			self._plan(invoices=[self._invoice(), self._invoice("SINV-CN", is_return=1)])
		self.assertIn("SINV-CN", str(caught.exception))

	def test_an_advance_payment_entry_on_the_order_blocks_and_is_named(self):
		with self.assertRaises(frappe.ValidationError) as caught:
			self._plan(by_doctype={"Payment Entry Reference": ["PE-1"]})
		self.assertIn("PE-1", str(caught.exception))
		self.assertIn("SO-1", str(caught.exception))

	def test_a_payment_entry_on_an_invoice_blocks_and_is_named(self):
		with self.assertRaises(frappe.ValidationError) as caught:
			self._plan(invoices=[self._invoice()], blocker=(carrier_shipments.PAYMENT_ENTRY, "PE-9"))
		self.assertIn("SINV-1", str(caught.exception))
		self.assertIn("PE-9", str(caught.exception))

	def test_a_paid_or_pos_invoice_blocks(self):
		with self.assertRaises(frappe.ValidationError) as caught:
			self._plan(invoices=[self._invoice()], blocker=(carrier_shipments.UNPAID, ""))
		self.assertIn("SINV-1", str(caught.exception))

	def test_an_invoice_line_that_belongs_to_nothing_of_this_order_blocks(self):
		rows = [frappe._dict(sales_order=None, delivery_note=None)]
		with self.assertRaises(frappe.ValidationError) as caught:
			self._plan(notes=[self._note()], invoices=[self._invoice()], by_doctype={"Sales Invoice Item": rows})
		self.assertIn("SINV-1", str(caught.exception))


class IntegrationTestCascadePickedUp(IntegrationTestCase):
	def _check(self, picked_up, status=carrier_shipments.CREATED):
		adapter = SimpleNamespace(SERVICE_NAME="Bosta", picked_up=lambda note: picked_up)
		note = SimpleNamespace(items=[frappe._dict(against_sales_order="SO-1")])
		with (
			patch.object(frappe, "get_doc", return_value=note),
			patch.object(carrier_shipments, "status_of", return_value=status),
			patch.object(carrier_shipments, "adapter_for", return_value=adapter),
		):
			cascade._check_note("DN-1", "SO-1", True)

	def test_a_picked_up_live_shipment_is_refused_up_front(self):
		with self.assertRaises(frappe.ValidationError) as caught:
			self._check(True)
		self.assertIn("already picked up", str(caught.exception))
		self.assertIn("Nothing was cancelled", str(caught.exception))

	def test_a_shipment_not_yet_picked_up_passes(self):
		self._check(False)

	def test_a_cancelled_shipment_is_not_refused_even_if_the_note_was_submitted(self):
		self._check(True, status=carrier_shipments.CANCELLED)


class IntegrationTestBostaPickedUp(IntegrationTestCase):
	"""``picked_up`` reads Bosta's own state, not the docstatus (prod has submitted notes still at "Pickup requested")."""

	def _picked_up(self, status):
		from marketplace_integrations.integrations.bosta import delivery_note_shipment as bosta

		return bosta.picked_up(frappe._dict(docstatus=1, custom_shipping_status=status))

	def test_states_before_the_courier_collects_the_parcel_are_not_picked_up(self):
		for status in ("Pickup requested", "Waiting for route", "Route Assigned", "Picking up from consignee", "Picking up"):
			self.assertFalse(self._picked_up(status), status)

	def test_collected_in_transit_and_delivered_states_are_picked_up(self):
		for status in ("Picked up from business", "Picked up", "Received at warehouse", "In transit between Hubs", "Delivered", "Lost"):
			self.assertTrue(self._picked_up(status), status)

	def test_an_empty_or_unknown_state_counts_as_picked_up_so_the_cancel_is_refused(self):
		for status in ("", None, "Archived", "Some new Bosta state"):
			self.assertTrue(self._picked_up(status), status)

	def test_a_submitted_note_at_pickup_requested_is_cancelled_at_bosta_first_not_refused(self):
		adapter = SimpleNamespace(SERVICE_NAME="Bosta", picked_up=lambda note: self._picked_up("Pickup requested"))
		note = SimpleNamespace(items=[frappe._dict(against_sales_order="SO-1")])
		with (
			patch.object(frappe, "get_doc", return_value=note),
			patch.object(carrier_shipments, "status_of", return_value=carrier_shipments.CREATED),
			patch.object(carrier_shipments, "adapter_for", return_value=adapter),
		):
			cascade._check_note("DN-1", "SO-1", True)  # no refusal


class IntegrationTestNoonPickedUp(IntegrationTestCase):
	"""#541: Noon's "Shipped" is not a pickup; the note's stage is (Ready for Pickup is cancelled at Noon first)."""

	def _check(self, stage, shipping="Shipped"):
		from marketplace_integrations.integrations.noon import delivery_note_shipment as noon

		adapter = SimpleNamespace(SERVICE_NAME="Noon", picked_up=noon.picked_up)
		# A plain object, not a ``frappe._dict``: ``note.items`` on a dict is the ``dict.items`` method.
		fields = {"workflow_state": stage, "custom_shipping_status": shipping}
		note = SimpleNamespace(items=[{"against_sales_order": "SO-1"}], get=lambda key, default=None: fields.get(key, default))
		with (
			patch.object(frappe, "get_doc", return_value=note),
			patch.object(carrier_shipments, "status_of", return_value=carrier_shipments.CREATED),
			patch.object(carrier_shipments, "adapter_for", return_value=adapter),
		):
			cascade._check_note("DN-1", "SO-1", True)

	def test_a_shipped_note_in_ready_for_pickup_is_not_refused(self):
		self._check("Ready for Pickup")

	def test_a_note_at_picked_up_or_later_is_refused_with_the_sales_return_message(self):
		for stage in ("Picked Up", "In Transit", "Delivered"):
			with self.assertRaises(frappe.ValidationError) as caught:
				self._check(stage)
			self.assertIn("Sales Return", str(caught.exception), stage)
			self.assertIn("Nothing was cancelled", str(caught.exception), stage)


class IntegrationTestCascadeApply(IntegrationTestCase):
	"""``_apply``: the order of the cancels and what the error reports after a carrier commit."""

	def _run(self, plan, outcomes, live=("DN-1", "DN-2")):
		docs = {}

		def get_doc(doctype, name=None):
			if (doctype, name) not in docs:
				docs[(doctype, name)] = MagicMock()
				docs[(doctype, name)].name = name
			return docs[(doctype, name)]

		adapter = SimpleNamespace(SERVICE_NAME="Bosta")
		statuses = {name: carrier_shipments.CREATED for name in live}
		with (
			patch.object(frappe.db, "commit"),
			patch.object(frappe.db, "get_value"),
			patch.object(frappe, "get_doc", side_effect=get_doc),
			patch.object(cascade, "_payment_problem", return_value=None),
			patch.object(carrier_shipments, "adapter_for", return_value=adapter),
			patch.object(carrier_shipments, "status_of", side_effect=lambda note: statuses.get(note.name, "")),
			patch.object(carrier_shipments, "cancel", side_effect=outcomes) as cancel,
		):
			try:
				cascade._apply(_order(), plan, True)
			finally:
				self.docs = docs
				self.cancel = cancel

	def test_invoices_are_cancelled_before_notes_and_a_refusal_names_only_committed_documents(self):
		outcomes = [{"outcome": "refused", "message": "too late", "status": "Created"}]
		with self.assertRaises(frappe.ValidationError) as caught:
			self._run({"invoices": ["SINV-1"], "notes": ["DN-1"]}, outcomes)
		message = str(caught.exception)
		self.assertIn("too late", message)
		# The invoice was cancelled before the carrier's own commit: it stays, and is named.
		self.assertIn("SINV-1", message)
		self.assertIn("Already cancelled", message)

	def test_a_refusal_with_nothing_cancelled_before_it_reports_no_partial_result(self):
		outcomes = [{"outcome": "refused", "message": "too late", "status": "Created"}]
		with self.assertRaises(frappe.ValidationError) as caught:
			self._run({"invoices": [], "notes": ["DN-1"]}, outcomes)
		self.assertNotIn("Already cancelled", str(caught.exception))

	def test_an_unknown_carrier_answer_uses_its_own_wording(self):
		outcomes = [{"outcome": "unknown", "message": "timeout", "status": "Cancellation Unknown"}]
		with self.assertRaises(frappe.ValidationError) as caught:
			self._run({"invoices": [], "notes": ["DN-1"]}, outcomes)
		self.assertIn("not known whether", str(caught.exception))
		self.assertNotIn("did not cancel", str(caught.exception))

	def test_the_cascade_flag_is_restored_after_a_failure(self):
		frappe.flags[cascade.CASCADE_FLAG] = None
		outcomes = [{"outcome": "refused", "message": "no", "status": "Created"}]
		with self.assertRaises(frappe.ValidationError):
			self._run({"invoices": [], "notes": ["DN-1"]}, outcomes)
		self.assertFalse(frappe.flags.get(cascade.CASCADE_FLAG))


class IntegrationTestCascadeRefresh(IntegrationTestCase):
	def test_stored_header_and_row_values_replace_the_stale_copy_but_not_the_cancel_fields(self):
		class Doc(SimpleNamespace):
			def set(self, key, value):
				setattr(self, key, value)

		row = Doc(name="ROW-1", delivered_qty=5, billed_amt=100)
		order = Doc(name="SO-1", docstatus=2, per_delivered=100, custom_shipping_status_summary='{"Old": 9}', items=[row])

		def get_value(doctype, name, fields, as_dict=False):
			return frappe._dict(per_delivered=0, custom_shipping_status_summary='{"Delivered": 1}')

		def get_all(doctype, **kwargs):
			return [frappe._dict(name="ROW-1", delivered_qty=0, billed_amt=0)]

		meta = SimpleNamespace(get_valid_columns=lambda: ["name", "docstatus", "per_delivered", "custom_shipping_status_summary", "delivered_qty", "billed_amt"])
		with (
			patch.object(frappe, "get_meta", return_value=meta),
			patch.object(frappe.db, "get_value", side_effect=get_value),
			patch.object(frappe, "get_all", side_effect=get_all),
		):
			cascade._refresh_in_memory(order)

		self.assertEqual(order.per_delivered, 0)
		self.assertEqual(order.custom_shipping_status_summary, '{"Delivered": 1}')
		self.assertEqual(row.delivered_qty, 0)
		self.assertEqual(row.billed_amt, 0)
		self.assertEqual(order.docstatus, 2)


class IntegrationTestShopifyPush(IntegrationTestCase):
	def _queue(self, flag=False, shopify=True, option=None):
		option = option or {"already_requested": False, "platform_cancelled": False, "state": "eligible", "reasons": [], "instruction": ""}
		doc = frappe._dict(name="SO-1", custom_marketplace_order="MO-1")
		frappe.flags[cascade.SKIP_PUSH_FLAG] = flag
		try:
			with (
				patch.object(cascade.order_edit, "is_shopify_order", return_value=shopify),
				patch.object(frappe, "get_doc", return_value=MagicMock()),
				patch.object(cascade.outbound, "get_platform_cancel_option", return_value=option),
				patch.object(frappe, "enqueue") as enqueue,
				patch.object(frappe, "msgprint") as msgprint,
			):
				cascade.queue_shopify_cancel(doc)
		finally:
			frappe.flags[cascade.SKIP_PUSH_FLAG] = False
		return enqueue, msgprint

	def test_an_eligible_shopify_order_queues_the_push(self):
		enqueue, _ = self._queue()
		enqueue.assert_called_once()

	def test_skip_push_flag_suppresses_the_push(self):
		enqueue, _ = self._queue(flag=True)
		enqueue.assert_not_called()

	def test_cancel_in_erpnext_only_suppresses_the_push_once_and_is_per_user_and_order(self):
		cascade.skip_push_on_next_cancel("SO-1")
		try:
			enqueue, _ = self._queue()
			enqueue.assert_not_called()
			# consumed: the next cancel of the same order pushes again
			enqueue, _ = self._queue()
			enqueue.assert_called_once()
			# another order's cancel is not affected
			cascade.skip_push_on_next_cancel("SO-OTHER")
			enqueue, _ = self._queue()
			enqueue.assert_called_once()
		finally:
			frappe.cache().delete_value(cascade._SKIP_PUSH_KEY.format(frappe.session.user, "SO-OTHER"))

	def test_a_cancel_refused_after_erpnext_only_forgets_the_choice_so_the_next_cancel_pushes(self):
		cascade.skip_push_on_next_cancel("SO-1")
		with patch.object(cascade, "_plan", side_effect=frappe.ValidationError("Payment Entry PE-1")):
			with self.assertRaises(frappe.ValidationError):
				cascade.cascade_cancel(frappe._dict(name="SO-1", flags=frappe._dict()))
		enqueue, _ = self._queue()
		enqueue.assert_called_once()

	def test_asking_again_forgets_a_choice_left_over_from_an_earlier_refused_cancel(self):
		cascade.skip_push_on_next_cancel("SO-1")
		doc = frappe._dict(name="SO-1", docstatus=1, custom_marketplace_order="MO-1")
		option = {"state": "eligible", "already_requested": False, "platform_cancelled": False}
		with (
			patch.object(frappe, "get_doc", return_value=doc),
			patch.object(cascade.order_edit, "is_shopify_order", return_value=True),
			patch.object(cascade.outbound, "get_platform_cancel_option", return_value=option),
		):
			cascade.shopify_push_choice("SO-1")
		enqueue, _ = self._queue()
		enqueue.assert_called_once()

	def test_the_form_asks_only_for_a_submitted_shopify_order_the_platform_would_still_cancel(self):
		def ask(docstatus=1, shopify=True, **option):
			doc = frappe._dict(name="SO-1", docstatus=docstatus, custom_marketplace_order="MO-1")
			values = {"state": "eligible", "already_requested": False, "platform_cancelled": False, **option}
			with (
				patch.object(frappe, "get_doc", return_value=doc),
				patch.object(cascade.order_edit, "is_shopify_order", return_value=shopify),
				patch.object(cascade.outbound, "get_platform_cancel_option", return_value=values),
			):
				return cascade.shopify_push_choice("SO-1")["ask"]

		self.assertTrue(ask())
		self.assertFalse(ask(docstatus=0))
		self.assertFalse(ask(shopify=False))
		self.assertFalse(ask(state="blocked"))
		self.assertFalse(ask(platform_cancelled=True))
		self.assertFalse(ask(already_requested=True))

	def test_other_platforms_queue_nothing(self):
		enqueue, _ = self._queue(shopify=False)
		enqueue.assert_not_called()

	def test_an_order_not_eligible_on_shopify_is_skipped_with_a_warning(self):
		option = {"already_requested": False, "platform_cancelled": False, "state": "blocked", "reasons": ["Fulfilled."], "instruction": ""}
		enqueue, msgprint = self._queue(option=option)
		enqueue.assert_not_called()
		msgprint.assert_called_once()

	def test_an_order_already_cancelled_on_shopify_is_skipped_silently(self):
		option = {"already_requested": False, "platform_cancelled": True, "state": "blocked", "reasons": [], "instruction": ""}
		enqueue, msgprint = self._queue(option=option)
		enqueue.assert_not_called()
		msgprint.assert_not_called()

	def test_the_inbound_reconciliation_cancel_runs_with_the_skip_flag_set(self):
		from marketplace_integrations.services import sales_order_reconciliation as reconciliation

		seen = {}
		order = MagicMock(status="Cancelled")
		order.name = "SO-1"
		order.cancel.side_effect = lambda: seen.setdefault("flag", frappe.flags.get(cascade.SKIP_PUSH_FLAG))
		with patch.object(frappe, "get_doc", return_value=order):
			reconciliation.execute({"outcome": reconciliation.CANCEL_ORDER}, "SO-1")
		self.assertTrue(seen["flag"])
		self.assertFalse(frappe.flags.get(cascade.SKIP_PUSH_FLAG))

	def test_a_not_eligible_order_in_the_job_is_not_flagged_for_retry(self):
		option = {"already_requested": False, "platform_cancelled": False, "state": "blocked", "reasons": ["Fulfilled."], "instruction": ""}
		with (
			patch.object(frappe, "set_user"),
			patch.object(frappe, "get_doc", return_value=MagicMock()),
			patch.object(frappe.db, "commit"),
			patch.object(cascade.outbound, "get_platform_cancel_option", return_value=option),
			patch.object(cascade.outbound, "request_platform_cancel") as push,
			patch.object(cascade, "_flag_for_retry") as flag,
		):
			cascade.run_shopify_cancel("SO-1", "Administrator")
		push.assert_not_called()
		flag.assert_not_called()

	def test_a_failed_push_is_flagged_for_retry_and_never_raised(self):
		option = {"already_requested": False, "platform_cancelled": False, "state": "eligible", "reasons": [], "instruction": ""}
		with (
			patch.object(frappe, "set_user"),
			patch.object(frappe.db, "rollback"),
			patch.object(cascade.outbound, "get_platform_cancel_option", return_value=option),
			patch.object(cascade.outbound, "request_platform_cancel", side_effect=frappe.ValidationError("Shopify said no")),
			patch.object(cascade, "_flag_for_retry") as flag,
		):
			cascade.run_shopify_cancel("SO-1", "Administrator")
		flag.assert_called_once()
		self.assertIn("Shopify said no", flag.call_args.args[1])


class IntegrationTestNoonOrderCancel(IntegrationTestCase):
	def _note(self, carrier=True, is_return=0):
		note = frappe._dict(name="DN-1", is_return=is_return)
		note["items"] = [frappe._dict(against_sales_order="SO-1")]
		note["flags"] = frappe._dict(cancelled_by_carrier=carrier)
		return note

	def _queue(self, note, running=None, platform="Noon", docstatus=1):
		frappe.flags[cascade.CASCADE_FLAG] = running
		try:
			with (
				patch.object(frappe.db, "get_value", return_value=frappe._dict(docstatus=docstatus, custom_marketplace_order="MO-1")),
				patch.object(frappe, "get_cached_value", return_value=platform),
				patch.object(frappe, "enqueue") as enqueue,
			):
				cascade.queue_noon_order_cancel(note)
		finally:
			frappe.flags[cascade.CASCADE_FLAG] = None
		return enqueue

	def test_a_noon_cancel_started_by_noon_queues_the_order_cancel(self):
		enqueue = self._queue(self._note())
		enqueue.assert_called_once()
		self.assertEqual(enqueue.call_args.kwargs["sales_order"], "SO-1")
		# always the system, whoever's session cancelled the note (Confirm Returned runs as a Noon User)
		self.assertEqual(enqueue.call_args.kwargs["requested_by"], "Administrator")

	def test_a_persons_cancel_of_a_noon_note_leaves_the_order_alone(self):
		self._queue(self._note(carrier=False)).assert_not_called()

	def test_a_return_note_leaves_the_order_alone(self):
		self._queue(self._note(is_return=1)).assert_not_called()

	def test_other_platforms_leave_the_order_alone(self):
		self._queue(self._note(), platform="Shopify").assert_not_called()

	def test_loop_guard_the_cascade_does_not_queue_the_order_again(self):
		self._queue(self._note(), running={"SO-1"}).assert_not_called()

	def test_the_job_exits_when_the_order_is_no_longer_submitted(self):
		with (
			patch.object(frappe, "set_user"),
			patch.object(frappe.db, "commit"),
			patch.object(frappe.db, "get_value", return_value=2),
			patch.object(frappe, "get_doc") as get_doc,
		):
			cascade.run_noon_order_cancel("SO-1", "DN-1", "Administrator")
		get_doc.assert_not_called()

	def test_the_job_leaves_an_order_noon_has_not_fully_cancelled_to_the_order_sync(self):
		with (
			patch.object(frappe, "set_user"),
			patch.object(frappe.db, "commit"),
			patch.object(frappe.db, "get_value", side_effect=[1, 2]),
			patch.object(cascade, "_noon_cancelled_the_order", return_value=False),
			patch.object(frappe, "get_doc") as get_doc,
		):
			cascade.run_noon_order_cancel("SO-1", "DN-1", "Administrator")
		get_doc.assert_not_called()

	def test_the_job_runs_as_the_user_it_was_queued_with_and_sets_no_permission_bypass_flag(self):
		order = MagicMock()
		order.flags = frappe._dict()
		with (
			patch.object(frappe, "set_user") as set_user,
			patch.object(frappe.db, "commit"),
			patch.object(frappe.db, "get_value", side_effect=[1, 2]),
			patch.object(cascade, "_noon_cancelled_the_order", return_value=True),
			patch.object(frappe, "get_doc", return_value=order),
		):
			cascade.run_noon_order_cancel("SO-1", "DN-1", "stock@example.com")
		set_user.assert_called_once_with("stock@example.com")
		order.cancel.assert_called_once()
		self.assertFalse(order.flags.get("ignore_permissions"))

	def test_a_blocked_order_cancel_is_noted_and_notified_and_never_raised(self):
		order = MagicMock()
		order.flags = frappe._dict()
		order.cancel.side_effect = frappe.ValidationError("Sales Invoice SINV-1 has Payment Entry PE-1")
		with (
			patch.object(frappe, "set_user"),
			patch.object(frappe.db, "commit"),
			patch.object(frappe.db, "rollback"),
			patch.object(frappe.db, "get_value", side_effect=[1, 2]),
			patch.object(cascade, "_noon_cancelled_the_order", return_value=True),
			patch.object(frappe, "get_doc", return_value=order),
			patch.object(carrier_shipments, "notify_stock_managers") as notify,
		):
			cascade.run_noon_order_cancel("SO-1", "DN-1", "Administrator")
		notify.assert_called_once()
		self.assertIn("PE-1", notify.call_args.args[1])
		order.add_comment.assert_called()
