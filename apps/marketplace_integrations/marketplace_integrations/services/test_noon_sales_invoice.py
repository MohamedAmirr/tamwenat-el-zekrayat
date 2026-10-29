# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Tests for the draft Sales Invoice of a Noon order (#377).  A real submitted Sales Order with
non-stock Items (``tests/handover_fixture.py``); no Noon call is made."""

from unittest.mock import patch

import frappe

from marketplace_integrations.integrations.noon import fulfillment
from marketplace_integrations.integrations.noon.test_shipped_units import (
	ACKNOWLEDGED,
	SHIPPED,
	IntegrationTestNoonShippedBase,
)
from marketplace_integrations.services import marketplace_attention, noon_sales_invoice
from marketplace_integrations.services.delivery_note_drafts import create_draft_delivery_note, submit_note
from marketplace_integrations.services.sales_order_reconciliation import (
	CANCEL_ORDER,
	hold_cancel_for_draft_documents,
)


class IntegrationTestNoonSalesInvoice(IntegrationTestNoonShippedBase):
	def _submitted_note(self, order_name, sku, qty):
		"""Draft a Delivery Note for ``qty`` units of ``sku`` and submit it."""
		order = frappe.get_doc("Marketplace Order", order_name)
		result = create_draft_delivery_note(order, [{"external_sku": sku, "qty": qty}], queue_sync=False)
		order.flags.skip_sync_status_update = True
		order.save()
		return submit_note(result["delivery_note"])

	def _draft_note(self, order_name, sku, qty):
		order = frappe.get_doc("Marketplace Order", order_name)
		result = create_draft_delivery_note(order, [{"external_sku": sku, "qty": qty}], queue_sync=False)
		order.flags.skip_sync_status_update = True
		order.save()
		return result["delivery_note"]

	def _invoice(self, order_name):
		return frappe.db.get_value("Marketplace Order", order_name, "noon_sales_invoice")

	def _quantities(self, invoice):
		return sorted((row.item_code, row.qty, row.delivery_note) for row in frappe.get_doc("Sales Invoice", invoice).items)

	def _cleanup_invoice(self, invoice):
		if invoice and frappe.db.exists("Sales Invoice", invoice):
			doc = frappe.get_doc("Sales Invoice", invoice)
			if doc.docstatus == 1:
				doc.cancel()
			frappe.delete_doc("Sales Invoice", invoice, force=True)

	def test_the_first_submitted_note_creates_one_draft_and_later_notes_extend_it(self):
		fx, order, _nr = self._shipped_order([("A", 3)], ["A", "A", "A"])
		first = self._submitted_note(order, "A", 1)
		noon_sales_invoice.sync_sales_invoice(order)
		invoice = self._invoice(order)
		self.addCleanup(self._cleanup_invoice, invoice)
		self.assertTrue(invoice)
		self.assertEqual(frappe.db.get_value("Sales Invoice", invoice, ["docstatus", "update_stock"]), (0, 0))
		self.assertEqual(self._quantities(invoice), [(fx.items["A"].name, 1, first)])

		noon_sales_invoice.sync_sales_invoice(order)  # a repeat trigger adds nothing
		self.assertEqual(self._quantities(invoice), [(fx.items["A"].name, 1, first)])

		second = self._submitted_note(order, "A", 2)
		noon_sales_invoice.sync_sales_invoice(order)
		self.assertEqual(self._invoice(order), invoice)  # still one invoice
		self.assertEqual(
			self._quantities(invoice), sorted([(fx.items["A"].name, 1, first), (fx.items["A"].name, 2, second)], key=str)
		)
		self.assertEqual(frappe.db.count("Sales Invoice Item", {"parent": invoice}), 2)  # no quantity billed twice
		noon_sales_invoice.sync_sales_invoice(order)
		self.assertEqual(frappe.db.count("Sales Invoice Item", {"parent": invoice}), 2)

	def test_a_draft_delivery_note_adds_nothing(self):
		fx, order, _nr = self._shipped_order([("A", 2)], ["A", "A"])
		self._draft_note(order, "A", 1)
		noon_sales_invoice.sync_sales_invoice(order)
		self.assertFalse(self._invoice(order))

	def test_two_runs_back_to_back_make_one_invoice(self):
		fx, order, _nr = self._shipped_order([("A", 1)], ["A"])
		self._submitted_note(order, "A", 1)
		noon_sales_invoice.sync_sales_invoice(order)
		noon_sales_invoice.sync_sales_invoice(order)
		invoice = self._invoice(order)
		self.addCleanup(self._cleanup_invoice, invoice)
		self.assertEqual(
			frappe.db.count("Sales Invoice Item", {"parent": ["in", [invoice]]}), 1
		)
		self.assertEqual(
			frappe.get_all("Sales Invoice Item", filters={"sales_order": fx.name}, pluck="parent", parent_doctype="Sales Invoice"),
			[invoice],
		)

	def test_a_submitted_invoice_gets_nothing_added_and_the_order_needs_attention_until_the_note_is_billed(self):
		fx, order, _nr = self._shipped_order([("A", 2)], ["A", "A"])
		first = self._submitted_note(order, "A", 1)
		noon_sales_invoice.sync_sales_invoice(order)
		invoice = self._invoice(order)
		self.addCleanup(self._cleanup_invoice, invoice)
		frappe.get_doc("Sales Invoice", invoice).submit()

		second = self._submitted_note(order, "A", 1)
		noon_sales_invoice.sync_sales_invoice(order)
		self.assertEqual(self._invoice(order), invoice)
		self.assertEqual(frappe.db.count("Sales Invoice Item", {"parent": invoice}), 1)
		self.assertEqual(noon_sales_invoice.uninvoiced_notes(fx.name), [second])
		self.assertIn(second, self._attention_text(order))

		from erpnext.stock.doctype.delivery_note.delivery_note import make_sales_invoice

		by_hand = make_sales_invoice(second)
		by_hand.insert()
		by_hand.submit()
		self.addCleanup(self._cleanup_invoice, by_hand.name)
		self.assertEqual(noon_sales_invoice.uninvoiced_notes(fx.name), [])
		self.assertNotIn(second, self._attention_text(order))
		self.assertTrue(first)

	def test_a_failure_is_stored_shown_and_retried(self):
		fx, order, _nr = self._shipped_order([("A", 1)], ["A"])
		self._submitted_note(order, "A", 1)
		with patch.object(noon_sales_invoice, "_create_or_extend", side_effect=frappe.ValidationError("<b>boom</b>")):
			noon_sales_invoice.sync_sales_invoice(order)
		error = frappe.db.get_value("Marketplace Order", order, "noon_sales_invoice_error")
		self.assertIn("boom", error)
		self.assertNotIn("<b>", error)  # stored escaped
		self.assertFalse(self._invoice(order))
		self.assertIn("boom", self._attention_text(order))

		with patch.object(fulfillment, "queue_sales_invoice") as queue:
			fulfillment.queue_shipped_units_if_needed(frappe.get_doc("Marketplace Order", order))
		queue.assert_called_once()  # the next import retries

		noon_sales_invoice.sync_sales_invoice(order)
		self.addCleanup(self._cleanup_invoice, self._invoice(order))
		self.assertTrue(self._invoice(order))
		self.assertFalse(frappe.db.get_value("Marketplace Order", order, "noon_sales_invoice_error"))

	def test_a_submitted_shipment_note_queues_the_invoice_and_a_draft_does_not(self):
		fx, order, nr = self._shipped_order([("A", 2)], ["A", "A"])
		self._set_statuses(order, {"1": SHIPPED, "2": ACKNOWLEDGED})
		with patch.object(fulfillment, "queue_sales_invoice") as queue:
			fulfillment.process_shipped_units(order)  # a draft note
		queue.assert_not_called()

		self._set_statuses(order, {"1": SHIPPED, "2": SHIPPED})
		with patch.object(fulfillment, "queue_sales_invoice") as queue:
			fulfillment.process_shipped_units(order)  # the last unit: submitted
		queue.assert_called_once_with(order, fx.name)

	def test_mark_handed_over_queues_the_invoice_after_the_submit(self):
		fx, order, nr = self._shipped_order([("A", 1)], ["A"])
		with patch.object(fulfillment, "queue_sales_invoice") as queue:
			fulfillment.mark_handed_over(order, nr)
		queue.assert_called_once_with(order, fx.name)

	def test_the_job_is_queued_once_per_sales_order_after_commit(self):
		with patch("marketplace_integrations.services.noon_sales_invoice.frappe.enqueue") as enqueue:
			noon_sales_invoice.queue_sales_invoice("MO-1", "SO-1")
		kwargs = enqueue.call_args.kwargs
		self.assertEqual(enqueue.call_args.args[0], noon_sales_invoice.sync_sales_invoice)
		self.assertTrue(kwargs["enqueue_after_commit"])
		self.assertTrue(kwargs["deduplicate"])
		self.assertEqual(kwargs["job_id"], "marketplace-noon-sales-invoice-SO-1")

	def test_a_draft_invoice_holds_a_marketplace_driven_cancel(self):
		fx, order, _nr = self._shipped_order([("A", 1)], ["A"])
		self._submitted_note(order, "A", 1)
		noon_sales_invoice.sync_sales_invoice(order)
		invoice = self._invoice(order)
		self.addCleanup(self._cleanup_invoice, invoice)
		held = hold_cancel_for_draft_documents({"outcome": CANCEL_ORDER, "changes": []}, fx.name)
		self.assertNotEqual(held["outcome"], CANCEL_ORDER)
		self.assertIn(invoice, held["message"])

	def _attention_text(self, order_name):
		rows = marketplace_attention.get_attention_items(
			{"account": self.account.name, "problem_type": marketplace_attention.NOON_FULFILLMENT}
		)
		return " ".join(row["explanation"] for row in rows if row["reference_name"] == order_name)
