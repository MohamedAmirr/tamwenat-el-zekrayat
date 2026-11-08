# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Cancel a Shopify order before the carrier picked it up (epic #502, issue #501).

One chain, in this order, each step committed before the next:

1. ``carrier``: the draft Delivery Note's shipment is cancelled at the carrier (Bosta terminate,
   ``carrier_shipments.cancel``). Skipped when there is no draft note or its shipment never started / is cancelled.
2. ``delivery_note``: the draft Delivery Note (if any) is deleted, with a comment on the Sales Order that names it and
   its tracking number. The native delete is allowed only because step 1 left no live shipment.
3. ``sales_order``: the Sales Order is cancelled (ERPNext releases its stock reservation).
4. ``platform``: ``marketplace_outbound.request_platform_cancel`` cancels the order at Shopify.

Every step re-reads the real documents, so the chain is resumable: a failure STOPS it, leaves the earlier steps as
they are, and the answer names the failed step and the reason; calling it again continues with that step (a step
already done is skipped). Nothing is undone automatically. Before the first irreversible step (only while the Sales
Order is still submitted) the whole chain is checked: a submitted Delivery Note means the carrier has the parcel, so
the order follows the return flow instead (a Sales Return against the note); a submitted Sales Invoice or a Payment
Entry linked to the order is refused (ERPNext would stop the Sales Order cancel only after the shipment was
terminated and the note deleted); the permissions of every step (cancel on the Sales Order, write and delete on each
draft note) are checked; and Shopify's own cancel rules (``get_platform_cancel_option``) are read so a cancel
Shopify will refuse does not terminate the shipment first.
Automatic refunds are out of scope.

Every note is handled the same whether it was made on the Delivery Note form or by the bulk path (both make a draft
Delivery Note of the order); the chain reads the notes, not how they were made.
"""

import html

import frappe
from frappe import _
from frappe.utils import cstr, strip_html_tags

from marketplace_integrations.services import carrier_log, order_edit
from marketplace_integrations.services import marketplace_outbound as outbound

CARRIER = "carrier"
DELIVERY_NOTE = "delivery_note"
SALES_ORDER = "sales_order"
PLATFORM = "platform"


def step_labels():
	return {
		CARRIER: _("Cancel the shipment at the carrier"),
		DELIVERY_NOTE: _("Delete the draft Delivery Note"),
		SALES_ORDER: _("Cancel the Sales Order"),
		PLATFORM: _("Cancel the order on Shopify"),
	}


class Stopped(Exception):
	"""A step cannot go on (a plain-text reason)."""


def _plain(exc):
	"""Plain text of a message (Frappe messages are HTML; the client escapes the text where it shows it)."""
	return html.unescape(strip_html_tags(cstr(exc))).strip()[:500] or exc.__class__.__name__


def cancel_order(sales_order, cancellation_reason=""):
	"""Run the chain for a Shopify Sales Order (the API has checked the permissions). Returns ``{"ok", "completed":
	[step], "completed_labels", "stopped": None | {"step", "label", "message"}}``; raises only for a refusal before
	any step ran. A step that had nothing to do counts as completed."""
	order = frappe.get_doc("Sales Order", sales_order)
	if not order_edit.is_shopify_order(order.get("custom_marketplace_order")):
		frappe.throw(_("Sales Order {0} is not linked to a Shopify order.").format(order.name))
	if order.docstatus == 1:
		_preflight(order)
	completed = []
	for step, run in (
		(CARRIER, _cancel_shipment),
		(DELIVERY_NOTE, _delete_draft_note),
		(SALES_ORDER, _cancel_sales_order),
		(PLATFORM, lambda name: _cancel_on_platform(name, cancellation_reason)),
	):
		frappe.db.commit()
		try:
			run(order.name)
		except Exception as exc:
			frappe.db.rollback()
			if not isinstance(exc, (Stopped, frappe.ValidationError, frappe.PermissionError)):
				frappe.log_error(title="Order cancel chain step failed", message=f"{order.name} {step}\n{frappe.get_traceback()}")
			frappe.clear_messages()
			return {
				"ok": False,
				"completed": completed,
				"completed_labels": [step_labels()[done] for done in completed],
				"stopped": {"step": step, "label": step_labels()[step], "message": _plain(exc)},
			}
		frappe.db.commit()
		completed.append(step)
	return {"ok": True, "completed": completed, "completed_labels": [step_labels()[done] for done in completed], "stopped": None}


def _preflight(order):
	"""Refuse, before step 1 (the first irreversible one), whatever would stop a later step: a submitted note, a
	submitted Sales Invoice or a Payment Entry linked to the order (ERPNext would refuse or leave them half done at
	the Sales Order cancel, after the shipment was terminated and the note deleted), a missing permission on any
	step's document, and a cancel Shopify would refuse."""
	submitted = order_edit.picked_up_note(order.name)
	if submitted:
		frappe.throw(
			_(
				"The carrier already has this order (Delivery Note {0}). It can no longer be cancelled here: "
				"create a Sales Return against the Delivery Note."
			).format(submitted)
		)
	_check_linked_documents(order.name)
	_check_permissions(order)
	_check_draft_note_deletable(order)
	option = outbound.get_platform_cancel_option(order)
	if option["state"] != outbound.ELIGIBLE and not (option["already_requested"] or option["platform_cancelled"]):
		frappe.throw(
			_("The order cannot be cancelled on Shopify: {0} Nothing was cancelled.").format(
				" ".join(option["reasons"]) or option["instruction"]
			)
		)


