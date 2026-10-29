# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Noon shipment created from a draft Delivery Note (#391, epic #392).

Delivery Note-first fulfillment (``docs/marketplace-integrations/delivery-note-first.md``): a
person makes a draft Delivery Note from a submitted Noon Sales Order (Create > Delivery Note) and
saves it. Saving queues ``run()`` (an ``after_insert`` ``doc_events`` handler that never raises
and makes no HTTP call, see ``services/carrier_shipments.py``). The worker picks the units by SKU
and quantity from the Delivery Note rows, writes the Pending shipment, reuses
``fulfillment.run_create_shipment`` for the Noon calls and writes the result back to the Delivery
Note. The app never submits the Delivery Note.

Native ERPNext fields are used instead of new ones: ``transporter`` (Supplier "Noon", created by
a patch) and ``lr_no`` / ``lr_date`` (the AWB). Only the status and the error text are custom
(``custom_carrier_status``, ``custom_carrier_error``). A typed ``lr_no`` is sent as the sticker AWB.

Rules enforced here:

- ``guard_changes`` (Delivery Note ``validate``): once a shipment row exists for the note
  (Pending, Created or Error), ``transporter`` and the item rows (item, quantity, add, remove)
  cannot change, so the note always matches the units sent to Noon.
- ``require_created`` (``before_submit``): a Noon Delivery Note cannot be submitted until its
  shipment is Created.
