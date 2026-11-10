# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Auto invoice on Bosta Delivered (#506). No Bosta call is made."""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import nowdate

from marketplace_integrations.integrations.bosta import auto_invoice
from marketplace_integrations.tests.bosta_fixtures import BostaFixtureMixin


class IntegrationTestAutoInvoice(BostaFixtureMixin, IntegrationTestCase):
	def _delivered_note(self):
		"""A submitted Bosta note that Bosta reports Delivered and nothing bills yet."""
		note = self.make_note(order=self.make_order(qty=1))
		frappe.db.set_value(
			"Delivery Note",
			note.name,
			{"custom_integration_status": "Created", "custom_carrier_delivery_id": "AI-1", "lr_no": "TRK-AI"},
		)
		note = self.submit_note(note.name)
		frappe.db.set_value("Delivery Note", note.name, "custom_shipping_status", "Delivered")
		self._clear_failure_rows(note.name)
		self.addCleanup(self._clear_failure_rows, note.name)
		return note

	def _clear_failure_rows(self, note_name):
		"""The failure report commits its ToDo and Error Log; the fixture does not remove them."""
		frappe.db.delete("ToDo", {"reference_type": "Delivery Note", "reference_name": note_name})
		frappe.db.delete("Error Log", {"method": ["like", f"%Auto invoice%{note_name}%"]})
		frappe.db.commit()

	def test_no_bosta_supplier_means_no_query_and_no_invoice(self):
		with (
			patch.object(auto_invoice, "bosta_supplier", return_value=None),
			patch.object(auto_invoice, "_candidate_notes") as candidates,
		):
			auto_invoice.run_invoice_delivered_notes()
		candidates.assert_not_called()

	def test_candidates_are_filtered_by_the_bosta_supplier(self):
		note = self._delivered_note()
		names = [row.name for row in auto_invoice._candidate_notes(self.supplier, nowdate())]
		self.assertIn(note.name, names)
		self.assertEqual(auto_invoice._candidate_notes("No Such Supplier", nowdate()), [])

	def test_a_note_with_an_invoice_line_does_not_take_a_slot(self):
		from erpnext.stock.doctype.delivery_note.delivery_note import make_sales_invoice

		note = self._delivered_note()
		invoice = make_sales_invoice(source_name=note.name)
		invoice.insert(ignore_permissions=True)  # a draft line counts: docstatus < 2
		names = [row.name for row in auto_invoice._candidate_notes(self.supplier, nowdate())]
		self.assertNotIn(note.name, names)

		# force: the test site may hold stale ledger rows under a reused invoice name
		frappe.delete_doc("Sales Invoice", invoice.name, force=True, ignore_permissions=True)
		names = [row.name for row in auto_invoice._candidate_notes(self.supplier, nowdate())]
		self.assertIn(note.name, names)

	def test_a_delivered_note_gets_one_submitted_invoice_and_no_second(self):
		note = self._delivered_note()
		row = frappe._dict(
			name=note.name,
			posting_date=note.posting_date,
			custom_bosta_last_event_at=None,
			custom_last_sync_at=None,
		)

		def invoices():
			return set(
				frappe.get_all(
					"Sales Invoice Item", filters={"delivery_note": note.name, "docstatus": 1}, pluck="parent"
				)
			)

		try:
			auto_invoice._invoice_note(row, nowdate())
			self.assertEqual(len(invoices()), 1)
			self.assertNotIn(
				note.name, [r.name for r in auto_invoice._candidate_notes(self.supplier, nowdate())]
			)
			auto_invoice._invoice_note(row, nowdate())  # already billed: no second invoice
			self.assertEqual(len(invoices()), 1)
		finally:
			# The job commits, so the invoice must go before the fixture can cancel the note.
			for name in invoices():
				frappe.get_doc("Sales Invoice", name).cancel()
				frappe.delete_doc("Sales Invoice", name, force=True, ignore_permissions=True)
			frappe.db.commit()

	def test_failure_opens_one_todo_with_the_stable_marker(self):
		note = self._delivered_note()
		frappe.get_doc(
			{"doctype": "User", "email": auto_invoice.FAILURE_TODO_ASSIGNEE, "first_name": "Assignee"}
		).insert(ignore_permissions=True, ignore_if_duplicate=True)

		auto_invoice._report_failure(note.name, "boom <b>bold</b>")
		auto_invoice._report_failure(note.name, "boom again")  # an open ToDo exists: nothing new

		todos = frappe.get_all(
			"ToDo",
			filters={"reference_type": "Delivery Note", "reference_name": note.name},
			fields=["description", "priority", "allocated_to"],
		)
		self.assertEqual(len(todos), 1)
		self.assertTrue(todos[0].description.startswith(auto_invoice.FAILURE_MARKER))
		self.assertEqual(todos[0].priority, "High")
		self.assertNotIn("boom", todos[0].description)  # the error text is in the Error Log only

	def test_a_missing_assignee_skips_the_todo_but_logs_and_never_raises(self):
		note = self._delivered_note()
		with patch.object(auto_invoice, "FAILURE_TODO_ASSIGNEE", "nobody-here@example.com"):
			auto_invoice._report_failure(note.name, "boom")
		self.assertFalse(
			frappe.db.exists("ToDo", {"reference_type": "Delivery Note", "reference_name": note.name})
		)
		self.assertTrue(
			frappe.db.exists("Error Log", {"method": ["like", f"%Auto invoice failed: {note.name}%"]})
		)

	def test_a_failing_todo_insert_is_logged_and_the_batch_continues(self):
		note = self._delivered_note()
		real_get_doc = frappe.get_doc

		def failing_for_todo(*args, **kwargs):
			if args and isinstance(args[0], dict) and args[0].get("doctype") == "ToDo":
				raise RuntimeError("insert failed")
			return real_get_doc(*args, **kwargs)

		with patch("frappe.get_doc", side_effect=failing_for_todo):
			auto_invoice._report_failure(note.name, "boom")  # must not raise

	def test_one_failing_note_does_not_stop_the_next(self):
		rows = [
			frappe._dict(
				name="A", posting_date=nowdate(), custom_bosta_last_event_at=None, custom_last_sync_at=None
			),
			frappe._dict(
				name="B", posting_date=nowdate(), custom_bosta_last_event_at=None, custom_last_sync_at=None
			),
		]
		seen = []

		def fake_invoice(note, today):
			seen.append(note.name)
			if note.name == "A":
				raise frappe.ValidationError("bad")

		with (
			patch.object(auto_invoice, "bosta_supplier", return_value="Bosta"),
			patch.object(auto_invoice, "_candidate_notes", return_value=rows),
			patch.object(auto_invoice, "_invoice_note", side_effect=fake_invoice),
			patch.object(auto_invoice, "_report_failure") as report,
		):
			auto_invoice.run_invoice_delivered_notes()
		self.assertEqual(seen, ["A", "B"])
		report.assert_called_once()
