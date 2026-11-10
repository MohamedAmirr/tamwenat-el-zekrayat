# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""The ONE per-order "make the Delivery Note and start the shipment" service, shared by the Sales Order list's
bulk action (``services/bulk_delivery_notes.py``) and the form's "Create Shipment" button
(``api/order_shipment.py``). Bosta only (docs/marketplace-integrations/delivery-note-first.md, "One-step shipment").

Per order (``ship_order``):

1. Under a Sales Order row lock: eligibility and the guards (``ineligible_reason``; never raises, returns a reason).
   Every refusal creates nothing: no Delivery Note, no Bosta call.
2. The payment position (``payment_plan``): COD (the Sales Order GRAND total is the basis, like
   ``delivery_note_shipment.default_cod``): an unpaid order ships with COD = ``grand_total``; a fully paid one with COD
   0; a partly paid one with ``grand_total`` minus the paid amount. NO Sales Invoice is made here: the invoice of a
   Bosta order is made when the shipment is Delivered (decided 2026-11-09).
3. The Delivery Note: a live DRAFT note of the order without a shipment is REUSED (``existing_draft``) and its COD is
   overwritten with the computed amount and confirmed; otherwise a new draft is made with ERPNext's mapper, transporter Bosta, ``custom_cod_amount`` and ``custom_cod_confirmed = 1``.
   ``validate`` of a new Bosta note runs the same order guards and ``carrier_shipments.check_deliverable``: a stock
   shortage raises ``NotDeliverable`` and the order is Skipped with no note at all.
4. After the commit (no lock held): ``carrier_shipments.request_create`` (``submit_as`` the user), which runs the
   adapter guards and queues the deduplicated Bosta create job (lookup-first, so a repeat never makes a second
   shipment). The note stays a draft until Bosta's pickup event submits it (#500). THIS MODULE NEVER SUBMITS A
   DELIVERY NOTE and never calls Bosta itself.

The note this module makes is flagged ``SKIP_AUTO_REQUEST_FLAG`` so the ``after_insert`` trigger of a new Bosta
note (``carrier_shipments.request_shipment_on_insert``, the native Create > Delivery Note path) does not also
request the shipment: this module is the one responsible for the notes it makes.

A note this module MADE is deleted when the shipment DEFINITELY fails (``carrier_shipments.discard_draft``):
- step 4 refuses (a missing city, phone or address, COD limit ... : a deliberate validation or permission error):
  deleted here, the result row is failed with the reason and no note;
- Bosta definitively refuses the create (``Failed``): ``request_create(discard_on_failure=True)`` makes the create job
  delete the note right after writing Creation Failed; the waiting form / bulk run read that through ``shipment_outcome``.
