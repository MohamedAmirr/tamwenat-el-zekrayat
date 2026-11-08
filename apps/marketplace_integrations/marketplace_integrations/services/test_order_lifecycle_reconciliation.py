# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Nightly lifecycle check (epic #502, #501): what the ``note_rows`` check flags, and the refunded-status filter."""

from types import SimpleNamespace

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.services import order_lifecycle_reconciliation as reconciliation


def order_row(name, qty, delivered=0, supplier=0, item="ITEM"):
	return SimpleNamespace(name=name, qty=qty, delivered_qty=delivered, delivered_by_supplier=supplier, item_code=item)


def note_row(order, detail, qty):
	return SimpleNamespace(against_sales_order=order, so_detail=detail, qty=qty)


class IntegrationTestNoteRowsCheck(IntegrationTestCase):
	def _problem(self, note_rows, rows):
		return reconciliation._note_rows_problem(SimpleNamespace(rows=note_rows), "SO-1", {r.name: r for r in rows})

	def test_a_note_that_ships_exactly_the_pending_quantities_is_fine(self):
		self.assertEqual(self._problem([note_row("SO-1", "R1", 3)], [order_row("R1", 5, delivered=2)]), "")

	def test_a_partial_note_is_not_a_mismatch(self):
		rows = [order_row("R1", 5), order_row("R2", 2)]
		self.assertEqual(self._problem([note_row("SO-1", "R1", 2)], rows), "")

	def test_rows_of_another_order_in_a_shared_note_are_not_judged(self):
		rows = [order_row("R1", 5)]
		self.assertEqual(self._problem([note_row("SO-1", "R1", 5), note_row("SO-2", "X9", 40)], rows), "")

	def test_a_row_that_ships_more_than_is_left_is_a_mismatch(self):
		problem = self._problem([note_row("SO-1", "R1", 6)], [order_row("R1", 5, item="A")])
		self.assertIn("A: 6 / 5", problem)

	def test_split_rows_of_one_order_row_are_summed(self):
		rows = [order_row("R1", 5)]
		self.assertEqual(self._problem([note_row("SO-1", "R1", 2), note_row("SO-1", "R1", 3)], rows), "")
		self.assertTrue(self._problem([note_row("SO-1", "R1", 3), note_row("SO-1", "R1", 3)], rows))

	def test_a_row_of_the_order_that_is_gone_is_a_mismatch(self):
		self.assertIn("R9", self._problem([note_row("SO-1", "R9", 1)], [order_row("R1", 5)]))


class IntegrationTestRefundedStatusFilter(IntegrationTestCase):
	def test_an_empty_financial_status_is_not_dropped_by_the_not_in_filter(self):
		"""A Marketplace Order with no financial status must stay in the check: Frappe wraps the column of a ``not in``
		filter in ifnull, so a NULL / empty status matches."""
		query = frappe.get_all(
			"Marketplace Order",
			filters={"external_financial_status": ["not in", list(reconciliation._REFUNDED_FINANCIAL_STATUSES)]},
			fields=["name"],
			run=False,
		)
		self.assertIn("ifnull", str(query).lower())
