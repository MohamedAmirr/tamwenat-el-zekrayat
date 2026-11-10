# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""The neutral carrier contract and state machine of a Delivery Note shipment (epic #413, issue #415).

A Delivery Note whose transporter is a carrier's Supplier is the shipment (``docs/marketplace-integrations/
delivery-note-first.md``). This module owns what every carrier shares: the ``CARRIERS`` registry, the
document rules that ``hooks.py`` registers (``validate_delivery_note``, ``require_shipment_created``,
``guard_cancel_or_delete``, Sales Order ``refuse_sales_order_cancel``) and the state machine of the
Integration Status (``request_create`` -> ``run_create`` -> ``_finish``, ``check``, ``cancel``,
``reconcile_open_notes``). Neutral code never branches on a carrier name and holds no carrier logic: it
calls the adapter.

An adapter is a module exposing (the contract; a contract test checks every adapter in ``CARRIERS``):

- ``SHIPPING_CARRIER``: the Supplier name (``transporter_name``) that shows the Shipping section.
- ``SERVICE_NAME``: the Integration Request service and the name shown in messages.
- ``applies_to(doc)``: does the note (or a dict with ``is_return`` and ``transporter``) belong to it.
- ``guard_changes(doc)``: ``validate``: set up a new note, keep app-written fields and a started shipment
  untouched. A deliberate refusal raises.
- ``guard_always(doc)``: optional; ``validate`` of every Delivery Note, whoever owns it (a rule about a field
  that decides ownership, e.g. who may set a return source). A deliberate refusal raises.
- ``clear_inherited(doc)``: clear what an amended new note copied from a note of this carrier.
- ``validate_ready(doc, action="create")``: everything refusable before the carrier is contacted (raises).
  ``action`` is ``"create"`` (also run again under the row lock), ``"lookup"`` or ``"cancel"`` (configuration
  and note checks only). For ``"create"`` it may return a dict instead of ``None``: the note needs the
  person's input first and nothing is sent (the dict is returned to the form).
- ``find_remote(doc)``: the carrier's data when it has the shipment, ``None`` when it confirms none, raises
  ``carrier_log.Unknown`` when the answer is not certain. The job calls it first on every attempt after the
  first; an adapter that sets the optional ``LOOKUP_FIRST = True`` has it called on the first attempt too.
- ``create_remote(doc)``: create and return the carrier's data; raises ``carrier_log.Failed`` (definitive
  refusal) or ``carrier_log.Unknown``.
- ``created_values(doc, data)``: the Delivery Note fields to write once Created.
- ``log_data(values)``: the text kept on the Integration Log row of a create (no personal data) or ``None``.
- ``after_created(delivery_note, data)``: work after the Created write (runs in the worker).
- ``cancel_remote(doc)``: ``_dict(outcome="cancelled"|"refused", message, values, http_status)`` or raises
  ``carrier_log.Unknown``. ``values`` are extra fields written with the result.
- ``after_cancelled(delivery_note)``: work after the Cancelled write.
- ``picked_up(doc)``: optional; True when the carrier already has the parcel / the shipment is past the point where a
  cancel is possible (the Sales Order cancel cascade refuses such a note up front instead of asking the carrier).
- ``refresh(delivery_note)``: read the carrier's state and apply it; returns the shipping status. Once it
  wrote the Integration Status Cancelled it calls ``cancel_after_carrier_cancelled`` (every path that reads
  the carrier does: webhook, nightly job, Refresh).
- ``after_submit(doc)``: optional; work queued after the note is submitted (``on_submit``). Must not raise.
- ``REQUEST_ON_INSERT``: optional, True for an adapter whose NEW draft note starts its shipment by itself
  (``request_shipment_on_insert``, ``after_insert``). ``validate`` of such a new note runs the optional
  ``guard_new_note(doc)`` (a deliberate refusal raises) and ``check_deliverable`` (no note is made when the stock
  cannot cover it).
