# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Delivery stage and payment of the Sales Orders on one list page (Sales Order list Status and Payment columns).

The list cannot tell from the Sales Order row alone whether a (draft) Delivery Note exists, how far it is, or
whether an invoice is paid. ``get_states`` answers for a whole page in a fixed number of queries (Sales Order
Items, Delivery Note Items, Delivery Notes, Sales Invoice Items, Sales Invoices), never one query per row. Nothing is
stored: the answer is derived every time, so it cannot go stale.

Delivery stage of an order = the LEAST advanced stage of its live (not cancelled, not return) Delivery Notes. The
stage of a note comes from ``delivery_note_workflow.state_of_row``, the one mapping of carrier statuses to stages
(Bosta's table lives in ``integrations/bosta/statuses.py``); a submitted note with no carrier counts as Delivered
there. A problem is shown only when the NEWEST live note (by creation) is the problem one: a returned or lost note
(``problem``) or a note Bosta reports with an exception code (``attempt_failed``). An older problem note that a newer
note replaced is ignored. Talabat and Noon orders are paid by the marketplace, which collects the money. Quantity the live notes (drafts
included) do not cover counts as not prepared. An order with no note that ERPNext already shows as fully delivered
(``per_delivered`` 100: an update-stock invoice, a drop ship) is Delivered.
"""

import frappe
from frappe import _
from frappe.utils import flt

from marketplace_integrations.services import delivery_note_workflow as lifecycle

MAX_ORDERS = 500

NOT_PREPARED = "not_prepared"
WAITING_FOR_GOODS = "waiting_for_goods"
READY = "ready"
ON_THE_WAY = "on_the_way"
DELIVERED = "delivered"
PROBLEM = "problem"
ATTEMPT_FAILED = "attempt_failed"
PROBLEM_STAGES = (PROBLEM, ATTEMPT_FAILED)

PAID_IN_ADVANCE = "advance"
PAID_BY_INVOICE = "invoice"
PAID_BY_MARKETPLACE = "marketplace"
PAY_ON_DELIVERY = "cod"

#: Platforms whose marketplace collects the customer's money (decided 2026-11-10): their orders are paid.
MARKETPLACE_COLLECTS = ("Talabat", "Noon")

_RANK = {NOT_PREPARED: 0, WAITING_FOR_GOODS: 1, READY: 2, ON_THE_WAY: 3, DELIVERED: 4}
_STAGE_OF_NOTE_STATE = {
	lifecycle.WAITING_FOR_GOODS: WAITING_FOR_GOODS,
	lifecycle.ORDER_RECEIVED: READY,
	lifecycle.SHIPMENT_REQUESTED: READY,
	lifecycle.SHIPMENT_CREATED: READY,
	lifecycle.PICKED_UP: ON_THE_WAY,
	lifecycle.IN_TRANSIT: ON_THE_WAY,
	lifecycle.DELIVERED: DELIVERED,
	lifecycle.RETURNED_FAILED: PROBLEM,
}
#: An amount within this of the target counts as reached (rounding).
_TOLERANCE = 0.005
_QTY_TOLERANCE = 0.0001


def stage_of_note(note_row):
	"""The list stage of one Delivery Note row (dict with docstatus, is_return, the two statuses, workflow_state).
	A submitted note Bosta reports with an exception code is ``attempt_failed`` (a failed delivery attempt), not
	on the way."""
	from marketplace_integrations.integrations.bosta import statuses

	state = lifecycle.state_of_row(note_row)
	if state == lifecycle.IN_TRANSIT and note_row.get("custom_shipping_status") in [
		statuses.BOSTA_STATE_NAMES[code] for code in statuses.EXCEPTION_CODES
	]:
		return ATTEMPT_FAILED
	return _STAGE_OF_NOTE_STATE.get(state, READY)


def combine_stages(note_stages, per_delivered, covered_qty=None, order_qty=None):
	"""The stage of an order from the stages of its live notes, OLDEST FIRST (``[]`` = no note).

	A problem stage counts only when the newest note has it; older problem notes are dropped.

	``covered_qty`` / ``order_qty``: the summed Delivery Note Item quantity of the live notes (drafts included) and
	the order's quantity. Left out (``None``) means "do not check coverage". Quantity left uncovered is not
	prepared, unless ERPNext already shows the order fully delivered."""
	fully_delivered = flt(per_delivered) >= 100 - _TOLERANCE
	if not note_stages:
		return DELIVERED if fully_delivered else NOT_PREPARED
	if note_stages[-1] in PROBLEM_STAGES:
		return note_stages[-1]
	live = [stage for stage in note_stages if stage not in PROBLEM_STAGES]
	if covered_qty is not None and order_qty is not None and not fully_delivered:
		if flt(covered_qty) < flt(order_qty) - _QTY_TOLERANCE:
			return NOT_PREPARED
	return min(live, key=_RANK.__getitem__)


def payment_kind(order, invoice_outstanding):
	"""``order``: custom_marketplace_platform, grand_total, rounded_total, advance_paid, per_billed.
	``invoice_outstanding``: the outstanding amounts of the order's submitted, non-return Sales Invoices. Paid by
	the marketplace for a platform that collects the money (``MARKETPLACE_COLLECTS``); else paid in advance when
	the advance covers the total (the rounded total when there is one: ERPNext asks for that amount); paid by
	invoice when the order is fully billed by a submitted invoice; otherwise pay on delivery.

	INTERIM (decided 2026-11-10): an invoice counts as paid EVEN WHEN IT STILL HAS OUTSTANDING. Until the accounting
	"Option A" exists (the Bosta COD receivable is recorded with a Payment Entry at delivery), a Bosta order is
	invoiced when delivered while the carrier still holds the cash, so the outstanding amount cannot mean "unpaid".
	When Option A is built, bring the check back: ``all(flt(amount) <= _TOLERANCE for amount in
	invoice_outstanding)`` (the outstanding amounts are still fetched and passed in for that)."""
	if order.get("custom_marketplace_platform") in MARKETPLACE_COLLECTS:
		return PAID_BY_MARKETPLACE
	total = flt(order.get("rounded_total")) or flt(order.get("grand_total"))
	if total > 0 and flt(order.get("advance_paid")) >= total - _TOLERANCE:
		return PAID_IN_ADVANCE
	# INTERIM: no "nothing outstanding" condition here (see the docstring); the list is non-empty = a submitted,
	# non-return invoice exists.
	if invoice_outstanding and flt(order.get("per_billed")) >= 100 - _TOLERANCE:
		return PAID_BY_INVOICE
	return PAY_ON_DELIVERY


def get_states(names):
	"""``{sales_order: {"stage": ..., "payment": ...}}`` for the Sales Orders in ``names`` that the user may read.
	Orders the user cannot read are left out, so the answer reveals nothing about them."""
	names = list(dict.fromkeys(str(name) for name in (names or []) if name))
	if len(names) > MAX_ORDERS:
		frappe.throw(_("Too many orders were asked for at once. Ask for at most {0}.").format(MAX_ORDERS))
	if not names:
		return {}
	orders = frappe.get_list(
		"Sales Order",
		filters={"name": ["in", names]},
		fields=[
			"name",
			"custom_marketplace_platform",
			"grand_total",
			"rounded_total",
			"advance_paid",
			"per_delivered",
			"per_billed",
		],
		limit_page_length=0,
	)
	allowed = [order.name for order in orders]
	if not allowed:
		return {}
	# Permission was checked on the Sales Orders above; the notes and invoices are read for those orders only
	# and only turned into a stage and a paid flag.
	order_qty = _order_quantities(allowed)
	notes_of = _notes_by_order(allowed)
	outstanding_of = _invoice_outstanding_by_order(allowed)
	result = {}
	for order in orders:
		stages, covered = notes_of.get(order.name, ([], 0))
		result[order.name] = {
			"stage": combine_stages(stages, order.per_delivered, covered, order_qty.get(order.name)),
			"payment": payment_kind(order, outstanding_of.get(order.name, [])),
		}
	return result


def _order_quantities(orders):
	"""``{sales_order: summed item quantity}``."""
	totals = {}
	for row in frappe.get_all(
		"Sales Order Item",
		filters={"parent": ["in", orders], "parenttype": "Sales Order"},
		fields=["parent", "qty"],
		limit_page_length=0,
		parent_doctype="Sales Order",
	):
		totals[row.parent] = totals.get(row.parent, 0) + flt(row.qty)
	return totals


def _notes_by_order(orders):
	"""``{sales_order: ([stage of each live note, oldest first], summed quantity of those notes' rows)}``."""
	links = frappe.get_all(
		"Delivery Note Item",
		filters={"against_sales_order": ["in", orders], "parenttype": "Delivery Note"},
		fields=["against_sales_order", "parent", "qty"],
		limit_page_length=0,
		parent_doctype="Delivery Note",
	)
	if not links:
		return {}
	fields = [
		"name",
		"creation",
		"docstatus",
		"is_return",
		"custom_integration_status",
		"custom_shipping_status",
	]
	if lifecycle.is_installed():
		fields.append(lifecycle.FIELD)
	rows = {
		row.name: row
		for row in frappe.get_all(
			"Delivery Note",
			filters={
				"name": ["in", list({link.parent for link in links})],
				"docstatus": ["<", 2],
				"is_return": 0,
			},
			fields=fields,
			limit_page_length=0,
		)
	}
	notes = {}
	covered = {}
	for link in links:
		row = rows.get(link.parent)
		if not row:
			continue
		notes.setdefault(link.against_sales_order, {})[link.parent] = (
			row.creation,
			row.name,
			stage_of_note(row),
		)
		covered[link.against_sales_order] = covered.get(link.against_sales_order, 0) + flt(link.qty)
	# oldest first: the newest note decides whether a problem is shown
	return {
		order: ([stage for _created, _name, stage in sorted(found.values())], covered[order])
		for order, found in notes.items()
	}


def _invoice_outstanding_by_order(orders):
	links = frappe.get_all(
		"Sales Invoice Item",
		filters={"sales_order": ["in", orders], "parenttype": "Sales Invoice"},
		fields=["sales_order", "parent"],
		distinct=True,
		limit_page_length=0,
		parent_doctype="Sales Invoice",
	)
	if not links:
		return {}
	rows = {
		row.name: row
		for row in frappe.get_all(
			"Sales Invoice",
			filters={"name": ["in", list({link.parent for link in links})], "docstatus": 1, "is_return": 0},
			fields=["name", "outstanding_amount"],
			limit_page_length=0,
		)
	}
	result = {}
	for link in links:
		row = rows.get(link.parent)
		if row:
			result.setdefault(link.sales_order, []).append(flt(row.outstanding_amount))
	return result