def _check_linked_documents(sales_order):
	"""Refuse while a submitted Sales Invoice bills the order or a submitted Payment Entry references it (the same
	documents ``carrier_shipments._cancel_linked_sales_invoices`` and ``_check_native_cancel_not_blocked`` guard for a
	Delivery Note; here nothing is cancelled for the person, who handles them first)."""
	invoices = frappe.get_all(
		"Sales Invoice Item",
		filters={"sales_order": sales_order, "docstatus": 1, "parenttype": "Sales Invoice"},
		pluck="parent",
		distinct=True,
		limit=1,
	)
	if invoices:
		frappe.throw(
			_("Sales Invoice {0} is submitted for this order. Cancel the invoice first. Nothing was cancelled.").format(
				invoices[0]
			)
		)
	payments = frappe.get_all(
		"Payment Entry Reference",
		filters={"reference_doctype": "Sales Order", "reference_name": sales_order, "docstatus": 1},
		pluck="parent",
		limit=1,
	)
	if payments:
		frappe.throw(
			_(
				"Payment Entry {0} is linked to this order. Cancel or unlink it first. Nothing was cancelled."
			).format(payments[0])
		)


def _check_permissions(order):
	"""Every step's permission up front, so no step runs when a later one would be refused: cancel on the Sales
	Order, write (the carrier cancel) and delete (step 2) on each draft Delivery Note."""
	frappe.has_permission("Sales Order", "cancel", doc=order, throw=True)
	for note in _draft_notes(order.name):
		frappe.has_permission("Delivery Note", "write", doc=note.name, throw=True)
		frappe.has_permission("Delivery Note", "delete", doc=note.name, throw=True)


def _check_draft_note_deletable(order):
	"""Refuse, before the carrier is touched, a draft note that step 2 would not delete: one that holds items of
	another order, or one a draft document is linked to (the native delete refuses it, after the shipment was
	terminated)."""
	from frappe.model.delete_doc import get_linked_docs

	for note in _draft_notes(order.name):
		doc = frappe.get_doc("Delivery Note", note.name)
		if {row.against_sales_order for row in doc.items} - {order.name}:
			frappe.throw(
				_("Delivery Note {0} also holds items of other orders, so it is not deleted. Nothing was cancelled.").format(
					doc.name
				)
			)
		links = get_linked_docs(doc, "Delete")
		if links:
			frappe.throw(
				_("Delivery Note {0} is linked with {1} {2}, so it cannot be deleted. Nothing was cancelled.").format(
					doc.name, _(links[0]["reference_doctype"]), links[0]["reference_docname"]
				)
			)


def _draft_notes(sales_order):
	return [
		note
		for note in order_edit._notes(sales_order)
		if note.docstatus == 0 and not note.is_return
	]


def _cancel_shipment(sales_order):
	"""Step 1."""
	from marketplace_integrations.services import carrier_shipments

	for note in _draft_notes(sales_order):
		status = cstr(note.custom_integration_status)
		if status in (carrier_log.CREATING, carrier_log.UNKNOWN):
			raise Stopped(
				_(
					"The shipment of Delivery Note {0} has an unknown result. Check it with the carrier on the Delivery Note first."
				).format(note.name)
			)
		if status not in (carrier_log.CREATED, carrier_log.CANCELLATION_PENDING, carrier_log.CANCELLATION_UNKNOWN):
			continue
		frappe.has_permission("Delivery Note", "write", doc=note.name, throw=True)
		result = carrier_shipments.cancel(note.name)
		if result["outcome"] != "cancelled":
			message = _plain(result["message"]) if result.get("message") else ""
			raise Stopped(
				_("The carrier did not cancel the shipment of Delivery Note {0}: {1}").format(
					note.name, message or _("The carrier gave no reason.")
				)
			)


def _delete_draft_note(sales_order):
	"""Step 2: delete the draft note; the Sales Order keeps a comment as the record."""
	for note in _draft_notes(sales_order):
		doc = frappe.get_doc("Delivery Note", note.name)
		if {row.against_sales_order for row in doc.items} - {sales_order}:
			raise Stopped(_("Delivery Note {0} also holds items of other orders, so it is not deleted.").format(doc.name))
		tracking = cstr(doc.lr_no).strip()
		frappe.has_permission("Delivery Note", "delete", doc=doc, throw=True)
		frappe.delete_doc("Delivery Note", doc.name)
		frappe.get_doc("Sales Order", sales_order).add_comment(
			"Comment",
			_("Draft Delivery Note {0} was deleted when the order was cancelled. Carrier tracking number: {1}.").format(
				doc.name, tracking or "-"
			),
		)


def _cancel_sales_order(sales_order):
	"""Step 3."""
	doc = frappe.get_doc("Sales Order", sales_order)
	if doc.docstatus == 2:
		return
	if doc.docstatus != 1:
		raise Stopped(_("Sales Order {0} is not submitted.").format(sales_order))
	doc.cancel()


def _cancel_on_platform(sales_order, cancellation_reason):
	"""Step 4. ``request_platform_cancel`` commits (it must never run inside the Sales Order cancel transaction,
	which step 3 has committed)."""
	option = outbound.get_platform_cancel_option(sales_order)
	if option["already_requested"] or option["platform_cancelled"]:
		return
	if option["state"] != outbound.ELIGIBLE:
		raise Stopped(" ".join(option["reasons"]) or option["instruction"])
	outbound.request_platform_cancel(sales_order, cancellation_reason)