- ``SUBMIT_AT_PICKUP``: optional, True for an adapter whose note stays a draft until the carrier's pickup event
  (#500): the create job then does not submit, ``request_create`` records who asked
  (``custom_shipment_requested_by``) and the carrier's status code of the pickup queues ``run_submit_at_pickup``
  (the code table lives in the adapter's status module). ``after_pickup_submit(delivery_note)``: optional, work
  after that submit succeeded.
- ``reconcile()``: the nightly job of this carrier.
- ``refuse_sales_order_cancel(order)``: Sales Order ``before_cancel``.
- ``problem_states()``: ``{problem_type: [shipping statuses]}`` for Needs Attention (the exception and return
  types only; the other types come from ``custom_integration_status``). An adapter with ``SUBMIT_AT_PICKUP`` also
  lists its pickup statuses under "Picked Up Not Submitted" (a Created draft still in one of them).
- ``terminal_states(is_return=False)``: the shipping statuses that never change again (not a stale status);
  ``is_return`` is True for a return note the carrier owns (a pickup ends at different states).
"""

import json
import sys
import time

import frappe
from frappe import _
from frappe.utils import add_to_date, cstr, escape_html, flt, get_datetime, now_datetime, strip_html_tags
from frappe.utils.background_jobs import is_job_enqueued

from marketplace_integrations.integrations.bosta import delivery_note_shipment as bosta_shipment
from marketplace_integrations.integrations.noon import delivery_note_shipment as noon_shipment
from marketplace_integrations.services import delivery_note_workflow as lifecycle
from marketplace_integrations.services.carrier_log import (
	CANCELLATION_PENDING,
	CANCELLATION_UNKNOWN,
	CANCELLED,
	CREATED,
	CREATING,
	EDITABLE_STATUSES,
	FAILED,
	LIVE_STATUSES,
	NOT_CREATED,
	UNKNOWN,
	Failed,
	Unknown,
	attempt_number,
	close_log,
	open_log,
)

#: ``{carrier: adapter}``, a plain dict: no base class. To add a carrier: one adapter module with the
#: contract above, one row here, a Supplier, and its carrier-specific fields, see the Provider contract in the integration READMEs.
CARRIERS = {"Bosta": bosta_shipment, "Noon": noon_shipment}

SKIP_FLAG = "skip_carrier_shipment"
#: A Creating note with no queued job older than this counts as stuck (``check`` can resolve it).
STALE_CREATING_MINUTES = 5
#: A Cancellation Pending note older than this lost its request (the call is synchronous): it may be
#: cancelled again, which is safe because a refused repeat is resolved by one read.
STALE_CANCEL_MINUTES = 2

_CREATE_JOB = "{}-create|{}"
_REQUEST_JOB = "{}-request|{}"
#: Set on a new note by the code that requests its shipment itself (``order_shipment``): the ``after_insert`` trigger
#: ``request_shipment_on_insert`` then leaves it alone, so exactly one of them asks for the shipment. The trigger's
#: own requests never delete a note on failure (only ``order_shipment`` does, for the notes it made).
SKIP_AUTO_REQUEST_FLAG = "skip_auto_shipment_request"


class NotDeliverable(frappe.ValidationError):
	"""The stock rows of a note cannot be delivered (``check_deliverable``): short of stock or over the order."""

#: The linked doctypes ``DeliveryNote.on_cancel`` ignores (ERPNext sets ``ignore_linked_doctypes`` to the first
#: four) plus the Packing Slips it cancels first.
NATIVE_CANCEL_IGNORED_LINKS = (
	"GL Entry",
	"Stock Ledger Entry",
	"Repost Item Valuation",
	"Serial and Batch Bundle",
	"Packing Slip",
)


def adapter_for(doc):
	"""The adapter that owns ``doc`` (a Delivery Note or a dict with ``is_return`` and ``transporter``). A
	note the app drafts for a shipment that already exists sets ``flags.skip_carrier_shipment`` and is left
	alone."""
	flags = getattr(doc, "flags", None)
	if flags and flags.get(SKIP_FLAG):
		return None
	return next((adapter for adapter in CARRIERS.values() if adapter.applies_to(doc)), None)


def _adapter_of(delivery_note):
	adapter = adapter_for(frappe.get_doc("Delivery Note", delivery_note))
	if not adapter:
		frappe.throw(_("This Delivery Note has no carrier shipment."))
	return adapter


def saved_state(doc):
	"""The note as stored (ownership of an existing note is judged by it), else ``doc`` itself."""
	return doc if doc.is_new() else (doc.get_doc_before_save() or doc)


def status_of(doc_or_name):
	"""The stored Integration Status of a note ("" counts as Not Created)."""
	name = doc_or_name if isinstance(doc_or_name, str) else doc_or_name.name
	return cstr(frappe.db.get_value("Delivery Note", name, "custom_integration_status")) or NOT_CREATED


# ---------------------------------------------------------------------------
# Document rules (hooks.py)
# ---------------------------------------------------------------------------


def validate_delivery_note(doc, method=None):
	"""``validate``: refuse changes the started shipment depends on (a deliberate refusal)."""
	# #498: the stage (Workflow State) is locked on the server too: Workflow ``allow_edit`` is browser-only.
	lifecycle.keep_state_consistent(doc)
	# An existing note is judged by what is stored (rows just added in the form must not take it out of
	# the guard) and by the new copy (a person who just chose a carrier on an editable note).
	if doc.flags.get(SKIP_FLAG):
		return
	adapters = []
	for adapter in (adapter_for(saved_state(doc)), adapter_for(doc)):
		if adapter and adapter not in adapters:
			adapters.append(adapter)
	for adapter in adapters:
		adapter.guard_changes(doc)
	for adapter in CARRIERS.values():
		always = getattr(adapter, "guard_always", None)
		if always:
			always(doc)
	if doc.is_new() and doc.get("amended_from") and not adapter_for(doc):
		# Amend copies even no_copy fields: the carrier was changed, so clear what the source note's carrier left.
		source = frappe.db.get_value("Delivery Note", doc.amended_from, ["is_return", "transporter", "custom_return_source"], as_dict=True)
		if source:
			for adapter in CARRIERS.values():
				if adapter.applies_to(source):
					adapter.clear_inherited(doc)
	if doc.is_new() and not doc.get("is_return"):
		# A NEW note of a carrier that starts its shipment on insert: the order guards and the stock check run now,
		# so a note that cannot be shipped is never made (no draft left behind). Only a new note: Edit Order's refresh
		# and the pickup submit save existing ones.
		adapter = adapter_for(doc)
		if adapter and getattr(adapter, "REQUEST_ON_INSERT", False):
			guard_new = getattr(adapter, "guard_new_note", None)
			if guard_new:
				guard_new(doc)
			check_deliverable(doc)


def request_shipment_on_insert(doc, method=None):
	"""``after_insert``: a NEW draft note of a carrier with ``REQUEST_ON_INSERT`` starts its shipment, so nobody has to
	press anything after Save. Only the first insert triggers: later saves (Edit Order, the pickup submit) and
	return notes never do, and a note made by ``order_shipment`` (``SKIP_AUTO_REQUEST_FLAG``) is requested by that
	service. The work is a deduplicated job queued after the commit (``request_create`` commits and must not run inside the
	save); the job looks the shipment up first on every retry, so a repeat never makes a second one."""
	adapter = adapter_for(doc)
	if (
		not adapter
		or not getattr(adapter, "REQUEST_ON_INSERT", False)
		or doc.get("is_return")
		or doc.docstatus != 0
		or doc.flags.get(SKIP_AUTO_REQUEST_FLAG)
	):
		return
	frappe.enqueue(
		"marketplace_integrations.services.carrier_shipments.run_request_on_insert",
		queue="short",
		timeout=300,
		job_id=_REQUEST_JOB.format(adapter.SHIPPING_CARRIER.lower(), doc.name),
		deduplicate=True,
		enqueue_after_commit=True,
		delivery_note=doc.name,
		requested_by=frappe.session.user,
	)


def run_request_on_insert(delivery_note, requested_by):
	"""The job of ``request_shipment_on_insert``: as the user who saved the note, start the shipment exactly like the
	workflow's Create Shipment (``delivery_note_workflow.start_shipment``). A failure never deletes the note (it was not
	made by the one-step flow): it stays a draft with the error. Never raises."""
	frappe.set_user(requested_by)
	try:
		if status_of(delivery_note) not in EDITABLE_STATUSES or frappe.db.get_value("Delivery Note", delivery_note, "docstatus") != 0:
			return
		if not frappe.has_permission("Delivery Note", "submit", doc=delivery_note):
			lifecycle.record_request_failure(delivery_note, _("You need permission to submit Delivery Notes to start the shipment."))
			return
		# A note made any way but the one-step flow is never deleted by a failure: it stays a draft with the error.
		lifecycle.start_shipment(frappe._dict(name=delivery_note))
	except Exception:
		frappe.db.rollback()
		frappe.log_error(title="Starting the shipment of a new Delivery Note failed", message=f"{delivery_note}\n{frappe.get_traceback()}")


def require_shipment_created(doc, method=None):
	"""``before_submit``: no submit unless the shipment is Created (judged by the stored status, not by the
	copy a client sent)."""
	adapter = adapter_for(doc)
	# A return pickup is optional (a return the carrier owns may be submitted without one): submitting the
	# return is the receipt. Only a pickup still being requested blocks it, so it cannot be left orphaned.
	status = status_of(doc)
	blocked = status in (CREATING, UNKNOWN) if doc.get("is_return") else status != CREATED
	if adapter and blocked:
		frappe.throw(
			_("The {0} shipment of this Delivery Note is not Created yet, so it cannot be confirmed.").format(
				adapter.SERVICE_NAME
			),
			title=_("{0} shipment").format(adapter.SERVICE_NAME),
		)
	if (
		adapter
		and getattr(adapter, "SUBMIT_AT_PICKUP", False)
		and not doc.get("is_return")
		and not doc.flags.get(PICKUP_SUBMIT_FLAG)
	):
		# #500: the carrier's pickup event submits the note (the job sets the flag); nobody submits it before.
		frappe.throw(
			_("The Delivery Note is submitted when {0} picks the shipment up.").format(adapter.SERVICE_NAME),
			title=_("{0} shipment").format(adapter.SERVICE_NAME),
		)


def after_submit(doc, method=None):
	"""``on_submit``: the optional adapter hook ``after_submit(doc)`` (an adapter queues follow-up work here, never
	while the note is a draft being submitted). Must not raise."""
	hook = getattr(adapter_for(doc), "after_submit", None)
	if hook:
		hook(doc)


def guard_cancel_or_delete(doc, method=None):
	"""``before_cancel`` and ``on_trash``: refuse while a live shipment is linked. A note whose Integration
	Status is Cancelled passes, without any call to the carrier."""
	adapter = adapter_for(doc)
	if adapter and status_of(doc) in LIVE_STATUSES:
		frappe.throw(
			_(
				"This Delivery Note has a {0} shipment ({1}). Cancel the shipment at {0} first "
				"(Cancel {0} Shipment, or Cancel on a submitted note)."
			).format(adapter.SERVICE_NAME, _(status_of(doc))),
			title=_("{0} shipment").format(adapter.SERVICE_NAME),
		)


#: A DRAFT note in one of these statuses has a shipment at the carrier that a delete cancels first.
CANCEL_BEFORE_DELETE_STATUSES = (CREATED, CANCELLATION_PENDING, CANCELLATION_UNKNOWN)


def guard_delete(doc, method=None):
	"""``on_trash``: a DRAFT note with a live shipment (Created, Cancellation Pending, Cancellation Unknown) is deleted
	only after the carrier cancelled the shipment: ``cancel`` runs here, before the native delete goes on.

	- Cancelled: the delete continues. Everything else (the carrier refused, the answer is unknown) throws, so the
	  native delete stops and nothing is removed; the status the cancel wrote stays (``cancel`` committed it) and a
	  second delete asks again.
	- ``cancel`` commits before it locks and again after the carrier answered (lock-after-commit, no lock held across the
	  carrier call). That is safe here: the delete has changed nothing yet when ``on_trash`` runs (it refuses on the
	  throw), so the commit only publishes the cancel. If the native delete fails AFTER this (a linked document), the note
	  stays with Integration Status Cancelled, which a later delete passes without asking the carrier again. A linked
	  document that would block the native delete is checked FIRST (``_check_native_delete_not_blocked``), before any
	  carrier call, so the shipment is not cancelled for a delete that then fails.
	- Creating and Creation Unknown still block (``guard_cancel_or_delete``); a submitted note cannot be deleted at all.
	- It needs write permission on the note (the same as ``api/carrier_shipments.cancel_shipment`` for a draft), on top
	  of the delete permission Frappe already checked; a system delete (``ignore_permissions``) skips only that check."""
	adapter = adapter_for(doc)
	if not (adapter and doc.docstatus == 0 and status_of(doc) in CANCEL_BEFORE_DELETE_STATUSES):
		return guard_cancel_or_delete(doc, method)
	if not doc.flags.get("ignore_permissions"):
		frappe.has_permission("Delivery Note", "write", doc=doc, throw=True)
	service = adapter.SERVICE_NAME
	# The carrier cancel cannot be undone: refuse a delete a linked document would block BEFORE asking the carrier.
	_check_native_delete_not_blocked(doc)
	result = cancel(doc.name)
	if result["outcome"] == "cancelled":
		# Keep the in-memory note in step with what cancel() stored, so the Deleted Document snapshot and the
		# later on_trash handlers see the Cancelled status.
		doc.custom_integration_status = result["status"]
		doc.modified = frappe.db.get_value("Delivery Note", doc.name, "modified") or doc.modified
		return
	reason = escape_html(strip_html_tags(cstr(result.get("message"))).strip()) or _("The carrier gave no reason.")
	if result["outcome"] == "refused":
		frappe.throw(
			_("{0} did not cancel the shipment, so the Delivery Note was not deleted: {1}").format(service, reason),
			title=_("{0} shipment").format(service),
		)
	frappe.throw(
		_(
			"It is not known whether {0} cancelled the shipment, so the Delivery Note was not deleted. Try again in a few minutes. {1}"
		).format(service, reason),
		title=_("{0} shipment").format(service),
	)


def refuse_sales_order_cancel(doc, method=None):
	"""Sales Order ``before_cancel``: refuse while a draft Delivery Note has a live carrier shipment."""
	for adapter in CARRIERS.values():
		adapter.refuse_sales_order_cancel(doc)


def boot_session(bootinfo):
	"""Boot data: the carriers whose Delivery Note shows the generic Shipping section. The section's
	``depends_on`` reads it, so no carrier name is hard-coded."""
	bootinfo.marketplace_shipping_carriers = [
		adapter.SHIPPING_CARRIER for adapter in CARRIERS.values() if getattr(adapter, "SHIPPING_CARRIER", None)
	]


# ---------------------------------------------------------------------------
# Request (user action) -> job
# ---------------------------------------------------------------------------


def check_deliverable(doc):
	"""Raise (a validation error, no carrier is contacted) when ERPNext's own submit would refuse the stock rows
	of this draft note: a row (or a Product Bundle component of its packed items, summed per item and warehouse
	with the other rows) asking for more than the stock of its warehouse (unless negative stock is
	allowed), or more than the Sales Order has left to deliver (its over-delivery allowance counts, and so
	does the role allowed to over-deliver). Only stock items are judged, like ERPNext. A return is not checked
	(it brings stock in). Stock Reservation Entries of OTHER orders are subtracted from the stock of a warehouse (those of the note's own Sales Orders are not). The stock race is not judged: it still ends as a Created draft
	(``_submit_created``). One message names every problem, so the form's Submit and the bulk run show the same
	text."""
	if doc.get("is_return"):
		return
	# ERPNext functions relied on (docs/frappe-best-practices.md, Version compatibility): the negative-stock
	# rule and the over-delivery allowance of ``StatusUpdater``.
	from erpnext.controllers.status_updater import get_allowance_for
	from erpnext.stock.stock_ledger import is_negative_stock_allowed

	from marketplace_integrations.services import order_reservation
	from marketplace_integrations.services.listing_quantity import _actual_quantities

	own_orders = {row.against_sales_order for row in doc.items if row.get("against_sales_order")}
	wanted, ordered = {}, {}

	def want(item_code, warehouse, qty):
		if warehouse and frappe.get_cached_value("Item", item_code, "is_stock_item"):
			wanted[(item_code, warehouse)] = wanted.get((item_code, warehouse), 0) + flt(qty)

	for row in doc.items:
		if not row.item_code:
			continue
		if doc.has_product_bundle(row.item_code):
			# ERPNext posts a Product Bundle through its packed items only (``get_item_list``): each component, in
			# its own warehouse (the row's when empty), in stock units. The packed items are saved with the draft
			# (the ``validate`` that inserts or saves it builds them). The parent row is not a stock movement.
			for packed in doc.get("packed_items"):
				if packed.parent_detail_docname == row.name and packed.parent_item == row.item_code:
					want(packed.item_code, packed.warehouse or row.warehouse, packed.qty)
		else:
			want(row.item_code, row.warehouse, row.stock_qty or row.qty)
		if row.so_detail and frappe.get_cached_value("Item", row.item_code, "is_stock_item"):
			ordered[row.so_detail] = ordered.get(row.so_detail, 0) + flt(row.qty)

	precision = frappe.get_precision("Delivery Note Item", "qty")

	def text(value):
		# 3, not 3.0; never negative
		return ("%.*f" % (precision, max(flt(value), 0))).rstrip("0").rstrip(".") or "0"

	problems = []
	by_warehouse = {}
	for item_code, warehouse in wanted:
		if not is_negative_stock_allowed(item_code=item_code):
			by_warehouse.setdefault(warehouse, []).append(item_code)
	for warehouse, codes in by_warehouse.items():
		in_stock = _actual_quantities(warehouse, codes)  # Bin.actual_qty clamped at 0, one query
		# Other orders' Stock Reservation Entries are not this note's stock (#499); its own orders' are.
		held = order_reservation.reserved_by_others(warehouse, codes, own_orders)
		for item_code in codes:
			in_stock[item_code] = max(flt(in_stock[item_code]) - held[item_code], 0)
			qty = wanted[(item_code, warehouse)]
			if qty - flt(in_stock[item_code]) > 0.0001:
				problems.append(
					_("Item {0}: {1} to deliver but only {2} in stock in warehouse {3}.").format(
						item_code, text(qty), text(in_stock[item_code]), warehouse
					)
				)
	role = frappe.get_single_value("Stock Settings", "role_allowed_to_over_deliver_receive")
	if not (role and role in frappe.get_roles()):
		for detail, qty in ordered.items():
			line = frappe.db.get_value(
				"Sales Order Item", detail, ["item_code", "qty", "delivered_qty", "parent"], as_dict=True
			)
			if not line:
				continue
			allowance = get_allowance_for(line.item_code)[0]
			left = flt(line.qty) * (100 + flt(allowance)) / 100 - flt(line.delivered_qty)
			if qty - left > 0.0001:
				problems.append(
					_("Item {0}: {1} to deliver but only {2} left on Sales Order {3}.").format(
						line.item_code, text(qty), text(left), line.parent
					)
				)
	if problems:
		frappe.throw(" ".join(problems), exc=NotDeliverable, title=_("The Delivery Note cannot be confirmed"))


def check_ready(doc):
	"""Raise what ``request_create`` would refuse before it contacts the carrier (the "Create Shipment" Workflow
	transition runs this inside the transition, so a note that cannot be sent never leaves Order Received). The
	locked check in ``request_create`` still runs once more."""
	adapter = adapter_for(doc)
	if not adapter:
		frappe.throw(_("This Delivery Note has no carrier shipment."))
	if doc.docstatus != 0 or status_of(doc) not in EDITABLE_STATUSES:
		frappe.throw(_("The shipment of this Delivery Note was already requested."))
	if not cstr(doc.get("custom_carrier_reference")):
		check_deliverable(doc)
	needs = adapter.validate_ready(doc)
	if needs and needs.get("needs_city"):
		frappe.throw(
			_("{0} needs the city of the delivery address. Choose it (Pick {0} City), save the Delivery Note, then create the shipment again.").format(
				adapter.SERVICE_NAME
			)
		)
	if needs:
		frappe.throw(_("{0} needs more information before the shipment can be created.").format(adapter.SERVICE_NAME))


def request_create(delivery_note, modified=None, submit_as=None, discard_on_failure=False):
	"""Start the create of a draft note (the API has checked the submit permission). Idempotent: a note that
	is already Creating or Created only returns its status. When the adapter answers that it needs the
	person's input first, nothing is sent and that answer is returned.

	``submit_as`` is the user who pressed Submit: only ``request_shipment`` for a delivery sets it. The job
	then submits the note as that user once the carrier confirms Created (delivery-note-first.md). Retries,
	checks, the nightly job and return pickups never pass it, so they never submit.

	``discard_on_failure`` is set only by ``order_shipment.ship_order`` for a draft note it just MADE: the create job
	then deletes the draft when the carrier definitively refuses it (``discard_draft``, as ``submit_as``). No other
	path passes it, so a note made any other way is never deleted by a failure."""
	doc = frappe.get_doc("Delivery Note", delivery_note)
	adapter = adapter_for(doc)
	if not adapter:
		frappe.throw(_("This Delivery Note has no carrier shipment."))
	if modified and cstr(doc.modified) != cstr(modified) and status_of(doc) in EDITABLE_STATUSES:
		frappe.throw(_("The Delivery Note was changed. Reload it and try again."), frappe.TimestampMismatchError)
	if status_of(doc) not in (CREATING, CREATED):
		# A stored carrier reference means a create was tried before (Creation Unknown, a lookup-first retry): the
		# shipment may exist at the carrier, so the retry must reach the lookup whatever the stock is now.
		if not cstr(doc.get("custom_carrier_reference")):
			check_deliverable(doc)
		needs = adapter.validate_ready(doc)
		if needs:
			return {"status": status_of(doc), "queued": False, **needs}
	# Commit, then lock as the very next statement (MariaDB snapshot isolation).
	frappe.db.commit()
	locked = frappe.db.get_value(
		"Delivery Note",
		delivery_note,
		["custom_integration_status", "docstatus", "modified"],
		as_dict=True,
		for_update=True,
	)
	if locked.docstatus != 0 or (cstr(locked.custom_integration_status) or NOT_CREATED) in (CREATING, CREATED):
		frappe.db.commit()
		return {"status": cstr(locked.custom_integration_status) or NOT_CREATED, "queued": False}
	# What was validated above may have changed before the lock (a save, an edit): judge the locked row.
	try:
		if modified and cstr(locked.modified) != cstr(modified):
			frappe.throw(_("The Delivery Note was changed. Reload it and try again."), frappe.TimestampMismatchError)
		locked_doc = frappe.get_doc("Delivery Note", delivery_note)
		if not cstr(locked_doc.get("custom_carrier_reference")):
			check_deliverable(locked_doc)
		if adapter.validate_ready(locked_doc):
			frappe.throw(_("The Delivery Note was changed. Reload it and try again."), frappe.TimestampMismatchError)
	except Exception:
		frappe.db.commit()
		raise
	# A reference on the note means a create was tried before: look the shipment up first (durable, unlike
	# the log rows). It is stored from the first attempt on and cleared on an amended note.
	previous = cstr(frappe.db.get_value("Delivery Note", delivery_note, "custom_carrier_reference"))
	reference = previous or doc.name
	started = {
		"custom_integration_status": CREATING,
		"custom_carrier_reference": reference,
		"custom_last_integration_error": None,
	}
	if submit_as and not doc.get("is_return") and getattr(adapter, "SUBMIT_AT_PICKUP", False):
		# #500: the person who asked for the shipment is who the pickup event submits the note as.
		started["custom_shipment_requested_by"] = submit_as
	frappe.db.set_value("Delivery Note", delivery_note, lifecycle.with_state(delivery_note, started))
	# A return note the carrier owns is a pickup request: same machine, its own operation name.
	operation = "Return Pickup" if doc.get("is_return") else "Create"
	log = open_log(
		adapter.SERVICE_NAME,
		delivery_note,
		operation,
		attempt=attempt_number(adapter.SERVICE_NAME, delivery_note, operation),
		reference=reference,
	)
	extra = {}
	if discard_on_failure and not doc.get("is_return"):
		extra = {"discard_on_failure": True}
	frappe.enqueue(
		"marketplace_integrations.services.carrier_shipments.run_create",
		queue="short",
		timeout=120,
		job_id=_CREATE_JOB.format(adapter.SHIPPING_CARRIER.lower(), delivery_note),
		deduplicate=True,
		enqueue_after_commit=True,
		delivery_note=delivery_note,
		log=log,
		lookup_first=bool(previous) or bool(getattr(adapter, "LOOKUP_FIRST", False)),
		submit_as=None if doc.get("is_return") else submit_as,
		**extra,
	)
	frappe.db.commit()
	return {"status": CREATING, "queued": True}


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


def run_create(delivery_note, log=None, lookup_first=True, submit_as=None, discard_on_failure=False):
	"""Create (or find) the shipment of one Creating note and write the result back. ``lookup_first`` is
	False only for the very first attempt of a note (``request_create`` decides it). ``submit_as`` (the user
	who pressed Submit) makes the job submit the note, as that user, once the carrier confirmed Created.
	``discard_on_failure``: a DEFINITE refusal (``Failed``) deletes the draft note after it was marked Creation
	Failed (``discard_draft``); Unknown outcomes and unexpected errors always keep it."""
	frappe.set_user("Administrator")
	if status_of(delivery_note) != CREATING:
		frappe.db.commit()
		return
	adapter = None
	try:
		doc = frappe.get_doc("Delivery Note", delivery_note)
		adapter = adapter_for(doc)
		frappe.db.commit()
		data = adapter.find_remote(doc) if lookup_first else None
		if data is None:
			data = adapter.create_remote(doc)
		values = adapter.created_values(doc, data)
		if _finish(
			delivery_note,
			CREATED,
			values,
			log,
			"Success",
			data=adapter.log_data(values),
			delivery_id=values.get("custom_carrier_delivery_id"),
		):
			adapter.after_created(delivery_note, data)
			if submit_as and not getattr(adapter, "SUBMIT_AT_PICKUP", False):
				# Submitted as soon as Created, unless the carrier's note stays a draft until its pickup event (#500).
				_submit_created(delivery_note, submit_as, adapter.SERVICE_NAME)
	except Failed as failure:
		written = _finish(
			delivery_note, FAILED, {"custom_last_integration_error": cstr(failure)}, log, "Failed", error=cstr(failure)
		)
		if written and discard_on_failure:
			discard_draft(delivery_note, cstr(failure), requested_by=submit_as)
	except Unknown as unknown:
		_finish(delivery_note, UNKNOWN, {"custom_last_integration_error": cstr(unknown)}, log, "Unknown", error=cstr(unknown))
	except Exception:
		frappe.db.rollback()
		service = adapter.SERVICE_NAME if adapter else _("carrier")
		frappe.log_error(title=f"{service} shipment from Delivery Note failed", message=frappe.get_traceback())
		message = _(
			"Unexpected error while sending the shipment to {0}; see the Error Log. It may or may not have reached {0}."
		).format(service)
		_finish(delivery_note, UNKNOWN, {"custom_last_integration_error": message}, log, "Unknown", error=message)


PICKUP_SUBMIT_JOB = "pickup-submit|{}|{}"
#: Set on the document by the pickup job: the only submit of a ``SUBMIT_AT_PICKUP`` note that
#: ``require_shipment_created`` lets through.
PICKUP_SUBMIT_FLAG = "pickup_submit"
#: The pickup submit is tried this many times, each retry a separate job on the long queue that pauses first (a
#: bounded wait that must not hold a short-queue worker; this Frappe has no delayed enqueue). After the last failure
#: the error stays stored for the form and Needs Attention (Retry).
PICKUP_SUBMIT_ATTEMPTS = 3
PICKUP_SUBMIT_PAUSE_SECONDS = 30


def queue_submit_at_pickup(delivery_note, attempt=1):
	"""Queue the deduplicated job that submits a Created draft whose carrier picked it up (#500). Attempt 1 runs on
	the short queue; a retry (``attempt`` > 1) runs on the long queue and waits first. Queued after the caller's
	commit; the job decides again from the stored row, so a repeat is harmless."""
	frappe.enqueue(
		"marketplace_integrations.services.carrier_shipments.run_submit_at_pickup",
		queue="short" if attempt <= 1 else "long",
		timeout=300,
		job_id=PICKUP_SUBMIT_JOB.format(delivery_note, attempt),
		deduplicate=True,
		enqueue_after_commit=True,
		delivery_note=delivery_note,
		attempt=attempt,
	)


def run_submit_at_pickup(delivery_note, attempt=1):
	"""The job: submit the Created draft as ``custom_shipment_requested_by`` (never as the system, no
	``ignore_permissions``: the Authorization Control and credit limit rules judge the requester), then, when the
	carrier already reported the goods returned, queue the draft return exactly as a submitted note would. When the
	submit fails and ``attempt`` is below ``PICKUP_SUBMIT_ATTEMPTS`` the next attempt is queued."""
	frappe.set_user("Administrator")
	if attempt > 1:
		# a transient cause (a stock race, a lock) may be gone shortly; this is a long-queue worker
		time.sleep(PICKUP_SUBMIT_PAUSE_SECONDS)
	values = frappe.db.get_value(
		"Delivery Note",
		delivery_note,
		["custom_shipment_requested_by", "docstatus", "custom_integration_status", "custom_shipping_status", "is_return"],
		as_dict=True,
	)
	if not values or values.docstatus != 0 or cstr(values.custom_integration_status) != CREATED or values.is_return:
		return
	adapter = _adapter_of(delivery_note)
	# No requester recorded (a note whose shipment was started before #500): the note's owner.
	requester = cstr(values.custom_shipment_requested_by) or frappe.db.get_value("Delivery Note", delivery_note, "owner")
	_submit_created(
		delivery_note,
		requester,
		adapter.SERVICE_NAME,
		_("{0} picked the shipment up; the Delivery Note could not be confirmed: {1}"),
		pickup=True,
	)
	docstatus = frappe.db.get_value("Delivery Note", delivery_note, "docstatus")
	if docstatus == 0 and attempt < PICKUP_SUBMIT_ATTEMPTS:
		queue_submit_at_pickup(delivery_note, attempt + 1)
		frappe.db.commit()
	elif docstatus == 1:
		after = getattr(adapter, "after_pickup_submit", None)
		if after:
			after(delivery_note)
			frappe.db.commit()


def _submit_created(delivery_note, submit_as, service, failure=None, pickup=False):
	"""Submit a note whose shipment is Created, as ``submit_as`` (the requester the shipment was started for; never as
	the system: the Authorization Control and credit limit rules judge that user). Never raises: a failure leaves a Draft
	note that is Created, with the reason in ``custom_last_integration_error`` (Needs Attention); a note can
	be submitted by hand, except one that waits for the carrier's pickup event, which only the pickup job submits (Retry in Needs Attention queues it again).
	``failure`` is the translated message template ``{0}`` service, ``{1}`` reason. ``pickup``: the submit of
	the pickup job, the only one ``require_shipment_created`` allows for a note that waits for its pickup."""
	# Lock order, the same as ``order_edit.apply_edit``: the Sales Order rows first, then the note. Read the orders
	# of the note and commit, then lock as the very next statement (MariaDB snapshot isolation), so a submit and an
	# Edit Order of the same order wait for each other instead of deadlocking.
	frappe.db.commit()
	orders = sorted(
		{
			name
			for name in frappe.get_all(
				"Delivery Note Item",
				filters={"parent": delivery_note, "parenttype": "Delivery Note"},
				pluck="against_sales_order",
				parent_doctype="Delivery Note",
				limit_page_length=0,
			)
			if name
		}
	)
	frappe.db.commit()
	for name in orders:
		frappe.db.get_value("Sales Order", name, "name", for_update=True)
	locked = frappe.db.get_value(
		"Delivery Note", delivery_note, ["docstatus", "custom_integration_status"], as_dict=True, for_update=True
	)
	if not locked or locked.docstatus != 0 or cstr(locked.custom_integration_status) != CREATED:
		frappe.db.commit()
		return
	try:
		frappe.set_user(submit_as)
		note = frappe.get_doc("Delivery Note", delivery_note)
		if pickup:
			note.flags[PICKUP_SUBMIT_FLAG] = True
		note.submit()
		frappe.db.commit()
		if failure:
			# A retry that worked: the earlier "could not be confirmed" reason no longer applies.
			frappe.set_user("Administrator")
			frappe.db.set_value("Delivery Note", delivery_note, "custom_last_integration_error", None, update_modified=False)
			frappe.db.commit()
	except Exception as exc:
		frappe.db.rollback()
		if not isinstance(exc, frappe.ValidationError):
			frappe.log_error(title="Automatic submit of a Delivery Note failed", message=f"{delivery_note}\n{frappe.get_traceback()}")
		frappe.clear_messages()
		reason = strip_html_tags(cstr(exc)).strip()[:300] or exc.__class__.__name__
		message = (failure or _("Shipment created at {0}; the Delivery Note could not be confirmed: {1}")).format(
			service, reason
		)
		frappe.set_user("Administrator")
		frappe.db.set_value("Delivery Note", delivery_note, "custom_last_integration_error", message, update_modified=False)
		frappe.db.commit()
	finally:
		frappe.set_user("Administrator")


def _finish(delivery_note, status, values, log, result, expected=CREATING, **log_values):
	"""Close the log and write the note back, only if it is still ``expected`` (Creating for a create,
	Cancellation Pending for a cancel). Commit, lock as the next statement, write with ``db_set`` (system
	write-back, notifies open forms). Returns whether the note was written. The error text is stored raw: it
	is escaped where it is rendered."""
	close_log(log, result, **log_values)
	frappe.db.commit()
	locked = frappe.db.get_value("Delivery Note", delivery_note, "custom_integration_status", for_update=True)
	if cstr(locked) != expected:
		frappe.db.commit()
		return False
	doc = frappe.get_doc("Delivery Note", delivery_note)
	doc.db_set(
		lifecycle.with_state(delivery_note, dict(values, custom_integration_status=status)),
		update_modified=True,
		notify=True,
		commit=True,
	)
	return True


DISCARDED_KEY = "shipment-discarded|{}"
DISCARDED_TTL_SECONDS = 3600


def discard_draft(delivery_note, reason, requested_by=None):
	"""Delete a DRAFT note whose shipment DEFINITELY failed (our pre-check refused it, or the carrier refused the
	create: Integration Status Not Created / Creation Failed), because the one-step flow that made it must not leave it
	behind. The caller decides it is the flow's own note and that the failure is definite. Never raises; returns whether
	the note was deleted (False: it is kept, nothing changed).

	``requested_by`` is the user who triggered the flow: the note is deleted only when THAT user may delete Delivery
	Notes (no user, no delete). Without the permission the note is kept; a Creation Failed note then gets a line saying
	it could not be removed, next to the carrier's reason.

	The delete itself runs as the system. It keeps the audit trail in two places
	(the note itself is gone): a comment on each Sales Order, and a short-lived cache entry
	(``discarded_notice``) so a waiting form or bulk run can still show the reason. The Sales Invoice of a paid order
	is never touched. Lock order as ``_submit_created``: the Sales Orders, then the note."""
	try:
		frappe.db.commit()
		orders = sorted(
			{
				name
				for name in frappe.get_all(
					"Delivery Note Item",
					filters={"parent": delivery_note, "parenttype": "Delivery Note"},
					pluck="against_sales_order",
					parent_doctype="Delivery Note",
					limit_page_length=0,
				)
				if name
			}
		)
		frappe.db.commit()
		for name in orders:
			frappe.db.get_value("Sales Order", name, "name", for_update=True)
		locked = frappe.db.get_value(
			"Delivery Note", delivery_note, ["docstatus", "custom_integration_status"], as_dict=True, for_update=True
		)
		if not locked or locked.docstatus != 0 or (cstr(locked.custom_integration_status) or NOT_CREATED) not in EDITABLE_STATUSES:
			frappe.db.commit()
			return False
		if not (requested_by and frappe.has_permission("Delivery Note", "delete", doc=delivery_note, user=requested_by)):
			if (cstr(locked.custom_integration_status) or NOT_CREATED) == FAILED:
				kept = _("The Delivery Note could not be removed because you do not have permission to delete it.")
				stored = cstr(frappe.db.get_value("Delivery Note", delivery_note, "custom_last_integration_error"))
				frappe.db.set_value(
					"Delivery Note",
					delivery_note,
					"custom_last_integration_error",
					f"{stored} {kept}".strip(),
					update_modified=False,
				)
			frappe.db.commit()
			return False
		adapter = adapter_for(frappe.get_doc("Delivery Note", delivery_note))
		service = adapter.SERVICE_NAME if adapter else _("carrier")
		frappe.delete_doc("Delivery Note", delivery_note, ignore_permissions=True)
		reason = strip_html_tags(cstr(reason)).strip()[:300]
		text = _("Delivery Note {0} was deleted because the {1} shipment could not be created: {2}").format(
			delivery_note, service, reason
		)
		for name in orders:
			frappe.get_doc("Sales Order", name).add_comment("Comment", text)
		frappe.cache().set_value(
			DISCARDED_KEY.format(delivery_note),
			{"reason": reason, "user": requested_by, "service": service, "orders": orders},
			expires_in_sec=DISCARDED_TTL_SECONDS,
		)
		frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		frappe.log_error(title="Deleting the draft of a failed shipment failed", message=f"{delivery_note}\n{frappe.get_traceback()}")
		return False
	return True


def discarded_notice(delivery_note):
	"""``{"reason", "user", "service", "orders"}`` of a note ``discard_draft`` deleted in the last hour, else ``None``."""
	return frappe.cache().get_value(DISCARDED_KEY.format(delivery_note))


# ---------------------------------------------------------------------------
# Check (lookup only, never creates)
# ---------------------------------------------------------------------------


def is_stale_creating(delivery_note):
	"""A Creating note with no queued job whose last change is older than ``STALE_CREATING_MINUTES``."""
	values = frappe.db.get_value(
		"Delivery Note",
		delivery_note,
		["custom_integration_status", "modified", "is_return", "transporter", "custom_return_source"],
		as_dict=True,
	)
	if not values or cstr(values.custom_integration_status) != CREATING:
		return False
	adapter = adapter_for(values)
	if adapter and is_job_enqueued(_CREATE_JOB.format(adapter.SHIPPING_CARRIER.lower(), delivery_note)):
		return False
	return get_datetime(values.modified) <= add_to_date(now_datetime(), minutes=-STALE_CREATING_MINUTES)


def check(delivery_note):
	"""Ask the carrier whether a Creation Unknown (or stuck Creating) note's shipment exists. Found: Created.
	Otherwise the note stays Creation Unknown: this never creates. The result is written only if the note
	is still as it was when the check started. The caller checked write access. The returned ``message``
	may hold the carrier's text: the caller escapes it where it is shown."""
	adapter = _adapter_of(delivery_note)
	service = adapter.SERVICE_NAME
	start = frappe.db.get_value(
		"Delivery Note", delivery_note, ["custom_integration_status", "modified", "custom_carrier_reference"], as_dict=True
	)
	status = cstr(start.custom_integration_status) or NOT_CREATED
	if status == CREATING and not is_stale_creating(delivery_note):
		frappe.throw(_("The shipment is still being sent to {0}. Check again in a few minutes.").format(service))
	if status not in (CREATING, UNKNOWN):
		frappe.throw(_("Only a note with an unknown {0} result can be checked.").format(service))
	reference = start.custom_carrier_reference or delivery_note
	doc = frappe.get_doc("Delivery Note", delivery_note)
	adapter.validate_ready(doc, "lookup")
	frappe.db.commit()
	log = open_log(service, delivery_note, "Refresh", reference=reference)
	frappe.db.commit()
	changed = {
		"status": status,
		"found": False,
		"message": _("The Delivery Note changed while checking with {0}. Reload it and check again.").format(service),
	}
	try:
		data = adapter.find_remote(doc)
	except Unknown as unknown:
		message = cstr(unknown)
		if not _resolve_unknown(delivery_note, start, UNKNOWN, {"custom_last_integration_error": message}, log, "Unknown", error=message):
			return changed
		return {"status": UNKNOWN, "found": False, "message": message}
	if data is None:
		message = _("{0} has no delivery with this reference yet. The result is still unknown.").format(service)
		if not _resolve_unknown(delivery_note, start, UNKNOWN, {"custom_last_integration_error": message}, log, "Unknown", error=message):
			return changed
		return {"status": UNKNOWN, "found": False, "message": message}
	values = adapter.created_values(doc, data)
	if not _resolve_unknown(
		delivery_note, start, CREATED, values, log, "Success", delivery_id=values.get("custom_carrier_delivery_id")
	):
		return changed
	adapter.after_created(delivery_note, data)
	return {"status": CREATED, "found": True}


def _resolve_unknown(delivery_note, start, status, values, log, result, **log_values):
	"""Like ``_finish`` but for a note that is Creation Unknown or a stuck Creating: ``start`` is the
	status and ``modified`` read before the carrier call; nothing is written if either changed since (a
	retry, another check or the worker may have moved the note on). Returns whether it was written."""
	close_log(log, result, **log_values)
	frappe.db.commit()
	locked = frappe.db.get_value(
		"Delivery Note", delivery_note, ["custom_integration_status", "modified"], as_dict=True, for_update=True
	)
	if (
		not locked
		or cstr(locked.custom_integration_status) != cstr(start.custom_integration_status)
		or cstr(locked.modified) != cstr(start.modified)
	):
		frappe.db.commit()
		return False
	frappe.get_doc("Delivery Note", delivery_note).db_set(
		lifecycle.with_state(delivery_note, dict(values, custom_integration_status=status)),
		update_modified=True,
		notify=True,
		commit=True,
	)
	return True


# ---------------------------------------------------------------------------
# Cancel (#410)
# ---------------------------------------------------------------------------


def cancel(delivery_note):
	"""Cancel the note's shipment at the carrier (the API checked the cancel / write permission). The ERPNext
	cancel or delete comes after, natively, and is allowed by ``guard_cancel_or_delete`` only once the
	Integration Status is Cancelled.

	Synchronous (no lock held across the carrier call). Created / Cancellation Unknown -> Cancellation
	Pending (committed) -> Cancelled, Cancellation Unknown (the adapter raised ``Unknown``) or back to
	Created with the refusal in ``custom_last_integration_error``. A note that is already Cancelled is
	answered without calling the carrier: this is how a failed local cancel is retried.

	Returns ``{"status", "outcome", "message"}``; ``outcome`` is ``cancelled``, ``refused`` or ``unknown`` and
	``message`` is the carrier's raw text (the caller escapes it where it is shown)."""
	adapter = _adapter_of(delivery_note)
	service = adapter.SERVICE_NAME
	# Commit, then lock as the very next statement (MariaDB snapshot isolation).
	frappe.db.commit()
	locked = frappe.db.get_value(
		"Delivery Note",
		delivery_note,
		["custom_integration_status", "docstatus", "modified", "custom_carrier_reference"],
		as_dict=True,
		for_update=True,
	)
	status = cstr(locked.custom_integration_status) or NOT_CREATED
	if status == CANCELLED and locked.docstatus != 2:
		frappe.db.commit()
		return {"status": CANCELLED, "outcome": "cancelled", "message": ""}
	too_new = status == CANCELLATION_PENDING and get_datetime(locked.modified) > add_to_date(
		now_datetime(), minutes=-STALE_CANCEL_MINUTES
	)
	if (
		locked.docstatus == 2
		or too_new
		or status not in (CREATED, CANCELLATION_PENDING, CANCELLATION_UNKNOWN)
	):
		frappe.db.commit()
		if too_new:
			frappe.throw(_("{0} is already being asked to cancel this shipment. Check again in a minute.").format(service))
		frappe.throw(_("Only a note with a created {0} shipment can be cancelled at {0}.").format(service))
	doc = frappe.get_doc("Delivery Note", delivery_note)
	try:
		adapter.validate_ready(doc, "cancel")
		if doc.docstatus == 1:
			# The carrier cancel cannot be undone: refuse it while a linked document (a submitted Sales
			# Invoice, a Sales Return) would make the native cancel of the note fail afterwards.
			_check_native_cancel_not_blocked(doc)
	except Exception:
		frappe.db.commit()
		raise
	doc.db_set(
		lifecycle.with_state(
			delivery_note, {"custom_integration_status": CANCELLATION_PENDING, "custom_last_integration_error": None}
		),
		update_modified=True,
		notify=True,
	)
	log = open_log(service, delivery_note, "Cancel", reference=cstr(locked.custom_carrier_reference) or delivery_note)
	frappe.db.commit()

	http_status = None
	try:
		result = adapter.cancel_remote(doc)
	except Unknown as unknown:
		outcome, message, http_status, extra = "unknown", cstr(unknown), unknown.http_status, {}
	else:
		outcome, message, http_status, extra = result.outcome, result.message, result.http_status, result.get("values") or {}

	if outcome == "cancelled":
		values = {"custom_last_integration_error": None, "custom_last_sync_at": now_datetime(), **extra}
		new_status, log_result = CANCELLED, "Success"
	elif outcome == "refused":
		values, new_status, log_result = {"custom_last_integration_error": message}, CREATED, "Failed"
	else:
		values, new_status, log_result = {"custom_last_integration_error": message}, CANCELLATION_UNKNOWN, "Unknown"
	written = _finish(
		delivery_note,
		new_status,
		values,
		log,
		log_result,
		expected=CANCELLATION_PENDING,
		error=message if outcome != "cancelled" else None,
		http_status=http_status,
	)
	if not written:
		# A webhook or a refresh moved the note on while the carrier was asked; report what is stored now.
		return {"status": status_of(delivery_note), "outcome": outcome, "message": message}
	if new_status == CANCELLED:
		adapter.after_cancelled(delivery_note)
	return {"status": new_status, "outcome": outcome, "message": message}


# ---------------------------------------------------------------------------
# Automatic native cancel after the carrier cancelled
# ---------------------------------------------------------------------------


def cancel_after_carrier_cancelled(delivery_note):
	"""Called by an adapter right after its refresh, webhook or nightly read wrote the Integration Status
	Cancelled (the carrier itself confirmed the cancel; never on Unknown or Pending). A submitted note is
	then cancelled natively (``doc.cancel()``) with no call to the carrier. The cancel runs in a short
	background job (``run_auto_cancel``, queued after the caller's commit, one per note), so it runs as the
	system without changing the session user of the request or job that read the carrier. Idempotent (a
	cancelled or draft note is left alone) and never raises: the caller's own work (a log row, the next
	note of a nightly run) must not fail because of it.

	Blocked (a linked submitted document) or failing: the note stays submitted, its
	``custom_last_integration_error`` holds the reason (Needs Attention "Carrier Exception") and the people
	in ``_notify_recipients`` are told a manual step is needed, once per distinct reason."""
	try:
		frappe.enqueue(
			"marketplace_integrations.services.carrier_shipments.run_auto_cancel",
			queue="short",
			timeout=300,
			job_id=f"auto-cancel|{delivery_note}",
			deduplicate=True,
			enqueue_after_commit=True,
			delivery_note=delivery_note,
		)
	except Exception:
		frappe.log_error(title="Queueing the automatic cancel of a Delivery Note failed", message=f"{delivery_note}\n{frappe.get_traceback()}")


def run_auto_cancel(delivery_note):
	"""The job of ``cancel_after_carrier_cancelled``: runs as the system. Never raises."""
	frappe.set_user("Administrator")
	try:
		_auto_cancel(delivery_note)
	except Exception:
		frappe.db.rollback()
		frappe.log_error(title="Automatic cancel of a carrier-cancelled Delivery Note failed", message=f"{delivery_note}\n{frappe.get_traceback()}")


def _auto_cancel(delivery_note):
	# Commit, then lock as the very next statement (MariaDB snapshot isolation). The lock is held until the
	# cancel commits, so a concurrent webhook and the nightly job cancel the note once.
	frappe.db.commit()
	locked = frappe.db.get_value(
		"Delivery Note",
		delivery_note,
		["custom_integration_status", "docstatus", "custom_last_integration_error"],
		as_dict=True,
		for_update=True,
	)
	if not locked or locked.docstatus != 1 or cstr(locked.custom_integration_status) != CANCELLED:
		frappe.db.commit()
		return
	# Read after the lock: the document is current, so the cancel meets no "modified after you opened it".
	doc = frappe.get_doc("Delivery Note", delivery_note)
	adapter = adapter_for(doc)
	if not adapter:
		frappe.db.commit()
		return
	service = adapter.SERVICE_NAME
	try:
		# The job runs as the system: the cancel does not depend on whoever read the carrier. The invoice
		# cancels and the note cancel share this transaction: any failure below rolls both back.
		cancelled_invoices = _cancel_linked_sales_invoices(doc)
		if cancelled_invoices:
			doc.reload()  # the invoice cancel rewrote the note's billing status
		_check_native_cancel_not_blocked(doc)
		doc.flags.ignore_permissions = True
		# The carrier cancelled the shipment (not a person): the order cancel hook (order_cancel_cascade) reads it.
		doc.flags.cancelled_by_carrier = True
		doc.cancel()
	except Exception as exc:
		frappe.db.rollback()
		if not isinstance(exc, frappe.ValidationError):
			frappe.log_error(title="Automatic cancel of a Delivery Note failed", message=f"{delivery_note}\n{frappe.get_traceback()}")
		frappe.clear_messages()
		reason = strip_html_tags(cstr(exc)).strip()[:300] or exc.__class__.__name__
		message = _("{0} cancelled the shipment, but this Delivery Note could not be cancelled automatically: {1}").format(service, reason)
		if cstr(locked.custom_last_integration_error) != message:
			frappe.db.set_value("Delivery Note", delivery_note, "custom_last_integration_error", message, update_modified=False)
			frappe.db.commit()
			_notify(
				doc,
				_("Delivery Note {0}: {1} cancelled the shipment but the note must be cancelled by hand. {2}").format(
					delivery_note, service, reason
				),
			)
		# The notification is queued on this transaction: a later rollback by the caller must not lose it.
		frappe.db.commit()
		return
	frappe.db.commit()
	adapter.after_cancelled(delivery_note)
	if cancelled_invoices:
		subject = _("Delivery Note {0} was cancelled automatically because {1} cancelled the shipment. Cancelled with it: {2}").format(
			delivery_note, service, ", ".join(cancelled_invoices)
		)
	else:
		subject = _("Delivery Note {0} was cancelled automatically because {1} cancelled the shipment.").format(delivery_note, service)
	_notify(doc, subject)
	frappe.db.commit()


#: ``invoice_blocker`` kinds: the linked document that holds a payment or return, or an invoice that is not fully unpaid / POS.
PAYMENT_ENTRY, JOURNAL_ENTRY, RETURN, UNPAID = "Payment Entry", "Journal Entry", "Sales Invoice", "unpaid"


def invoice_blocker(name, values=None):
	"""Why a submitted Sales Invoice must not be cancelled automatically, else ``None``: ``(kind, linked document)``
	where kind is ``PAYMENT_ENTRY``, ``JOURNAL_ENTRY`` (a submitted one references the invoice), ``RETURN`` (a credit
	note against it) or ``UNPAID`` (POS, or ``outstanding_amount`` differs from the total, so it is paid or partly
	paid; the linked document is ``""``). The one payment rule shared by the carrier auto-cancel and the Sales Order
	cancel cascade (``order_cancel_cascade``). ``values`` (``outstanding_amount``, ``is_pos``, ``rounded_total``,
	``grand_total``) may be passed when the caller already read them under a lock."""
	linked = frappe.get_all(
		"Payment Entry Reference",
		filters={"reference_doctype": "Sales Invoice", "reference_name": name, "docstatus": 1},
		pluck="parent",
		limit=1,
	)
	if linked:
		return PAYMENT_ENTRY, linked[0]
	linked = frappe.get_all(
		"Journal Entry Account",
		filters={"reference_type": "Sales Invoice", "reference_name": name, "docstatus": 1},
		pluck="parent",
		limit=1,
	)
	if linked:
		return JOURNAL_ENTRY, linked[0]
	linked = frappe.get_all("Sales Invoice", filters={"return_against": name, "docstatus": 1}, pluck="name", limit=1)
	if linked:
		return RETURN, linked[0]
	values = values or frappe.db.get_value(
		"Sales Invoice", name, ["outstanding_amount", "is_pos", "rounded_total", "grand_total"], as_dict=True
	)
	precision = frappe.get_precision("Sales Invoice", "outstanding_amount")
	total = values.rounded_total or values.grand_total
	if values.is_pos or flt(values.outstanding_amount, precision) != flt(total, precision):
		return UNPAID, ""
	return None


def _cancel_linked_sales_invoices(doc):
	"""Cancel, natively, the submitted Sales Invoices that bill this note (Sales Invoice Item.delivery_note), so
	the note's own cancel is no longer blocked by them. Called only by the automatic cancel, in the same
	transaction as the note cancel (nothing is committed here). A return note's invoice is a credit note and is
	cancelled the same way.

	Each invoice is locked first, then refused (``frappe.ValidationError``, nothing is cancelled and the caller
	rolls back) when it also bills another note or a line without a note (cancelling it would undo more than
	this shipment), is a POS invoice, or is not exactly unpaid (``outstanding_amount`` differs from its total;
	credit notes carry negative totals, so the same test holds). A submitted Payment Entry, Journal Entry or
	Sales Return against it is checked first only to name the linked document in the message. The payment
	check is explicit because ERPNext's native cancel does not refuse it: with Accounts Settings "Unlink
	Payment on Cancellation of Invoice" on (the default) it cancels the invoice and silently unlinks the
	payment, so the money would be left unallocated.

	Each cancelled invoice gets a comment naming the note. Returns the cancelled invoice names."""
	invoices = sorted(
		frappe.get_all(
			"Sales Invoice Item",
			filters={"delivery_note": doc.name, "docstatus": 1, "parenttype": "Sales Invoice"},
			pluck="parent",
			distinct=True,
		)
	)
	for name in invoices:
		# Lock first: a payment entered meanwhile waits for this transaction instead of slipping past the checks.
		values = frappe.db.get_value(
			"Sales Invoice",
			name,
			["outstanding_amount", "is_pos", "rounded_total", "grand_total"],
			as_dict=True,
			for_update=True,
		)
		line = {"parent": name, "parenttype": "Sales Invoice"}
		if frappe.db.count("Sales Invoice Item", {**line, "delivery_note": ["!=", doc.name]}) or frappe.db.count(
			"Sales Invoice Item", {**line, "delivery_note": ["is", "not set"]}
		):
			frappe.throw(
				_("Sales Invoice {0} also bills other Delivery Notes or items without one, so it is not cancelled automatically.").format(name)
			)
		blocker = invoice_blocker(name, values)
		if blocker and blocker[0] != UNPAID:
			frappe.throw(
				_("Sales Invoice {0} is linked with {1}, so it is not cancelled automatically.").format(name, blocker[1])
			)
		if blocker:
			frappe.throw(
				_("Sales Invoice {0} is a POS invoice or is not fully unpaid, so it is not cancelled automatically.").format(name)
			)
	for name in invoices:
		invoice = frappe.get_doc("Sales Invoice", name)
		invoice.flags.ignore_permissions = True
		invoice.cancel()
		invoice.add_comment(
			"Comment",
			_("Cancelled automatically with Delivery Note {0} because the carrier cancelled the shipment.").format(doc.name),
		)
	return invoices


def _submitter(doc):
	"""Who submitted the note: ``modified_by`` is whoever wrote it last (often the system), so the Version
	row that changed ``docstatus`` to 1 is read; the creator when there is none."""
	for version in frappe.get_all(
		"Version",
		filters={"ref_doctype": "Delivery Note", "docname": doc.name},
		fields=["owner", "data"],
		order_by="creation desc",
		limit_page_length=20,
	):
		try:
			changed = json.loads(version.data or "{}").get("changed") or []
		except ValueError:
			continue
		if any(len(row) >= 3 and row[0] == "docstatus" and row[2] == 1 for row in changed):
			return version.owner
	return doc.owner


def _notify_recipients(doc):
	"""The submitter plus every enabled user with the Stock Manager role, each once."""
	candidates = set(frappe.get_all("Has Role", filters={"role": "Stock Manager", "parenttype": "User"}, pluck="parent"))
	candidates.add(_submitter(doc))
	candidates.discard("Guest")
	candidates.discard(None)
	if not candidates:
		return []
	return frappe.get_all("User", filters={"name": ["in", list(candidates)], "enabled": 1}, pluck="name")


def notify_stock_managers(delivery_note, subject):
	"""One native Notification Log per enabled Stock Manager, linked to ``delivery_note``. Must not raise."""
	from frappe.desk.doctype.notification_log.notification_log import enqueue_create_notification

	try:
		users = frappe.get_all(
			"User",
			filters={
				"name": ["in", frappe.get_all("Has Role", filters={"role": "Stock Manager", "parenttype": "User"}, pluck="parent") or [""]],
				"enabled": 1,
			},
			pluck="name",
		)
		users = [user for user in users if user != "Guest"]
		if users:
			enqueue_create_notification(
				users, {"type": "Alert", "subject": subject, "document_type": "Delivery Note", "document_name": delivery_note}
			)
	except Exception:
		frappe.log_error(title="Delivery Note notification failed", message=f"{delivery_note}\n{frappe.get_traceback()}")


def _notify(doc, subject):
	"""One native Notification Log per recipient, linked to the Delivery Note. Must not raise."""
	from frappe.desk.doctype.notification_log.notification_log import enqueue_create_notification

	try:
		users = _notify_recipients(doc)
		if users:
			enqueue_create_notification(
				users, {"type": "Alert", "subject": subject, "document_type": "Delivery Note", "document_name": doc.name}
			)
	except Exception:
		frappe.log_error(title="Delivery Note auto-cancel notification failed", message=f"{doc.name}\n{frappe.get_traceback()}")


# ---------------------------------------------------------------------------
# Nightly
# ---------------------------------------------------------------------------


def _check_native_cancel_not_blocked(doc):
	"""Raise the same link error the native cancel of a submitted note raises (the checks ``Document.cancel``
	runs), before anything is cancelled at the carrier. Like ERPNext's ``DeliveryNote.on_cancel`` it ignores
	the documents the cancel itself reverses (ledger rows) or cancels first (Packing Slips), or every note
	that moved stock would be refused."""
	from frappe.model.delete_doc import check_if_doc_is_dynamically_linked, check_if_doc_is_linked

	doc.ignore_linked_doctypes = NATIVE_CANCEL_IGNORED_LINKS
	check_if_doc_is_linked(doc, method="Cancel")
	check_if_doc_is_dynamically_linked(doc, method="Cancel")
	# ERPNext's own cancel refuses a note whose Sales Order is Closed or On Hold (not checked for a return)
	if not doc.get("is_return") and hasattr(doc, "check_sales_order_on_hold_or_close"):
		doc.check_sales_order_on_hold_or_close("against_sales_order")


#: Documents the native delete removes itself in its own ``on_trash`` (Frappe's wildcard ``doc_events`` runs
#: ``workflow_action.process_workflow_actions``, which deletes the note's Workflow Action rows) BEFORE its link
#: check. This early check runs before that cleanup, so it must not count them as links.
NATIVE_DELETE_CLEARED_LINKS = ("Workflow Action",)


def _native_delete_is_forced(doc):
	"""True when this delete runs under ``frappe.delete_doc(..., force=True)``. ``delete_doc`` keeps ``force`` in a
	local variable (it sets no flag), so the only way to see it from ``on_trash`` is the calling ``delete_doc`` frame."""
	if doc.flags.get("force") or frappe.flags.get("force"):
		return True
	from frappe.model.delete_doc import delete_doc as native_delete_doc

	frame = sys._getframe(1)
	while frame:
		if frame.f_code is native_delete_doc.__code__:
			return bool(frame.f_locals.get("force"))
		frame = frame.f_back
	return False


def _check_native_delete_not_blocked(doc):
	"""Raise the link error the native delete of a draft note raises (``frappe.model.delete_doc``), before the
	carrier is contacted. Skipped for a forced delete, which the native delete does not check either. It uses
	Frappe's own link lookups (so ``ignore_links_on_delete`` applies) and, like the native delete, does not count
	the rows its ``on_trash`` clears (``NATIVE_DELETE_CLEARED_LINKS``)."""
	if _native_delete_is_forced(doc):
		return
	from frappe.model.delete_doc import get_dynamic_linked_docs, get_linked_docs, raise_link_exists_exception

	for link in (*get_linked_docs(doc, "Delete"), *get_dynamic_linked_docs(doc, "Delete")):
		if link["reference_doctype"] not in NATIVE_DELETE_CLEARED_LINKS:
			raise_link_exists_exception(
				doc, link["reference_doctype"], link["reference_docname"], link.get("at_position", "")
			)


def reconcile_open_notes():
	"""Nightly (``59 23 * * *``, ``hooks.py``): each carrier reconciles its own open notes. One carrier's
	failure does not stop the others."""
	for adapter in CARRIERS.values():
		try:
			adapter.reconcile()
		except Exception:
			frappe.db.rollback()
			frappe.log_error(title=f"{adapter.SERVICE_NAME} reconciliation could not start", message=frappe.get_traceback())