An UNKNOWN outcome (timeout, 5xx, Creation Unknown) or an unexpected error keeps the note (it may exist at Bosta; a
retry looks it up first). A REUSED draft (``existing_draft``: the person's own note) is never deleted, nor is a note made any other way (the
native form, Duplicate, Data Import, REST, Amend: their after_insert trigger and the Workflow's Create Shipment keep it
with the error). The delete needs the requesting user to have delete permission on Delivery Note, else the note is kept. The Sales Invoice
of a paid order is never touched (none is made at creation since 2026-11-09).
"""

import frappe
from frappe import _
from frappe.utils import cint, cstr, flt, strip_html_tags

from marketplace_integrations.integrations.bosta import delivery_note_shipment as bosta_shipment
from marketplace_integrations.services import (
	carrier_log,
	carrier_shipments,
	order_reservation,
	sync_reason_codes,
)
from marketplace_integrations.services.carrier_log import EDITABLE_STATUSES, NOT_CREATED

BOSTA = "Bosta"
OPEN_STATUSES = ("To Deliver", "To Deliver and Bill")
#: Financial statuses of a Shopify order that was (partly) refunded; the order in ERPNext may not know yet.
REFUNDED_STATUSES = ("REFUNDED", "PARTIALLY_REFUNDED")
#: Reason codes of a Marketplace Order whose change ERPNext holds: shipping it now would ship stale rows.
HELD_REASONS = (sync_reason_codes.ORDER_LOCKED_BY_SHIPMENT, sync_reason_codes.WAITING_FOR_STOCK_EDIT)

# Per-order states (the bulk dialog shows them).
WAITING = "waiting"  # not handled yet
CREATING = "creating"  # documents being created
SENT = "sent"  # the shipment request was queued, waiting for Bosta
ACCEPTED = "accepted"  # the shipment reached Created at Bosta
STILL_CREATING = "still_creating"  # the wait ended and Bosta had not answered
SKIPPED = "skipped"
FAILED = "failed"


class Refused(Exception):
	"""An order that cannot be shipped by this action; the message is the reason shown to the user.
	``delivery_note`` names the note that is in the way, when there is one."""

	def __init__(self, message, delivery_note=None):
		super().__init__(message)
		self.delivery_note = delivery_note


def check_permissions():
	"""The user needs to create and submit the Delivery Note up front (the pickup submits it as the user who asked).
	The per-document checks still run when the note is saved."""
	for ptype in ("create", "submit"):
		if not frappe.has_permission("Delivery Note", ptype):
			frappe.throw(
				_("You need permission to create and submit {0} to use this action.").format(_("Delivery Note")),
				frappe.PermissionError,
			)


# ---------------------------------------------------------------------------
# One order
# ---------------------------------------------------------------------------


def process_order(name, shipping_method=BOSTA):
	"""One order: a result dict, never an exception. A failure rolls back whatever this order did since the
	last commit; the previous orders are already committed."""
	try:
		return ship_order(name, shipping_method)
	except Refused as refusal:
		frappe.db.rollback()
		return _result(name, SKIPPED, cstr(refusal), delivery_note=refusal.delivery_note)
	except Exception as exc:
		frappe.db.rollback()
		return _result(name, FAILED, _failure_reason(exc, name))


def ship_order(name, shipping_method=BOSTA):
	"""The steps in the module docstring. Raises ``Refused`` for an ineligible order; any other exception
	leaves nothing behind (the caller rolls back everything since the commit below)."""
	if shipping_method != BOSTA:
		raise Refused(_("Choose a shipping method."))
	# Commit, then lock as the very next statement (MariaDB snapshot isolation). The lock keeps a second run
	# on the same order from passing the live-note check while this one is creating its note.
	frappe.db.commit()
	if not frappe.db.get_value("Sales Order", name, "name", for_update=True):
		raise Refused(_("Sales Order {0} was not found.").format(name))
	order = frappe.get_doc("Sales Order", name)
	if not frappe.has_permission("Sales Order", "read", doc=order):
		raise Refused(_("You do not have permission to read Sales Order {0}.").format(name))
	reason, existing = _eligibility(order)
	if reason:
		raise Refused(reason[0], reason[1])

	created_here = not existing
	if existing:
		# A draft the person made (or an earlier try left): the shipment is started for it as it is, and it is
		# kept when the shipment fails (it is not this action's note to delete). Its shipping charge amount is
		# restored and its COD overwritten with ``payment_plan``'s amount (the formula behind ``computed_cod``).
		plan = payment_plan(order)
		invoice, cod = plan["invoice"], plan["cod"]
		note_name = existing
		_set_draft_cod(existing, cod)
	else:
		plan = payment_plan(order)
		invoice, cod = plan["invoice"], plan["cod"]
		note_name = _make_note(order, cod).name
	frappe.db.commit()  # releases the Sales Order lock before anything that may call out

	result = {"delivery_note": note_name, "sales_invoice": invoice, "cod": cod}
	user = frappe.session.user
	try:
		frappe.has_permission("Delivery Note", "submit", doc=note_name, throw=True)
		# A note made here is deleted by the create job when Bosta definitely refuses it (``discard_draft``).
		answer = carrier_shipments.request_create(note_name, submit_as=user, discard_on_failure=created_here)
	except Exception as exc:
		frappe.db.rollback()
		reason = _failure_reason(exc, name)
		# Only a deliberate refusal is definite; an unexpected error keeps the note (its cause is unknown), and so
		# does a TimestampMismatchError (the note was changed meanwhile: not a refusal of the shipment).
		if (
			created_here
			and isinstance(exc, frappe.ValidationError | frappe.PermissionError)
			and not isinstance(exc, frappe.TimestampMismatchError)
		):
			deleted = _discarded(name, note_name, reason, user, result)
			if deleted:
				return deleted
		return _result(
			name,
			FAILED,
			_(
				"Delivery Note {0} was created as a draft but Bosta was not started. Open it, fix the problem and press Create Shipment. Reason: {1}"
			).format(note_name, reason),
			**result,
		)
	if answer.get("needs_city"):
		if created_here:
			deleted = _discarded(
				name,
				note_name,
				_("The Bosta city could not be found from the delivery address. Correct the address and try again."),
				user,
				result,
			)
			if deleted:
				return deleted
		return _result(
			name,
			FAILED,
			_(
				"Delivery Note {0} was created as a draft. The Bosta city could not be found from the address: open it, pick the city and press Create Shipment."
			).format(note_name),
			**result,
		)
	return _result(name, SENT, _("Creating at Bosta"), **result)


def _discarded(sales_order, note_name, reason, user, result):
	"""The pre-check refused the note this action just made: delete it (``carrier_shipments.discard_draft``) and return the
	failed row, which names the reason and no longer links the note. ``None`` when the note could not be deleted (it is
	kept and the caller reports it as before)."""
	if not carrier_shipments.discard_draft(note_name, reason, requested_by=user):
		return None
	return _result(
		sales_order,
		FAILED,
		_("The Bosta shipment could not be created, so the Delivery Note {0} was deleted. Reason: {1}").format(
			note_name, reason
		),
		sales_invoice=result["sales_invoice"],
		cod=result["cod"],
	)


# ---------------------------------------------------------------------------
# The outcome of a started shipment (the form's wait and the bulk run's wait read it)
# ---------------------------------------------------------------------------

#: ``shipment_outcome`` states.
OUTCOME_CREATING = "creating"
OUTCOME_ACCEPTED = "accepted"
OUTCOME_FAILED = "failed"  # refused or unknown; the note is KEPT
OUTCOME_DISCARDED = "discarded"  # refused for good; the draft note was deleted
OUTCOME_MISSING = "missing"


def shipment_outcome(delivery_note):
	"""``{"state", "status", "reason", "user"}`` for a note this module started. Reads only; no permission check (the
	API checks). ``accepted``: Created at the carrier (a Bosta note stays a draft until pickup, #500). ``failed``:
	Creation Failed or Creation Unknown, the note is kept with the reason. ``discarded``: the note was deleted after the
	carrier definitively refused it (``carrier_shipments.discarded_notice``). ``creating``: no answer yet."""
	values = frappe.db.get_value(
		"Delivery Note", delivery_note, ["custom_integration_status", "custom_last_integration_error"], as_dict=True
	)
	if not values:
		notice = carrier_shipments.discarded_notice(delivery_note)
		if notice:
			return {"state": OUTCOME_DISCARDED, "status": "", "reason": notice["reason"], "user": notice.get("user")}
		return {"state": OUTCOME_MISSING, "status": "", "reason": "", "user": None}
	status = cstr(values.custom_integration_status)
	reason = strip_html_tags(cstr(values.custom_last_integration_error)).strip()
	if status == carrier_log.CREATED:
		return {"state": OUTCOME_ACCEPTED, "status": status, "reason": "", "user": None}
	if status and status != carrier_log.CREATING:
		return {"state": OUTCOME_FAILED, "status": status, "reason": reason or _(status), "user": None}
	return {"state": OUTCOME_CREATING, "status": status, "reason": "", "user": None}


# ---------------------------------------------------------------------------
# Rules (guards)
# ---------------------------------------------------------------------------


def ineligible_reason(order):
	"""Why the Sales Order cannot be shipped by this action, or ``None``. Never raises."""
	reason, _existing = _eligibility(order)
	return reason[0] if reason else None


def _eligibility(order):
	"""``(reason, existing_draft)``: ``reason`` is ``(text, note)`` or ``None``; ``existing_draft`` is the live draft
	note to reuse, else ``None``."""
	if order.docstatus != 1:
		return (_("Sales Order {0} is not submitted.").format(order.name), None), None
	if not order.get("custom_marketplace_platform"):
		return (_("Sales Order {0} is not a marketplace order.").format(order.name), None), None
	platform = bosta_shipment.excluded_platform(order.name)
	if platform:
		return (
			_("Bosta shipments cannot be created for {0} orders: {0} fulfils its own orders.").format(platform),
			None,
		), None
	# A live note that is in the way is named first: an order whose note was submitted is "To Bill" or Completed, and
	# that status would hide the note from the reason.
	existing, note_reason = existing_draft(order)
	if note_reason:
		return note_reason, None
	if order.status not in OPEN_STATUSES:
		return (
			_("Sales Order {0} is {1}; only To Deliver and To Deliver and Bill orders can be shipped.").format(
				order.name, _(order.status)
			),
			None,
		), None
	problem = order_state_problem(order.name)
	if problem:
		return (problem, None), None
	if any(order.has_product_bundle(row.item_code) for row in order.items if row.item_code):
		return (
			_(
				"Sales Order {0} has a Product Bundle item. Product Bundles cannot be shipped by this action: packs must be normal items (the -P<N> items)."
			).format(order.name),
			None,
		), None
	if not bosta_shipment.bosta_supplier():
		return (_("The Bosta carrier is not set up: there is no Supplier named Bosta."), None), None
	return None, existing


def order_state_problem(sales_order):
	"""Why a new Bosta Delivery Note cannot be made for the Sales Order now (plain text), else ``None``. Used by
	the one-step path and by ``validate`` of every new Bosta note (the native path), so both refuse the same way:

	- the stage is Waiting for Stock (nothing is reserved for it);
	- a Shopify edit is in flight, or the order is held (``HELD_REASONS``): its rows may not match the marketplace;
	- the marketplace cancelled or refunded the order and ERPNext does not know yet;
	- a row's warehouse is not the Marketplace Account's warehouse."""
	if not sales_order:
		return None
	fields = ["custom_marketplace_order"]
	if order_reservation.stage_installed():
		fields.append(order_reservation.STAGE_FIELD)
	values = frappe.db.get_value("Sales Order", sales_order, fields, as_dict=True)
	if not values:
		return None
	if values.get(order_reservation.STAGE_FIELD) == order_reservation.WAITING_FOR_STOCK:
		return _("Sales Order {0} is Waiting for Stock, so it cannot be shipped yet.").format(sales_order)
	if not values.custom_marketplace_order:
		return None
	marketplace = frappe.db.get_value(
		"Marketplace Order",
		values.custom_marketplace_order,
		[
			"marketplace_account",
			"outbound_edit_state",
			"sync_reason_code",
			"cancelled_at",
			"external_financial_status",
		],
		as_dict=True,
	)
	if not marketplace:
		return None
	if cstr(marketplace.outbound_edit_state):
		return _("A change of Sales Order {0} is being sent to the marketplace. Wait for it to finish.").format(
			sales_order
		)
	if cstr(marketplace.sync_reason_code) in HELD_REASONS:
		return _(
			"Sales Order {0} is held: the marketplace changed it and ERPNext was not updated. Review it first."
		).format(sales_order)
	if marketplace.cancelled_at or cstr(marketplace.external_financial_status).upper() in REFUNDED_STATUSES:
		return _(
			"The marketplace cancelled or refunded Sales Order {0}, but ERPNext was not updated yet. Sync it first."
		).format(sales_order)
	account_warehouse = (
		frappe.db.get_value("Marketplace Account", marketplace.marketplace_account, "warehouse")
		if marketplace.marketplace_account
		else None
	)
	if account_warehouse:
		wrong = frappe.get_all(
			"Sales Order Item",
			filters={"parent": sales_order, "warehouse": ["not in", [account_warehouse, ""]]},
			pluck="warehouse",
			distinct=True,
			parent_doctype="Sales Order",
			limit_page_length=0,
		)
		if wrong:
			return _(
				"Sales Order {0} has items in warehouse {1}, but the Marketplace Account's warehouse is {2}."
			).format(sales_order, ", ".join(wrong), account_warehouse)
	return None


def _live_notes(sales_order):
	"""The non-return, non-cancelled Delivery Notes (draft or submitted) of the order."""
	# System-internal existence check after the caller's Sales Order read check: permissions must not hide a
	# note the user cannot read, or a second one would be made.
	parents = frappe.get_all(
		"Delivery Note Item",
		filters={"against_sales_order": sales_order, "docstatus": ["<", 2]},
		pluck="parent",
		distinct=True,
		limit_page_length=0,
	)
	if not parents:
		return []
	return frappe.get_all(
		"Delivery Note",
		filters={"name": ["in", parents], "is_return": 0, "docstatus": ["<", 2]},
		fields=["name", "docstatus", "custom_integration_status", "transporter"],
		order_by="creation asc",
		limit_page_length=0,
	)


def live_note(sales_order):
	"""A non-return, non-cancelled Delivery Note (draft or submitted) of the order, else ``None``."""
	notes = _live_notes(sales_order)
	return notes[0].name if notes else None


def existing_draft(order):
	"""``(draft_name, reason)``. A live DRAFT note of the order without a shipment is reused (``draft_name``); a
	note that has a shipment, is submitted, is of another carrier, or does not ship the whole order is a refusal
	naming it (``reason`` is ``(text, note)``). ``(None, None)`` when the order has no live note."""
	notes = _live_notes(order.name)
	if not notes:
		return None, None
	for note in notes:
		started = cstr(note.custom_integration_status) or NOT_CREATED
		if note.docstatus != 0 or started not in EDITABLE_STATUSES:
			return None, (_("Delivery Note {0} already exists for this order.").format(note.name), note.name)
	if len(notes) > 1:
		return None, (
			_("Sales Order {0} has more than one draft Delivery Note ({1}); keep one first.").format(
				order.name, ", ".join(note.name for note in notes)
			),
			notes[0].name,
		)
	note = notes[0]
	from marketplace_integrations.services import order_edit

	doc = frappe.get_doc("Delivery Note", note.name)
	if not bosta_shipment.applies_to(doc) or not order_edit._whole_order_note(doc, order):
		return None, (
			_(
				"Delivery Note {0} is not a Bosta note for the whole order, so its shipment is not created here. Open it and use its own buttons."
			).format(note.name),
			note.name,
		)
	return note.name, None


def _invoices(sales_order, docstatus):
	# An invoice the user cannot read must still be seen.
	parents = frappe.get_all(
		"Sales Invoice Item",
		filters={"sales_order": sales_order, "docstatus": docstatus},
		pluck="parent",
		distinct=True,
		limit_page_length=0,
	)
	if not parents:
		return []
	return frappe.get_all(
		"Sales Invoice",
		filters={"name": ["in", parents], "is_return": 0, "docstatus": docstatus},
		pluck="name",
		limit_page_length=0,
	)


def payment_plan(order):
	"""``{"cod", "paid_in_full", "invoice"}`` from the stored documents (never from the client). Raises ``Refused``
	when the position is ambiguous. No invoice is ever made here.

	- Fully paid is judged by ERPNext's own measure: nothing outstanding on ``(rounded_total or grand_total)
	  - advance_paid`` (ERPNext caps a recorded payment at the rounded total), COD 0. Not paid at all: COD =
	  ``grand_total`` (Shopify's total). Partly paid: COD = ``grand_total`` minus paid, never below 0. Paid up to
	  ``grand_total`` only (a rounding difference left) is refused.
	- One submitted Sales Invoice that already exists (made by someone else) is read, never duplicated: it must cover
	  the WHOLE order (same grand total, order billed 100%) and have no credit note, else the order is refused. Paid
	  so far = its total minus its outstanding. With an outstanding AND a payment still recorded on the order the
	  position is ambiguous, so the order is refused.
	"""
	grand_total = flt(order.grand_total, 2)
	if grand_total <= 0:
		raise Refused(_("Sales Order {0} has a zero total; create its Delivery Note by hand.").format(order.name))
	invoices = _invoices(order.name, 1)
	if len(invoices) > 1:
		raise Refused(
			_("Sales Order {0} has more than one submitted Sales Invoice ({1}); check them first.").format(
				order.name, ", ".join(invoices)
			)
		)
	if invoices:
		invoice = frappe.get_doc("Sales Invoice", invoices[0])
		if frappe.db.exists("Sales Invoice", {"return_against": invoice.name, "docstatus": 1}):
			raise Refused(
				_("Sales Invoice {0} has a credit note; check Sales Order {1} by hand.").format(
					invoice.name, order.name
				)
			)
		if flt(invoice.grand_total, 2) != grand_total or flt(order.per_billed, 2) < 100:
			raise Refused(
				_(
					"Sales Invoice {0} does not cover the whole Sales Order {1} (invoice {2}, order {3}); check it first."
				).format(invoice.name, order.name, flt(invoice.grand_total, 2), grand_total)
			)
		outstanding = flt(invoice.outstanding_amount, 2)
		if outstanding > 0 and flt(order.advance_paid, 2) > 0:
			raise Refused(
				_(
					"Sales Invoice {0} still has an outstanding amount while Sales Order {1} also has a recorded payment. Apply the payment to the invoice first."
				).format(invoice.name, order.name)
			)
		# ERPNext's own measure: the invoice is paid when nothing is outstanding on it.
		paid = flt((flt(invoice.rounded_total) or flt(invoice.grand_total)) - outstanding, 2)
		paid_in_full = outstanding <= 0
		invoice_name = invoice.name
	else:
		paid = flt(order.advance_paid, 2)
		# ERPNext caps the recorded payment of an order at its rounded total (a grand total of 494.21 rounded to
		# 494 is fully paid at 494, the 0.21 is written off): paid in full when nothing is outstanding on
		# (rounded_total or grand_total) - advance_paid.
		paid_in_full = paid > 0 and flt((flt(order.rounded_total) or grand_total) - paid, 2) <= 0
		invoice_name = None
	if paid_in_full:
		cod = 0
	else:
		cod = grand_total if paid <= 0 else max(flt(grand_total - paid, 2), 0)
		if cod <= 0:
			# Paid up to the grand total but not to the rounded one: only a rounding difference is left, and
			# COD 0 for it is never shipped.
			raise Refused(
				_(
					"The recorded payment of Sales Order {0} covers its total except a rounding difference; settle the difference first."
				).format(order.name)
			)
	return {"cod": cod, "paid_in_full": paid_in_full, "invoice": invoice_name}


def _set_draft_cod(note_name, cod):
	"""A reused unsent draft: first restore the shipping charge amount ``make_delivery_note`` reset to 0
	(``carry_order_charges``), then overwrite its COD with the order's computed amount (``payment_plan``, the same
	formula ``computed_cod`` uses for a new note) and confirm it. Saved only when something differs, as the requesting
	user (the shipment request needs write permission on the note anyway). Calling ``payment_plan`` for a reused draft
	means an ambiguous payment position now refuses the order (``Refused``) instead of shipping the draft as it was."""
	note = frappe.get_doc("Delivery Note", note_name)
	total = flt(note.grand_total, 2)
	bosta_shipment.carry_order_charges(note)
	if (
		flt(note.grand_total, 2) == total
		and flt(note.get("custom_cod_amount"), 2) == flt(cod, 2)
		and cint(note.get("custom_cod_confirmed"))
	):
		return
	note.custom_cod_amount = cod
	note.custom_cod_confirmed = 1
	note.save()


def _make_note(order, cod):
	"""The draft Delivery Note: ERPNext's mapper, transporter Bosta, the COD confirmed. The adapter's own
	``guard_changes`` (run by ``insert``) applies the COD rule and limit; its ``validate`` hook runs the order guards and
	the stock check of a new note (``NotDeliverable`` becomes a refusal: nothing is left behind). It is left a draft:
	the Bosta pickup event submits it (``statuses._write_state``, #500)."""
	from erpnext.selling.doctype.sales_order.sales_order import make_delivery_note

	note = make_delivery_note(order.name)
	note.transporter = bosta_shipment.bosta_supplier()
	note.custom_cod_amount = cod
	note.custom_cod_confirmed = 1
	# The same amount ``computed_cod`` would give (both are ``payment_plan``); marked typed so it is kept as given.
	note.set(bosta_shipment.TYPED_FIELD, 1)
	note.flags.ignore_permissions = False
	# This service requests the shipment itself, so the after_insert trigger of a new Bosta note must not.
	note.flags[carrier_shipments.SKIP_AUTO_REQUEST_FLAG] = True
	try:
		note.insert()
	except carrier_shipments.NotDeliverable as exc:
		frappe.clear_messages()
		raise Refused(strip_html_tags(cstr(exc)).strip()) from exc
	return note


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------


def _result(sales_order, status, message, delivery_note=None, sales_invoice=None, cod=None):
	return {
		"sales_order": sales_order,
		"status": status,
		"message": message,
		"delivery_note": delivery_note,
		"sales_invoice": sales_invoice,
		"cod": cod,
	}


def _failure_reason(exc, name):
	"""Plain text for the result row (the client escapes it). Unexpected errors go to the Error Log."""
	if isinstance(exc, frappe.PermissionError):
		reason = _("You do not have permission to do this.")
	elif isinstance(exc, frappe.ValidationError):
		reason = strip_html_tags(cstr(exc)).strip()
	else:
		frappe.log_error(title="Order shipment failed", message=f"{name}\n{frappe.get_traceback()}")
		reason = strip_html_tags(cstr(exc)).strip() or exc.__class__.__name__
	frappe.clear_messages()
	return reason[:300] or exc.__class__.__name__
