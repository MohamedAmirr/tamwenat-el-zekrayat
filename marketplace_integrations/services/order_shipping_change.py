# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""A shipping change that comes from Shopify, applied to the SUBMITTED Sales Order (#534, epic #535).

Shopify's shipping charge can change after the order was imported (fast shipping chosen, a store pickup). The planner
(``sales_order_reconciliation.plan``) reports it as ``UPDATE_SHIPPING``; ``sales_order_sync._execute_shipping_change``
runs the steps below. ERPNext has no supported way to edit the tax rows of a submitted Sales Order, so the Sales
Order is changed by a direct, controlled update (the research is in the issue): the "Shipping" Actual row, then
``calculate_taxes_and_totals`` / ``set_total_in_words`` / ``set_payment_schedule`` / ``save`` with
``flags.ignore_validate_update_after_submit``. ERPNext's ``update_child_qty_rate`` is NOT called: the items and the
stock reservation are untouched.

The row has the shape ``sales_order_sync._apply_snapshot`` builds (Actual, description "Shipping", the account's
shipping income account). A shipping of 0 (store pickup) REMOVES the row, as the sync builds an order without
shipping (no row at 0).

Guards, in this order (the Sales Order is never half changed):

1. ``blocker``: a submitted Sales Invoice exists, or the parcel was picked up (a submitted Delivery Note): the order is
   not changed, the person gets a Needs Attention flag with the old and new shipping ("make a Credit Note / Debit
   Note"), and the baseline moves (the change is reported once, by the flag).
2. ``hold_reason`` (the planner) and ``cod_plan`` (the executor): a Bosta shipment that is Creating / Unknown, or a COD
   Bosta cannot take, holds the change (order unchanged, flagged, baseline kept; a later Retry Sync applies it).
3. A Created Bosta shipment first gets the new COD (``send_cod``; the #501 sequence of ``order_edit``), before the
   order changes. If the order cannot be changed afterwards the old COD is sent back (``restore_cod``).

``apply`` runs under the Sales Order and draft Delivery Note row locks, inside the caller's savepoint, and never
commits. The draft Delivery Note that ships the whole order gets the same shipping row (``order_edit.
refresh_draft_note``); a partial note carries no shipping charge and is left alone.
"""

import frappe
from frappe import _
from frappe.utils import cstr, flt, fmt_money

from marketplace_integrations.services import carrier_log, order_edit

DESCRIPTION = "Shipping"
_TOLERANCE = 0.005


class ShippingChangeBlocked(frappe.ValidationError):
	"""The order changed (an invoice or a pickup appeared) between the plan and the locked update."""


def shipping_amount(doc):
	"""The order's current shipping: the sum of its "Shipping" Actual rows."""
	return flt(
		sum(
			flt(row.tax_amount)
			for row in doc.get("taxes") or []
			if row.charge_type == "Actual" and cstr(row.description) == DESCRIPTION
		),
		2,
	)


def set_shipping_row(doc, amount, account):
	"""Make the "Shipping" Actual row of ``doc`` (a Sales Order or a Delivery Note, in memory) hold ``amount``: update
	it, add it (same shape as the sync) when there is none, remove it at 0. Returns whether anything changed. The caller
	recalculates the totals."""
	amount = flt(amount, 2)
	rows = [
		row for row in doc.get("taxes") or [] if row.charge_type == "Actual" and cstr(row.description) == DESCRIPTION
	]
	if amount <= 0:
		for row in rows:
			doc.remove(row)
		return bool(rows)
	if rows:
		row = rows[0]
		for extra in rows[1:]:
			doc.remove(extra)
		changed = len(rows) > 1 or flt(row.tax_amount, 2) != amount
		row.tax_amount = amount
		return changed
	if not account:
		frappe.throw(_("The Marketplace Account has no ERPNext Shipping Income Account, so a shipping charge cannot be added."))
	doc.append("taxes", shipping_row(amount, account))
	return True


def shipping_row(amount, account):
	"""The one charge row of a Shopify order's shipping (``sales_order_sync._apply_snapshot`` builds it with this too)."""
	return {
		"charge_type": "Actual",
		"account_head": account,
		"description": DESCRIPTION,
		"tax_amount": amount,
		"included_in_print_rate": 0,
	}


def blocker(sales_order):
	"""Why the Sales Order must not be changed (plain text for a message), or ``None``: a submitted Sales Invoice bills
	it, or the parcel was picked up (a submitted Delivery Note exists)."""
	invoices = frappe.get_all(
		"Sales Invoice Item",
		filters={"sales_order": sales_order, "docstatus": 1},
		pluck="parent",
		distinct=True,
		parent_doctype="Sales Invoice",
		limit_page_length=1,
	)
	if invoices:
		return _("Sales Invoice {0} already bills this order.").format(invoices[0])
	note = order_edit.picked_up_note(sales_order)
	if note:
		return _("The parcel was already picked up (Delivery Note {0}).").format(note)
	return None


def hold_reason(sales_order):
	"""Why Shopify's shipping change cannot be applied yet (plain text), or ``None``: ``order_edit.update_hold_reason``
	(two drafts or a partial draft Delivery Note, a Bosta shipment that is Creating or Unknown). An invoice or a pickup is
	NOT a hold: ``blocker`` handles it (order unchanged, flagged, baseline moves)."""
	if blocker(sales_order):
		return None
	return order_edit.update_hold_reason(sales_order)


def cod_plan(sales_order, new_shipping, account):
	"""``(plan, reasons)``: the Bosta COD update this shipping change needs (``order_edit.cod_plan``, computed on a
	copy of the order that is never saved; no network), and what forbids it. ``plan`` is ``None`` when there is no
	Bosta shipment to update. A Bosta note that does not ship the whole order carries no shipping charge: nothing to
	update, nothing forbidden."""
	reasons = []
	note = order_edit._bosta_note(sales_order.name)
	if not note or not order_edit._whole_order_note(note, sales_order):
		return None, reasons
	edit = frappe._dict(qty={}, add=[], shipping=(new_shipping, account))
	return order_edit.cod_plan(sales_order, edit, reasons), reasons


def send_cod(plan):
	"""Step 1 for a Created Bosta shipment: send the new COD. Commits (Bosta's update does). Returns ``None`` when Bosta
	took it, else the plain-text reason it did not (the order must then stay unchanged)."""
	from marketplace_integrations.integrations.bosta import editing

	result = editing.update_cod(plan.note, plan.new)
	if result.outcome == "updated":
		return None
	if result.outcome == "failed":
		return _("Bosta did not accept the new COD of Delivery Note {0}: {1}").format(plan.note, cstr(result.message))
	return _(
		"Could not confirm that Bosta updated the COD of Delivery Note {0}. It is listed in Needs Attention as Bosta COD "
		"Differs: Retry there puts the old COD back."
	).format(plan.note)


def restore_cod(plan):
	"""The order could not be changed after Bosta took the new COD: send the old COD back. Returns whether Bosta
	confirmed it; otherwise the difference stays listed as Bosta COD Differs with Retry, and the caller says so in its
	flag. An exception rolls the transaction back (the caller then locks the Marketplace Order again)."""
	from marketplace_integrations.integrations.bosta import editing

	try:
		return editing.update_cod(plan.note, plan.old).outcome == "updated"
	except Exception:
		frappe.db.rollback()
		frappe.log_error(title="Bosta COD could not be restored", message=f"{plan.note}\n{frappe.get_traceback()}")
		frappe.clear_messages()
		return False


def restore_failed_note(plan):
	"""The sentence added to a flag when ``restore_cod`` did not work."""
	return _(
		"Bosta still has the new COD of Delivery Note {0}. It is listed in Needs Attention as Bosta COD Differs: "
		"use Retry there to put {1} back."
	).format(plan.note, f"{flt(plan.old, 2):.2f}")


def apply(sales_order_name, new_shipping, account, cod=None):
	"""Change the Sales Order's shipping row and totals and the draft Delivery Note's (module docstring). Under the row
	locks (Sales Order, then the draft note, the order every writer takes); inside the caller's savepoint; never
	commits. ``cod``: the COD already sent to Bosta (written to the note). Raises ``ShippingChangeBlocked`` when an
	invoice or a pickup appeared since the plan, ``frappe.ValidationError`` when ERPNext refuses. Returns ``{"old",
	"new", "total", "advance_paid", "bosta_note"}`` (the order's total after the change and the started Bosta note, for
	the caller's flags)."""
	frappe.db.get_value("Sales Order", sales_order_name, "name", for_update=True)
	draft = order_edit.draft_note(sales_order_name)
	if draft:
		frappe.db.get_value("Delivery Note", draft, "name", for_update=True)
	reason = blocker(sales_order_name)
	if reason:
		raise ShippingChangeBlocked(reason)
	order = frappe.get_doc("Sales Order", sales_order_name)
	old = shipping_amount(order)
	bosta_note = order_edit._bosta_note(sales_order_name)
	set_shipping_row(order, new_shipping, account)
	order.flags.ignore_validate_update_after_submit = True
	order.calculate_taxes_and_totals()
	order.set_total_in_words()
	order.set_payment_schedule()
	order.save()
	if draft and not order_edit.draft_note_problem(sales_order_name):
		order_edit.refresh_draft_note(sales_order_name, cod=cod, shipping=(new_shipping, account))
	order.add_comment(
		"Comment",
		_("Shipping changed from {0} to {1}. Source: Shopify.").format(
			fmt_money(old, currency=order.currency), fmt_money(flt(new_shipping), currency=order.currency)
		),
	)
	order.reload()
	return {
		"currency": order.currency,
		"old": old,
		"new": flt(new_shipping, 2),
		"total": flt(order.rounded_total) or flt(order.grand_total),
		"advance_paid": flt(order.advance_paid),
		"bosta_note": bosta_note.name
		if bosta_note and cstr(bosta_note.custom_integration_status) != carrier_log.CANCELLED
		else None,
	}


def flags_after_apply(applied):
	"""The Needs Attention reasons (plain text, one line each) for a change that was applied: a total below what the
	customer paid (a refund is owed), a store pickup while a Bosta shipment is started."""
	reasons = []
	over = flt(applied["advance_paid"] - applied["total"], 2)
	if over > _TOLERANCE:
		reasons.append(_("Refund owed: {0}. Refund it in Shopify.").format(fmt_money(over, currency=applied["currency"])))
	if applied["new"] <= 0 < applied["old"] and applied["bosta_note"]:
		reasons.append(_("Bosta shipment started; cancel it if the customer picks up from the store."))
	return reasons


def blocked_reason(old, new, why, currency=None):
	"""The Needs Attention reason for a change that was NOT applied because of ``blocker`` (three sentences; ``why`` is
	a full sentence)."""
	return " ".join(
		[
			_("Shopify changed the shipping from {0} to {1}.").format(
				fmt_money(old, currency=currency), fmt_money(new, currency=currency)
			),
			cstr(why),
			_("The Sales Order was not changed. Make a Credit Note / Debit Note for the difference."),
		]
	)


def held_reason(old, new, why, currency=None):
	"""The Needs Attention reason for a change that was held (nothing changed; Retry Sync applies it later; ``why`` is a
	full sentence)."""
	return " ".join(
		[
			_("Shopify changed the shipping from {0} to {1}, but the change was not applied.").format(
				fmt_money(old, currency=currency), fmt_money(new, currency=currency)
			),
			cstr(why),
		]
	)