- The worker never holds the Marketplace Order row lock across a Noon call (it relies on
  ``fulfillment.py``'s commit-before-lock pattern) and writes the Delivery Note with ``db_set``, so
  no Delivery Note hook runs again. A refused or failed attempt keeps the draft, shows the error
  on the note (``custom_carrier_error``) and in Needs Attention, and **Retry** re-queues it.
- Retry and a duplicate job reuse the note's existing shipment row (same shipment number), so
  nothing is created twice.

Background work: retries and backoff are those of ``run_create_shipment`` (a failed Noon call
ends in Error, the user retries); validation refusals (too few shippable units, missing mapping,
an Item mapped to several Noon SKUs on the order, a typed AWB already in use) are terminal until
the user fixes the note or the mapping. The job id is per Delivery Note, deduplicated.
"""

import frappe
from frappe import _
from frappe.utils import add_to_date, cstr, escape_html, flt, get_datetime, getdate, now_datetime, strip_html
from frappe.utils.background_jobs import is_job_enqueued

from marketplace_integrations.integrations.noon import fulfillment, handover_state
from marketplace_integrations.services.mappings import resolve_item

#: ``supplier_name`` of the native Supplier used as the Delivery Note ``transporter`` (created by a
#: patch). The Supplier's document name depends on Buying Settings ``supp_master_name``, so it is
#: looked up by this name (``transporter_name()``), never assumed to be the same string.
SUPPLIER_NAME = "Noon"
#: A Pending note older than this (since its last modification) counts as stuck and can be retried.
#: Mirrored in ``public/js/delivery_note.js``; the server is the gate.
STALE_PENDING_MINUTES = 15

PENDING = "Pending"
CREATED = "Created"
ERROR = "Error"
#: The note's Noon shipment was cancelled or discarded; Retry Noon Shipment creates a new one.
CANCELLED = "Cancelled"

#: Shipment statuses that still hold units / exist on Noon (the same set as fulfillment's active one).
_LIVE_STATUSES = tuple(sorted(fulfillment._ACTIVE_SHIPMENT_STATUSES))
#: Shipment row statuses that tie the note to its shipment (transporter, lr_no, status and items are
#: locked), Cancel Pending included: a cancel that Noon has not answered yet must not drift from the note.
_LOCKING_STATUSES = _LIVE_STATUSES
_JOB_PREFIX = "marketplace-noon-delivery-note-shipment"


class _Refused(Exception):
	"""A readable reason the shipment cannot be created from this note (shown to the user)."""


# ---------------------------------------------------------------------------
# Which notes belong to Noon
# ---------------------------------------------------------------------------


def noon_order_name(doc):
	"""The Noon Marketplace Order of a draft/new Delivery Note whose rows all come from one
	submitted, Noon-linked Sales Order, else ``None`` (returns and every other note are not ours)."""
	if doc.get("is_return"):
		return None
	sales_orders = {row.get("against_sales_order") for row in doc.get("items") or []}
	if len(sales_orders) != 1 or not next(iter(sales_orders)):
		return None
	values = frappe.db.get_value(
		"Sales Order", next(iter(sales_orders)), ["docstatus", "custom_marketplace_order"], as_dict=True
	)
	if not values or values.docstatus != 1 or not values.custom_marketplace_order:
		return None
	if frappe.db.get_value("Marketplace Order", values.custom_marketplace_order, "platform") != "Noon":
		return None
	return values.custom_marketplace_order


def applies_to(doc):
	return bool(noon_order_name(doc))


def transporter_name():
	"""The Supplier docname of "Noon" (looked up by ``supplier_name``), or ``None`` when missing."""
	return frappe.db.get_value("Supplier", {"supplier_name": SUPPLIER_NAME}, "name")


def _shipment_rows(order_name, delivery_note, statuses=None):
	"""Shipment rows of the order that belong to ``delivery_note``: its first note (``delivery_note``)
	or any note in the row's ``delivery_notes`` list (an old-flow shipment can hold several)."""
	filters = {"parent": order_name, "parenttype": "Marketplace Order"}
	if statuses:
		filters["status"] = ["in", list(statuses)]
	rows = frappe.get_all(
		"Marketplace Order Shipment",
		filters=filters,
		or_filters=[
			["delivery_note", "=", delivery_note],
			["delivery_notes", "like", f'%"{delivery_note}"%'],
		],
		fields=[
			"name",
			"integration_shipment_nr",
			"status",
			"last_error",
			"delivery_note",
			"delivery_notes",
			"handed_over_on",
		],
		parent_doctype="Marketplace Order",
		order_by="idx desc",
	)
	return [row for row in rows if delivery_note in handover_state.shipment_notes(row)]


def _flow_rows(order_name, delivery_note, statuses=None):
	"""The note's shipment rows that were created from the note (this flow). A row with
	``handed_over_on`` belongs to the old Mark Handed Over flow and is left to its own rules. Everything
	that guards or serves a Delivery Note decides from these rows, never from the editable
	``custom_carrier_status``."""
	return [row for row in _shipment_rows(order_name, delivery_note, statuses) if not row.handed_over_on]


# ---------------------------------------------------------------------------
# Document rules (called from services/carrier_shipments.py)
# ---------------------------------------------------------------------------


def _rows(doc):
	return sorted((cstr(row.name), cstr(row.item_code), flt(row.qty, 6)) for row in doc.get("items") or [])


def saved_state(doc):
	"""The note as stored (what decides ownership for an existing note: a row added in the form
	without a Sales Order must not take the note out of the guard), else ``doc`` itself."""
	return doc if doc.is_new() else (doc.get_doc_before_save() or doc)


def guard_changes(doc):
	"""``validate``: show Pending on a new note; refuse changes the shipment depends on."""
	before = saved_state(doc)
	order_name = noon_order_name(before)
	if not order_name:
		return
	if doc.is_new():
		if not doc.get("custom_carrier_status"):
			doc.custom_carrier_status = PENDING
			doc.custom_carrier_error = None
		if doc.get("amended_from") and doc.get("lr_no"):
			# Amend copies the old note's AWB, which belongs to the shipment of that note and would
			# be sent as a typed sticker AWB (and refused as used). A different AWB typed by the
			# user is kept.
			if doc.lr_no == frappe.db.get_value("Delivery Note", doc.amended_from, "lr_no"):
				doc.lr_no = None
				doc.lr_date = None
		return
	if not _flow_rows(order_name, doc.name, _LOCKING_STATUSES):
		return
	if cstr(before.get("custom_carrier_status")) != cstr(doc.get("custom_carrier_status")):
		# The worker writes the status with db_set (no validate); a person must not edit it away.
		frappe.throw(
			_("The carrier status cannot be changed by hand after the Noon shipment was started."),
			title=_("Noon shipment"),
		)
	if cstr(before.get("transporter")) != cstr(doc.get("transporter")):
		frappe.throw(
			_("The carrier cannot be changed after the Noon shipment was started."),
			title=_("Noon shipment"),
		)
	if cstr(before.get("lr_no")) != cstr(doc.get("lr_no")):
		frappe.throw(
			_("The AWB / Tracking No cannot be changed after the Noon shipment was started."),
			title=_("Noon shipment"),
		)
	if _rows(before) != _rows(doc):
		frappe.throw(
			_(
				"Items, quantities and rows cannot be changed after the Noon shipment was started. "
				"They must match the units sent to Noon."
			),
			title=_("Noon shipment"),
		)


def require_created(doc):
	"""``before_submit``: a Noon Delivery Note needs a Created shipment."""
	order_name = noon_order_name(doc)
	if not order_name:
		return
	if _shipment_rows(order_name, doc.name, [CREATED]):
		return
	if not doc.get("custom_carrier_status") and not _shipment_rows(order_name, doc.name):
		return  # a Noon note from before this flow (no status, no shipment): submits as it always did
	frappe.throw(
		_("The Noon shipment of this Delivery Note is not Created yet, so it cannot be submitted."),
		title=_("Noon shipment"),
	)


def queue(doc):
	"""Queue the worker for ``doc`` after the current transaction commits (one job per note)."""
	frappe.enqueue(
		run,
		queue="long",
		timeout=900,
		enqueue_after_commit=True,
		job_id=f"{_JOB_PREFIX}-{doc.name}",
		deduplicate=True,
		delivery_note=doc.name,
	)


def _is_stale_pending(modified):
	return get_datetime(modified) <= add_to_date(now_datetime(), minutes=-STALE_PENDING_MINUTES)


def retry(delivery_note):
	"""Re-queue a draft Delivery Note whose shipment failed (Error) or is stuck (Pending for longer
	than ``STALE_PENDING_MINUTES``). The caller checked write access.

	Safe against a job that is still running: the job id is per note and deduplicated (a queued or
	running job is not queued twice), the worker locks the note, reuses the note's shipment row and
	asks Noon (GetShipment) before creating, so a second run cannot create a second shipment."""
	values = frappe.db.get_value(
		"Delivery Note",
		delivery_note,
		["docstatus", "custom_carrier_status", "modified"],
		as_dict=True,
	)
	if values and values.docstatus == 0 and values.custom_carrier_status == PENDING:
		if not _is_stale_pending(values.modified):
			frappe.throw(
				_("The Noon shipment is still being created. You can retry if it is not finished after {0} minutes.").format(
					STALE_PENDING_MINUTES
				)
			)
	elif not values or values.docstatus != 0 or values.custom_carrier_status not in (ERROR, CANCELLED):
		frappe.throw(_("Only a draft Delivery Note with a failed, cancelled or stuck Noon shipment can be retried."))
	return requeue(delivery_note)


def requeue(delivery_note):
	"""Show Pending on the draft note (restarts its stale clock) and queue the worker."""
	_write_back(delivery_note, {"custom_carrier_status": PENDING, "custom_carrier_error": None})
	queue(frappe._dict(name=delivery_note))
	return {"queued": True}


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


def run(delivery_note):
	"""Create (or resume) the Noon shipment of one draft Delivery Note and write the result back."""
	frappe.set_user("Administrator")
	if frappe.db.get_value("Delivery Note", delivery_note, "docstatus") != 0:
		frappe.db.commit()
		return  # deleted, submitted or cancelled meanwhile
	order_name = noon_order_name(frappe.get_doc("Delivery Note", delivery_note))
	if not order_name:
		frappe.db.commit()
		return
	try:
		_run(delivery_note, order_name)
	except _Refused as refusal:
		frappe.db.rollback()
		_fail_safely(delivery_note, order_name, cstr(refusal))
	except Exception:
		frappe.db.rollback()
		frappe.log_error(title="Noon shipment from Delivery Note failed", message=frappe.get_traceback())
		_fail_safely(
			delivery_note, order_name, _("Unexpected error while creating the Noon shipment; see the Error Log.")
		)


def _fail_safely(delivery_note, order_name, message):
	"""``_fail`` can itself raise (connection disabled, a locked row); never leave the note Pending
	because of it: fall back to writing the error on the note alone, and log if even that fails."""
	try:
		_fail(delivery_note, order_name, message)
	except Exception:
		frappe.db.rollback()
		frappe.log_error(title="Noon shipment from Delivery Note: could not record the error", message=frappe.get_traceback())
		try:
			_write_back(delivery_note, {"custom_carrier_status": ERROR, "custom_carrier_error": escape_html(message)})
		except Exception:
			frappe.db.rollback()
			frappe.log_error(title="Noon shipment from Delivery Note: note left Pending", message=frappe.get_traceback())


def _run(delivery_note, order_name):
	transporter = transporter_name()
	if not transporter:
		raise _Refused(_("Supplier Noon (carrier) does not exist. Run bench migrate."))
	# Lock order is always Delivery Note first, then Marketplace Order (handover_state's on_trash
	# handler does the same). Nothing but locking reads sits between the two locks, and a person's
	# save of the note either finished before this lock or waits and then sees the shipment row.
	frappe.db.commit()
	frappe.db.get_value("Delivery Note", delivery_note, "name", for_update=True)
	order = fulfillment._lock_and_load_order(order_name, commit_first=False)
	doc = frappe.get_doc("Delivery Note", delivery_note)
	if doc.docstatus != 0 or noon_order_name(doc) != order_name:
		frappe.db.commit()
		return
	existing = _shipment_rows(order_name, doc.name, _LOCKING_STATUSES)
	if existing:
		if existing[0].status == "Cancel Pending":
			raise _Refused(_("The Noon shipment of this Delivery Note is being cancelled."))
		shipment_nr = existing[0].integration_shipment_nr
		resume = existing[0].status != CREATED
	else:
		_refuse_unless_previous_shipment_cancelled(order_name, doc)
		account, _client = _account_or_refuse(order)
		units = _pick_units(order, doc)
		try:
			shipment_nr = fulfillment.add_pending_shipment(
				order, account, units, doc.get("lr_no"), delivery_note=doc.name
			)
		except frappe.ValidationError as error:
			frappe.clear_messages()
			raise _Refused(strip_html(cstr(error))) from error
		order.save()
		resume = True
	frappe.db.commit()  # release both locks before any Noon call

	if resume:
		fulfillment.run_create_shipment(order_name, shipment_nr)

	order = fulfillment._lock_and_load_order(order_name)
	shipment = fulfillment._find_shipment_row(order, shipment_nr)
	status = shipment.status if shipment else None
	message = cstr(shipment.last_error) if shipment else ""
	awb_nr = next(
		(row.awb_nr for row in order.noon_shipment_awbs or [] if row.integration_shipment_nr == shipment_nr),
		"",
	)
	frappe.db.commit()
	if status == CREATED and awb_nr:
		_write_back(
			doc.name,
			{
				"transporter": transporter,
				"lr_no": awb_nr,
				"lr_date": getdate(),
				"custom_carrier_status": CREATED,
				"custom_carrier_error": None,
			},
		)
		return
	# run_create_shipment already logged the failure on the order (Needs Attention); only the note is left.
	_write_back(
		doc.name,
		{
			"custom_carrier_status": ERROR,
			"custom_carrier_error": escape_html(
				message
				or _("The Noon shipment is {0}; check the order's Noon Fulfillment.").format(
					_(status) if status else _("not found")
				)
			),
		},
	)


def _account_or_refuse(order):
	try:
		return fulfillment._account_and_client(order)
	except frappe.ValidationError as error:
		frappe.clear_messages()
		raise _Refused(strip_html(cstr(error))) from error


def _pick_units(order, doc):
	"""``[{"mp_item_nr", "partner_sku"}]`` for the note's rows: per Item, the row quantity (sales UOM)
	is the number of units, taken from the currently shippable units of the Item's Noon SKU."""
	wanted = {}
	for row in doc.get("items") or []:
		wanted[row.item_code] = wanted.get(row.item_code, 0) + flt(row.qty)

	order_skus = sorted(
		{
			cstr(item.get("partner_sku")).strip()
			for item in fulfillment._latest_payload_items(order)
			if isinstance(item, dict) and cstr(item.get("partner_sku")).strip()
		}
	)
	skus_by_item = {}
	for sku in order_skus:
		item = resolve_item(order.marketplace_account, sku)
		if item:
			skus_by_item.setdefault(item, []).append(sku)

	shippable_by_sku = {}
	for unit in fulfillment.get_shippable_units(order):
		shippable_by_sku.setdefault(unit["partner_sku"], []).append(unit)

	picked = []
	for item, qty in wanted.items():
		skus = skus_by_item.get(item, [])
		if not skus:
			raise _Refused(_("Item {0} has no enabled Noon product mapping for a SKU on this order.").format(item))
		if len(skus) > 1:
			raise _Refused(
				_("Item {0} is mapped to more than one Noon SKU on this order ({1}).").format(
					item, ", ".join(skus)
				)
			)
		if qty <= 0 or qty != int(qty):
			raise _Refused(_("The quantity of Item {0} must be a whole number of units.").format(item))
		available = shippable_by_sku.get(skus[0], [])
		if len(available) < qty:
			raise _Refused(
				_("Item {0} needs {1} shippable unit(s) of SKU {2}, but only {3} are available.").format(
					item, int(qty), skus[0], len(available)
				)
			)
		picked += [
			{"mp_item_nr": unit["mp_item_nr"], "partner_sku": unit["partner_sku"]}
			for unit in available[: int(qty)]
		]
	return picked


def _fail(delivery_note, order_name, message):
	"""Refusal before any shipment row exists: show it on the note and in Needs Attention."""
	order = fulfillment._lock_and_load_order(order_name)
	fulfillment._log_fulfillment(
		order, "CreateShipment", "Error", {"delivery_note": delivery_note}, response_data=message
	)
	_write_back(delivery_note, {"custom_carrier_status": ERROR, "custom_carrier_error": escape_html(message)})


def _write_back(delivery_note, values):
	"""Write the result on the draft Delivery Note without running its hooks, bumping ``modified``
	so a stale open form cannot save over it, and tell open forms to refresh."""
	if not frappe.db.exists("Delivery Note", delivery_note):
		frappe.db.commit()
		return
	doc = frappe.get_doc("Delivery Note", delivery_note)
	doc.db_set(values, update_modified=True, notify=True, commit=True)


# ---------------------------------------------------------------------------
# Cancel / delete of the note, Sales Order cancel (#393)
# ---------------------------------------------------------------------------


def _refuse_unless_previous_shipment_cancelled(order_name, doc):
	"""An amended note (``amended_from``) gets a new shipment only when the shipment of the note it
	amends is Cancelled (or Discarded): otherwise two shipments would claim the same goods."""
	previous = doc.get("amended_from")
	for _hop in range(5):
		if not previous:
			return
		live = _shipment_rows(order_name, previous, _LIVE_STATUSES)
		if live:
			raise _Refused(
				_("The Noon shipment of {0}, which this Delivery Note amends, is not Cancelled yet.").format(previous)
			)
		previous = frappe.db.get_value("Delivery Note", previous, "amended_from")


def _awb_rows_exist(order_name, shipment_nr):
	return bool(
		frappe.db.exists(
			"Marketplace Order Shipment AWB",
			{"parent": order_name, "parenttype": "Marketplace Order", "integration_shipment_nr": shipment_nr},
		)
	)


def _never_reached_noon(order_name, row):
	"""A Pending/Error shipment with no AWB row never got as far as CreateShipment (the AWB row is
	written before it), so nothing exists on Noon and it can be dropped locally."""
	return row.status in ("Pending", "Error") and not _awb_rows_exist(order_name, row.integration_shipment_nr)


def _lock_note_then_order(delivery_note, order_name):
	# Lock order: Delivery Note, then Marketplace Order (see _run).
	frappe.db.get_value("Delivery Note", delivery_note, "name", for_update=True)
	frappe.db.get_value("Marketplace Order", order_name, "name", for_update=True)


def _discard_locally(order_name, row):
	"""Mark a shipment that never reached Noon (or a failed one the user discards) Discarded, with no
	Noon call, and log DiscardShipment Success like ``fulfillment.discard_shipment`` so the earlier
	CreateShipment/GetShipment error stops showing in Needs Attention. Written in the caller's
	transaction like ``handover_state.clear_deleted_delivery_note`` (the parent's ``modified`` is
	bumped so a stale copy cannot save over it); it rolls back with that transaction."""
	frappe.db.set_value(
		"Marketplace Order Shipment", row.name, {"status": "Discarded", "last_error": None}, update_modified=False
	)
	frappe.db.set_value("Marketplace Order", order_name, "modified", now_datetime(), update_modified=False)
	order = frappe.get_doc("Marketplace Order", order_name)
	fulfillment._log_fulfillment(
		order, "DiscardShipment", "Success", {"integration_shipment_nr": row.integration_shipment_nr}, commit=False
	)


def _refusal_for(row):
	if row.status in ("Created", "Cancel Pending"):
		return _(
			"The Noon shipment of this Delivery Note must be cancelled first. On the Delivery Note form use "
			"Cancel (a dialog cancels the Noon shipment first), or on a draft the Cancel Noon Shipment "
			"button, then cancel or delete the note."
		)
	return _(
		"The Noon shipment of this Delivery Note may already exist on Noon. Use Retry Noon Shipment first "
		"(once it is Created it can be cancelled), or, if Noon keeps rejecting it, the Discard Noon "
		"Shipment button on the Delivery Note form."
	)


def guard_cancel_or_delete(doc, method=None):
	"""``before_cancel`` and ``on_trash``: refuse while a live Noon shipment created from this Delivery
	Note exists, however the cancel or delete was triggered (form, API, list bulk delete). A shipment
	that never reached Noon is discarded locally and the note proceeds. Decided from the shipment rows
	(not the editable carrier status); old-flow notes (Mark Handed Over) are not touched."""
	order_name = noon_order_name(doc)
	if not order_name or not _flow_rows(order_name, doc.name, _LIVE_STATUSES):
		return
	_lock_note_then_order(doc.name, order_name)
	for row in _flow_rows(order_name, doc.name, _LIVE_STATUSES):  # again, now under the locks
		if _never_reached_noon(order_name, row):
			_discard_locally(order_name, row)
		else:
			frappe.throw(_refusal_for(row), title=_("Noon shipment"))


def _is_pending_create(doc):
	"""The create job may still be about to make the shipment: a fresh Pending note with no shipment row."""
	return doc.get("custom_carrier_status") == PENDING and not _is_stale_pending(doc.modified)


def cancel_for_note(delivery_note):
	"""Start cancelling the live Noon shipment of a Delivery Note (queued, polled by the form dialog).

	Returns the same shape as ``cancel_state``. The caller (api) checked document-level permission."""
	frappe.db.commit()  # first statement before the locks (see fulfillment._lock_and_load_order)
	doc = frappe.get_doc("Delivery Note", delivery_note)
	order_name = noon_order_name(doc)
	if not order_name or doc.docstatus == 2:
		return {"state": "done"}
	if not _flow_rows(order_name, delivery_note, _LIVE_STATUSES):
		if _is_pending_create(doc):
			# Reporting "done" now could let the queued create job make a shipment right after.
			frappe.throw(
				_("The Noon shipment is still being created. Try again in a moment."), title=_("Noon shipment")
			)
		return {"state": "done"}
	frappe.db.commit()
	_lock_note_then_order(delivery_note, order_name)
	rows = _flow_rows(order_name, delivery_note, _LIVE_STATUSES)
	for row in rows:
		if _never_reached_noon(order_name, row):
			_discard_locally(order_name, row)
		elif row.status not in ("Created", "Cancel Pending"):
			frappe.throw(_refusal_for(row), title=_("Noon shipment"))
	frappe.db.commit()
	for row in rows:
		if row.status in ("Created", "Cancel Pending"):
			fulfillment.cancel_shipment(
				order_name, row.integration_shipment_nr, delivery_note=delivery_note, as_system=True
			)
	return cancel_state(delivery_note)


def resend_cancel(delivery_note, order_name, integration_shipment_nr):
	"""Sales Order Retry of a note's Cancel Pending shipment: through the note's own cancel job."""
	return fulfillment.cancel_shipment(
		order_name, integration_shipment_nr, delivery_note=delivery_note, as_system=True
	)


def _retry_in_flight(doc):
	if doc.get("custom_carrier_status") == PENDING:
		return True
	try:
		return bool(is_job_enqueued(f"{_JOB_PREFIX}-{doc.name}"))
	except Exception:
		return False


def discard_for_note(delivery_note):
	"""Discard the draft note's failed (Error) Noon shipment locally, with no Noon call.

	For a shipment stuck in Error (even one with an AWB row) that Noon keeps rejecting. It becomes
	Discarded, its units are released and the note goes to the state after a cancel (``lr_no`` and
	``lr_date`` cleared, Retry Noon Shipment can create a new shipment, or the note can be deleted).
	A Created shipment cannot be discarded this way: it is cancelled on Noon. Refused while a Retry is
	in flight (the note is Pending or its job is queued/running). It makes no Noon GetShipment check:
	the form's confirmation warns that a shipment Noon did create keeps existing there. The caller
	(api) checked document-level permission."""
	frappe.db.commit()  # first statement before the locks (see fulfillment._lock_and_load_order)
	doc = frappe.get_doc("Delivery Note", delivery_note)
	order_name = noon_order_name(doc)
	if not order_name or doc.docstatus != 0:
		frappe.throw(_("Only a draft Delivery Note with a failed Noon shipment can be discarded."))
	if _retry_in_flight(doc):
		frappe.throw(
			_("A Noon shipment retry is in progress for this Delivery Note. Wait for it to finish, then try again."),
			title=_("Noon shipment"),
		)
	frappe.db.commit()
	_lock_note_then_order(delivery_note, order_name)
	rows = _flow_rows(order_name, delivery_note, _LIVE_STATUSES)
	if not rows or any(row.status != ERROR for row in rows):
		frappe.throw(
			_(
				"Only a shipment in Error can be discarded. A Created shipment must be cancelled on Noon "
				"(Cancel Noon Shipment)."
			)
		)
	for row in rows:
		_discard_locally(order_name, row)
	frappe.db.commit()
	_write_back(delivery_note, _values_after_shipment_ended())
	return {"discarded": True}


def _values_after_shipment_ended():
	"""Note fields after its shipment was cancelled or discarded (``run_cancel``, ``discard_for_note``)."""
	# the old shipment's AWB must not be sent again as a typed sticker AWB
	return {"lr_no": None, "lr_date": None, "custom_carrier_status": CANCELLED, "custom_carrier_error": None}


def run_cancel(marketplace_order_name, integration_shipment_nr, delivery_note):
	"""Job: the existing Noon cancel worker, then, for a draft note whose shipment ended Cancelled,
	show that on the note so Retry Noon Shipment can create a new one."""
	fulfillment.run_cancel_shipment(marketplace_order_name, integration_shipment_nr)
	if frappe.db.get_value("Delivery Note", delivery_note, "docstatus") != 0:
		return
	order = frappe.get_doc("Marketplace Order", marketplace_order_name)
	shipment = fulfillment._find_shipment_row(order, integration_shipment_nr)
	if shipment and shipment.status == "Cancelled":
		_write_back(delivery_note, _values_after_shipment_ended())


def cancel_state(delivery_note):
	"""``{"state": "none"|"cancelling"|"error"|"live"|"done", "message"}`` for the cancel dialog.

	``done``: no live shipment is left, the note's own cancel/delete can go on. ``cancelling``: a
	cancel job is pending. ``error``: Noon refused or could not be reached (``message``). ``live``:
	a live shipment exists and no cancel was started."""
	doc = frappe.get_doc("Delivery Note", delivery_note)
	order_name = noon_order_name(doc)
	if not order_name:
		return {"state": "done"}
	rows = _flow_rows(order_name, delivery_note, _LIVE_STATUSES)
	if not rows:
		return {"state": "done"}
	for row in rows:
		# A definite Noon refusal puts the shipment back to Created with the error; an unanswered
		# attempt stays Cancel Pending (with the error when the call failed).
		if row.status in ("Cancel Pending", "Created"):
			if row.last_error:
				message = cstr(row.last_error)
				if "Traceback" in message:
					message = _("Noon could not be reached. Try again.")
				return {"state": "error", "message": message}
			if row.status == "Cancel Pending":
				return {"state": "cancelling"}
	return {"state": "live"}


def refuse_sales_order_cancel(doc, method=None):
	"""Sales Order ``before_cancel``: ERPNext only blocks on submitted links, so refuse while a draft
	Delivery Note of this order has a live Noon shipment."""
	marketplace_order = doc.get("custom_marketplace_order")
	if not marketplace_order or frappe.db.get_value("Marketplace Order", marketplace_order, "platform") != "Noon":
		return
	rows = frappe.get_all(
		"Marketplace Order Shipment",
		filters={"parent": marketplace_order, "parenttype": "Marketplace Order", "status": ["in", list(_LIVE_STATUSES)]},
		fields=["delivery_note", "delivery_notes"],
		parent_doctype="Marketplace Order",
	)
	drafts = sorted(
		{
			note
			for row in rows
			for note in handover_state.shipment_notes(row)
			if frappe.db.get_value("Delivery Note", note, "docstatus") == 0
		}
	)
	if drafts:
		frappe.throw(
			_(
				"Draft Delivery Note(s) {0} have a live Noon shipment. Cancel the Noon shipment and delete or "
				"cancel the Delivery Note first."
			).format(", ".join(drafts)),
			title=_("Noon shipment"),
		)


# ---------------------------------------------------------------------------
# Add AWB, shipment invoice, Retry from the note (#393)
# ---------------------------------------------------------------------------


def _created_shipment(delivery_note):
	doc = frappe.get_doc("Delivery Note", delivery_note)
	order_name = noon_order_name(doc)
	rows = _flow_rows(order_name, delivery_note, [CREATED]) if order_name else []
	if not rows:
		frappe.throw(_("This Delivery Note has no Created Noon shipment."))
	return order_name, rows[0].integration_shipment_nr


def add_awb_for_note(delivery_note, awb_nr):
	"""Add a replacement AWB to the note's Created shipment (the existing AddShipmentCourierAwbs flow).
	``lr_no`` is not touched: it keeps the AWB the shipment was created with."""
	order_name, shipment_nr = _created_shipment(delivery_note)
	return fulfillment.add_awb(order_name, shipment_nr, fulfillment.NOON_COURIER, awb_nr, as_system=True)


def upload_invoice_for_note(delivery_note, file_url, invoice_nr, issued_at):
	order_name, shipment_nr = _created_shipment(delivery_note)
	return fulfillment.upload_invoice(order_name, shipment_nr, file_url, invoice_nr, issued_at, as_system=True)


def retry_awb_or_invoice_for_note(delivery_note):
	"""Re-queue the note's Created shipment's pending AWB or Pending/Error invoice."""
	order_name, shipment_nr = _created_shipment(delivery_note)
	return fulfillment.retry(order_name, shipment_nr, as_system=True)


def shipment_summary(delivery_note):
	"""What the form needs to show buttons: the Created shipment's AWBs and invoice state, or ``None``."""
	doc = frappe.get_doc("Delivery Note", delivery_note)
	order_name = noon_order_name(doc)
	rows = _flow_rows(order_name, delivery_note, _LIVE_STATUSES) if order_name else []
	if not rows:
		return None
	row = rows[0]
	order = frappe.get_doc("Marketplace Order", order_name)
	shipment = fulfillment._find_shipment_row(order, row.integration_shipment_nr)
	awbs = [
		{"awb_nr": awb.awb_nr, "status": awb.status}
		for awb in order.noon_shipment_awbs or []
		if awb.integration_shipment_nr == row.integration_shipment_nr
	]
	return {
		"integration_shipment_nr": row.integration_shipment_nr,
		"status": row.status,
		"awbs": awbs,
		"invoice_status": shipment.invoice_status if shipment else None,
		"invoice_nr": shipment.invoice_nr if shipment else None,
	}
