# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Cancelling a Sales Order cancels what hangs on it (decided 2026-11-09).

Three triggers, all registered in ``hooks.py``:

1. ``cascade_cancel`` (Sales Order ``before_cancel``), for EVERY Sales Order, whoever cancels it (the form, the API, a
   system job): the submitted Sales Invoices that bill the order are cancelled first, then its submitted Delivery
   Notes. A note with a live carrier shipment first has the shipment cancelled at the carrier
   (``carrier_shipments.cancel``, the logic behind Cancel Bosta Shipment and the workflow Cancel); the note is cancelled
   only when the carrier confirmed. Everything is checked before anything is cancelled and the cancel is BLOCKED (a
   plain error naming the document) when: a Sales Invoice has a Payment Entry, Journal Entry or is paid or partly paid
   (payments are never cancelled by this code); a return exists (a credit note or a return Delivery Note, draft or
   submitted: returns are left alone); a document also holds another order's items; a note's shipment result is
   unknown; a note whose carrier shipment was already picked up (adapter ``picked_up``: the return flow applies); a note the adapter holds back for its goods to come back (adapter ``is_held``); or the user may not cancel one of the documents. A carrier that refuses stops the cancel too. The carrier
   cancel commits (``carrier_shipments.cancel`` does), so a refusal after it leaves the earlier documents cancelled: the
   error names them and cancelling the Sales Order again carries on.
2. ``queue_noon_order_cancel`` (Delivery Note ``on_cancel``): ONLY when Noon itself cancelled (the note was cancelled by
   the carrier-initiated automatic cancel, ``flags.cancelled_by_carrier``) and the stored Noon order shows no eligible
   unit left, the Sales Order is cancelled in a job after the commit, always as Administrator (the system-run automatic cancel and
   Confirm Returned, #527, set the marker; a person's own right plays no part). The
   cascade then also cancels the other notes (a live Noon shipment is cancelled at Noon through the generic carrier
   cancel). A person cancelling a note or shipment (the cancel-and-amend path for an AWB or items) never cancels the
   order. A block (a payment, a return) never undoes the note cancel: the Sales Order then stays and a comment and a
   notification say why. Other channels leave the order alive.
3. ``queue_shopify_cancel`` (Sales Order ``on_cancel``): a cancelled Shopify order is also cancelled on Shopify, in a
   job after the commit (``marketplace_outbound.request_platform_cancel`` commits and must never run inside the cancel).
   A Shopify order that cannot be cancelled (already cancelled, fulfilled, payment hold) is skipped with a warning. A
   failed push never undoes the Sales Order cancel: it is flagged for retry (Marketplace Order Needs Action, the
   "Cancel on Shopify" button on the Sales Order retries it; the failed call already wrote an Order Action Error log).

No loop: the cascade puts the Sales Order name in ``frappe.flags`` so the Delivery Notes it cancels do not queue the
Noon Sales Order cancel again, and that job exits when the order is no longer submitted.
"""

import frappe
from frappe import _
from frappe.utils import cstr, escape_html, strip_html_tags

from marketplace_integrations.services import carrier_log, order_edit
from marketplace_integrations.services import marketplace_outbound as outbound
from marketplace_integrations.services import sync_reason_codes as reasons

#: ``frappe.flags`` entry: the Sales Orders whose cascade is running in this process.
CASCADE_FLAG = "so_cancel_cascade"
#: ``frappe.flags`` entry: set by code that pushes the Shopify cancel itself (``order_cancel``), so the hook does not.
SKIP_PUSH_FLAG = "skip_platform_cancel_push"
#: Shopify requires a reason; nobody is asked on an automatic cancel.
SHOPIFY_REASON = "OTHER"

#: The carrier statuses whose shipment the carrier still has (cancelled at the carrier before the note).
_CARRIER_LIVE = (carrier_log.CREATED, carrier_log.CANCELLATION_PENDING, carrier_log.CANCELLATION_UNKNOWN)
#: The carrier does not know yet whether the shipment exists: nobody can cancel it.
_CARRIER_UNCERTAIN = (carrier_log.CREATING, carrier_log.UNKNOWN)

#: Cache key (session user, Sales Order): the user chose "Cancel in ERPNext only" for the cancel about to happen.
_SKIP_PUSH_KEY = "skip-shopify-push|{}|{}"
_SKIP_PUSH_TTL = 120
_NOTE_JOB = "noon-order-cancel|{}"
_SHOPIFY_JOB = "shopify-order-cancel|{}"


def _plain(value):
	"""Escaped plain text of an error or carrier message (Desk renders a thrown message as HTML)."""
	return escape_html(strip_html_tags(cstr(value)).strip()[:500]) or _("The carrier gave no reason.")


# ---------------------------------------------------------------------------
# 1. Sales Order before_cancel
# ---------------------------------------------------------------------------


def cascade_cancel(doc, method=None):
	"""Sales Order ``before_cancel``: cancel its Sales Invoices, then its Delivery Notes (see the module docstring)."""
	ignore = bool(doc.flags.get("ignore_permissions"))
	try:
		plan = _plan(doc, ignore)
		if not plan["invoices"] and not plan["notes"]:
			return
		_apply(doc, plan, ignore)
		_refresh_in_memory(doc)
	except Exception:
		# The cancel is refused: a "Cancel in ERPNext only" choice made for it must not wait for the next cancel.
		_forget_skip_push(doc.name)
		raise


def _forget_skip_push(sales_order):
	frappe.cache().delete_value(_SKIP_PUSH_KEY.format(frappe.session.user, sales_order))


def _plan(order, ignore):
	"""What to cancel; raises, with nothing written, when anything is in the way."""
	notes = [note for note in order_edit._notes(order.name)]
	returns = [note.name for note in notes if note.is_return]
	submitted = [note.name for note in notes if note.docstatus == 1 and not note.is_return]
	if submitted:
		returns += frappe.get_all(
			"Delivery Note",
			filters={"return_against": ["in", submitted], "docstatus": ["<", 2]},
			pluck="name",
		)
	invoices = _invoice_rows(order.name, submitted)
	cancel_invoices = [row.name for row in invoices if row.docstatus == 1 and not row.is_return]
	returns_si = [row.name for row in invoices if row.is_return]
	if cancel_invoices:
		returns_si += frappe.get_all(
			"Sales Invoice",
			filters={"return_against": ["in", cancel_invoices], "docstatus": ["<", 2], "is_return": 1},
			pluck="name",
		)
	if returns or returns_si:
		frappe.throw(
			_(
				"Sales Order {0} has a return ({1}). Returns are never cancelled automatically: handle the return first. Nothing was cancelled."
			).format(order.name, ", ".join(sorted(set(returns + returns_si)))),
			title=_("Cancel Sales Order"),
		)
	# An advance: ERPNext would cancel the order and leave the payment unallocated (same rule as order_cancel).
	advances = frappe.get_all(
		"Payment Entry Reference",
		filters={"reference_doctype": "Sales Order", "reference_name": order.name, "docstatus": 1},
		pluck="parent",
		limit=1,
	)
	if advances:
		frappe.throw(
			_(
				"Payment Entry {0} is linked to Sales Order {1}. Payments are never cancelled automatically: cancel or unlink the payment first. Nothing was cancelled."
			).format(advances[0], order.name),
			title=_("Cancel Sales Order"),
		)
	for name in cancel_invoices:
		problem = _payment_problem(name)
		if problem:
			frappe.throw(problem, title=_("Cancel Sales Order"))
		# Every line must belong to this order (directly or through one of its notes), like the carrier auto-cancel.
		stray = [
			row
			for row in frappe.get_all(
				"Sales Invoice Item",
				filters={"parent": name, "parenttype": "Sales Invoice"},
				fields=["sales_order", "delivery_note"],
			)
			if row.sales_order != order.name and row.delivery_note not in submitted
		]
		if stray:
			frappe.throw(
				_(
					"Sales Invoice {0} also bills other orders or items without an order, so it is not cancelled with this one. Nothing was cancelled."
				).format(name),
				title=_("Cancel Sales Order"),
			)
		if not ignore:
			frappe.has_permission("Sales Invoice", "cancel", doc=name, throw=True)
	for name in submitted:
		_check_note(name, order.name, ignore)
	return {"invoices": cancel_invoices, "notes": submitted}


def _invoice_rows(sales_order, note_names):
	"""The non-cancelled Sales Invoices that bill the order directly or through its Delivery Notes."""
	names = set(
		frappe.get_all(
			"Sales Invoice Item",
			filters={"sales_order": sales_order, "docstatus": ["<", 2], "parenttype": "Sales Invoice"},
			pluck="parent",
			distinct=True,
			parent_doctype="Sales Invoice",
		)
	)
	if note_names:
		names |= set(
			frappe.get_all(
				"Sales Invoice Item",
				filters={"delivery_note": ["in", note_names], "docstatus": ["<", 2], "parenttype": "Sales Invoice"},
				pluck="parent",
				distinct=True,
				parent_doctype="Sales Invoice",
			)
		)
	if not names:
		return []
	return frappe.get_all(
		"Sales Invoice",
		filters={"name": ["in", sorted(names)], "docstatus": ["<", 2]},
		fields=["name", "docstatus", "is_return"],
		order_by="name asc",
	)


def _other_orders(order_names, own):
	return sorted({name for name in order_names if name and name != own})


def _payment_problem(invoice):
	"""The message when a submitted Sales Invoice has a payment or is paid or partly paid, else ``None``."""
	from marketplace_integrations.services import carrier_shipments as shipments

	blocker = shipments.invoice_blocker(invoice)
	if not blocker:
		return None
	kind, linked = blocker
	if kind == shipments.PAYMENT_ENTRY:
		return _(
			"Sales Invoice {0} has Payment Entry {1}. Payments are never cancelled automatically: cancel or unlink the payment first. Nothing was cancelled."
		).format(invoice, linked)
	if kind == shipments.JOURNAL_ENTRY:
		return _(
			"Sales Invoice {0} has Journal Entry {1}. Payments are never cancelled automatically: cancel or unlink it first. Nothing was cancelled."
		).format(invoice, linked)
	if kind == shipments.RETURN:
		return _(
			"Sales Invoice {0} has a return ({1}). Returns are never cancelled automatically: handle the return first. Nothing was cancelled."
		).format(invoice, linked)
	return _(
		"Sales Invoice {0} is a POS invoice, paid or partly paid. Payments are never cancelled automatically: handle the payment first. Nothing was cancelled."
	).format(invoice)


def _check_note(name, sales_order, ignore):
	"""Refuse, before anything is cancelled, a note this cascade cannot cancel."""
	from marketplace_integrations.services import carrier_shipments

	note = frappe.get_doc("Delivery Note", name)
	other = _other_orders([row.get("against_sales_order") for row in note.items], sales_order)
	if other:
		frappe.throw(
			_("Delivery Note {0} also holds items of other orders ({1}), so it is not cancelled with this one. Nothing was cancelled.").format(
				name, ", ".join(other)
			),
			title=_("Cancel Sales Order"),
		)
	status = carrier_shipments.status_of(note)
	adapter = carrier_shipments.adapter_for(note)
	if adapter and status in _CARRIER_UNCERTAIN:
		frappe.throw(
			_(
				"The {0} shipment of Delivery Note {1} has an unknown result. Check it on the Delivery Note first. Nothing was cancelled."
			).format(adapter.SERVICE_NAME, name),
			title=_("Cancel Sales Order"),
		)
	picked_up = getattr(adapter, "picked_up", None)
	if adapter and status in _CARRIER_LIVE and picked_up and picked_up(note):
		# Past the point where the carrier lets the shipment be cancelled: the goods follow the return flow.
		frappe.throw(
			_(
				"The {0} shipment of Delivery Note {1} was already picked up, so the Sales Order cannot be cancelled. Create a Sales Return against the Delivery Note instead. Nothing was cancelled."
			).format(adapter.SERVICE_NAME, name),
			title=_("Cancel Sales Order"),
		)
	is_held = getattr(adapter, "is_held", None)
	if adapter and is_held and is_held(note):
		# The adapter holds the note back from the native cancel (goods still to come back): refuse up front, before
		# any other shipment of this order is cancelled at the carrier.
		frappe.throw(
			_(
				"Delivery Note {0} is waiting for its goods to come back from {1}. Use Confirm Returned on it first. Nothing was cancelled."
			).format(name, adapter.SERVICE_NAME),
			title=_("Cancel Sales Order"),
		)
	if not ignore:
		frappe.has_permission("Delivery Note", "cancel", doc=name, throw=True)
		if adapter and status in _CARRIER_LIVE:
			frappe.has_permission("Delivery Note", "write", doc=name, throw=True)


class _Stop(Exception):
	"""A carrier did not (or may not) cancel a shipment: ``args`` are the escaped message and the dialog title."""


def _apply(order, plan, ignore):
	"""Cancel the invoices, then the notes. Commits first (and again inside each carrier cancel), so locks are taken
	right after a commit and the payment check is repeated under them."""
	from marketplace_integrations.services import carrier_shipments

	frappe.db.commit()
	# Lock order: Sales Order first, then its invoices (docs/frappe-conventions.md, Concurrency).
	frappe.db.get_value("Sales Order", order.name, "name", for_update=True)
	for name in plan["invoices"]:
		frappe.db.get_value("Sales Invoice", name, "name", for_update=True)
	for name in plan["invoices"]:
		problem = _payment_problem(name)
		if problem:
			frappe.throw(problem, title=_("Cancel Sales Order"))

	running = set(frappe.flags.get(CASCADE_FLAG) or ())
	frappe.flags[CASCADE_FLAG] = running | {order.name}
	done = []
	#: What a carrier cancel's own commit made permanent: a later error rolls back the rest.
	committed = []
	try:
		for name in plan["invoices"]:
			invoice = frappe.get_doc("Sales Invoice", name)
			invoice.flags.ignore_permissions = ignore
			invoice.cancel()
			invoice.add_comment("Comment", _("Cancelled automatically with Sales Order {0}.").format(order.name))
			done.append(name)
		for name in plan["notes"]:
			note = frappe.get_doc("Delivery Note", name)
			adapter = carrier_shipments.adapter_for(note)
			if adapter and carrier_shipments.status_of(note) in _CARRIER_LIVE:
				# cancel() commits first: everything cancelled so far stays, whatever happens next.
				committed = list(done)
				result = carrier_shipments.cancel(name)
				if result["outcome"] == "refused":
					raise _Stop(
						_("{0} did not cancel the shipment of Delivery Note {1}, so Sales Order {2} was not cancelled: {3}").format(
							adapter.SERVICE_NAME, name, order.name, _plain(result.get("message"))
						),
						_("{0} shipment").format(adapter.SERVICE_NAME),
					)
				if result["outcome"] != "cancelled":
					raise _Stop(
						_(
							"It is not known whether {0} cancelled the shipment of Delivery Note {1}, so Sales Order {2} was not cancelled. Try again in a few minutes. {3}"
						).format(adapter.SERVICE_NAME, name, order.name, _plain(result.get("message"))),
						_("{0} shipment").format(adapter.SERVICE_NAME),
					)
				note.reload()
			note.flags.ignore_permissions = ignore
			note.cancel()
			note.add_comment("Comment", _("Cancelled automatically with Sales Order {0}.").format(order.name))
			done.append(name)
	except (_Stop, frappe.ValidationError) as exc:
		if not committed and not isinstance(exc, _Stop):
			raise
		if isinstance(exc, _Stop):
			message, title = exc.args
		else:
			message, title = _plain(exc), _("Cancel Sales Order")
		frappe.clear_messages()  # one dialog: the message below, not also the one first thrown
		if committed:
			# Only what a carrier cancel committed stays; a second cancel carries on from there.
			message = _("{0} Already cancelled before this stopped: {1}. Cancel the Sales Order again to continue.").format(
				message, ", ".join(committed)
			)
		frappe.throw(message, title=title)
	finally:
		frappe.flags[CASCADE_FLAG] = running


#: Columns that stay as the cancel set them (identity, the cancel's own timestamps and docstatus, row position).
_SKIP_COLUMNS = {"name", "owner", "creation", "modified", "modified_by", "docstatus", "idx", "parent", "parenttype", "parentfield"}


def _refresh_in_memory(order):
	"""Cancelling the invoices and notes (and the commit of a carrier cancel, which lets other writers in) rewrote
	columns of the order in the database: delivered and billed figures and statuses (ERPNext's status updater), the
	Bosta shipping summary (``after_cancelled``), anything a concurrent writer changed. The Sales Order being
	cancelled is saved from the copy loaded before them, which would write the old values back. Copy EVERY stored
	column of the header and of the item rows onto it. Child tables other than the items are not rewritten by the
	documents cancelled here."""
	header = [f for f in frappe.get_meta("Sales Order").get_valid_columns() if f not in _SKIP_COLUMNS]
	for field, value in (frappe.db.get_value("Sales Order", order.name, header, as_dict=True) or {}).items():
		order.set(field, value)
	columns = [f for f in frappe.get_meta("Sales Order Item").get_valid_columns() if f not in _SKIP_COLUMNS]
	stored = {row.name: row for row in frappe.get_all("Sales Order Item", filters={"parent": order.name}, fields=["name", *columns])}
	for row in order.items:
		if row.name in stored:
			for column in columns:
				row.set(column, stored[row.name].get(column))


# ---------------------------------------------------------------------------
# 2. Delivery Note on_cancel: Noon
# ---------------------------------------------------------------------------


def queue_noon_order_cancel(doc, method=None):
	"""Delivery Note ``on_cancel``: queue the Sales Order cancel of a NOON order, but ONLY when Noon itself cancelled:
	the note was cancelled by the carrier-initiated automatic cancel (``carrier_shipments._auto_cancel`` sets
	``flags.cancelled_by_carrier`` after a read showed every unit cancelled at Noon without this app's own cancel). A
	person cancelling a note or its shipment (the documented cancel-and-amend path for an AWB or items) never cancels
	the order. Never raises."""
	try:
		if doc.get("is_return") or not doc.flags.get("cancelled_by_carrier"):
			return
		running = set(frappe.flags.get(CASCADE_FLAG) or ())
		orders = {row.get("against_sales_order") for row in doc.get("items") or [] if row.get("against_sales_order")}
		for name in sorted(orders - running):
			values = frappe.db.get_value("Sales Order", name, ["docstatus", "custom_marketplace_order"], as_dict=True)
			if not values or values.docstatus != 1 or not values.custom_marketplace_order:
				continue
			if frappe.get_cached_value("Marketplace Order", values.custom_marketplace_order, "platform") != "Noon":
				continue
			frappe.enqueue(
				"marketplace_integrations.services.order_cancel_cascade.run_noon_order_cancel",
				queue="short",
				timeout=300,
				job_id=_NOTE_JOB.format(name),
				deduplicate=True,
				enqueue_after_commit=True,
				sales_order=name,
				delivery_note=doc.name,
				# Always the system: the note is cancelled by the system-run automatic cancel or by Confirm Returned
				# (a person with Noon User only, who must not be the user of the Sales Order cancel).
				requested_by="Administrator",
			)
	except Exception:
		frappe.log_error(title="Queueing the Noon Sales Order cancel failed", message=f"{doc.name}\n{frappe.get_traceback()}")


def _noon_cancelled_the_order(sales_order):
	"""True when the stored Noon order shows no eligible unit left (Noon cancelled the whole order). A partly
	cancelled order is the order sync's to shrink; one not refreshed yet is its to cancel, through the cascade, later."""
	from marketplace_integrations.services.sales_order_sync import _reconciliation_evidence

	name = frappe.db.get_value("Sales Order", sales_order, "custom_marketplace_order")
	return bool(name) and _reconciliation_evidence(frappe.get_doc("Marketplace Order", name)).get("cancellation") == "full"


def run_noon_order_cancel(sales_order, delivery_note, requested_by="Administrator"):
	"""The job of ``queue_noon_order_cancel``. ``requested_by`` is always Administrator: the trigger is a system
	run (a Noon cancel read by refresh, or Confirm Returned by a person who may hold no cancel rights), so there is
	no session user to scope to. The cascade then cancels the other invoices and notes and asks the carrier to cancel any
	live shipment (nothing else is sent to Noon). Never raises."""
	frappe.set_user(requested_by)
	try:
		frappe.db.commit()
		# Sales Order first, then its documents (the lock order in docs/frappe-conventions.md).
		docstatus = frappe.db.get_value("Sales Order", sales_order, "docstatus", for_update=True)
		if docstatus != 1 or frappe.db.get_value("Delivery Note", delivery_note, "docstatus") != 2:
			frappe.db.commit()
			return
		if not _noon_cancelled_the_order(sales_order):
			frappe.db.commit()
			return
		order = frappe.get_doc("Sales Order", sales_order)
		order.cancel()
		order.add_comment(
			"Comment", _("Cancelled automatically because Noon cancelled this order and Delivery Note {0} was cancelled.").format(delivery_note)
		)
		frappe.db.commit()
	except Exception as exc:
		frappe.db.rollback()
		if not isinstance(exc, frappe.ValidationError):
			frappe.log_error(title="Automatic cancel of a Noon Sales Order failed", message=f"{sales_order}\n{frappe.get_traceback()}")
		frappe.clear_messages()
		reason = strip_html_tags(cstr(exc)).strip()[:300] or exc.__class__.__name__
		try:
			frappe.get_doc("Sales Order", sales_order).add_comment(
				"Comment",
				_("Delivery Note {0} was cancelled but this Sales Order could not be cancelled automatically: {1}").format(delivery_note, reason),
			)
			frappe.db.commit()
			from marketplace_integrations.services import carrier_shipments

			carrier_shipments.notify_stock_managers(
				delivery_note,
				_("Delivery Note {0} was cancelled but its Noon Sales Order {1} must be cancelled by hand: {2}").format(
					delivery_note, sales_order, reason
				),
			)
			frappe.db.commit()
		except Exception:
			frappe.db.rollback()
			frappe.log_error(title="Noting the failed Noon Sales Order cancel failed", message=f"{sales_order}\n{frappe.get_traceback()}")


# ---------------------------------------------------------------------------
# 3. Sales Order on_cancel: Shopify
# ---------------------------------------------------------------------------


def shopify_push_choice(sales_order):
	"""``{"ask": bool}``: whether the Sales Order form asks, before a cancel, if Shopify is cancelled too (a submitted
	Shopify order the platform would still cancel). Anything else is cancelled without a question (the warning, if
	any, comes after the cancel). The API has checked the permission. A choice left over from a cancel that was refused
	is forgotten here: every new question starts clean."""
	_forget_skip_push(sales_order)
	doc = frappe.get_doc("Sales Order", sales_order)
	if doc.docstatus != 1 or not order_edit.is_shopify_order(doc.get("custom_marketplace_order")):
		return {"ask": False}
	option = outbound.get_platform_cancel_option(doc)
	return {"ask": option["state"] == outbound.ELIGIBLE and not (option["already_requested"] or option["platform_cancelled"])}


def skip_push_on_next_cancel(sales_order):
	"""The user chose "Cancel in ERPNext only": remember it for this user's next cancel of this order (``on_cancel``
	reads and deletes it; it expires on its own). Keyed by the session user and the order, so it cannot affect anyone
	else's cancel, and the API only allows it to someone who may cancel the order."""
	frappe.cache().set_value(_SKIP_PUSH_KEY.format(frappe.session.user, sales_order), 1, expires_in_sec=_SKIP_PUSH_TTL)


def queue_shopify_cancel(doc, method=None):
	"""Sales Order ``on_cancel``: cancel a Shopify order on Shopify too (job after the commit). Never raises."""
	try:
		key = _SKIP_PUSH_KEY.format(frappe.session.user, doc.name)
		chose_erp_only = frappe.cache().get_value(key)
		if chose_erp_only:
			frappe.cache().delete_value(key)
		if (
			chose_erp_only
			or frappe.flags.get(SKIP_PUSH_FLAG)
			or not order_edit.is_shopify_order(doc.get("custom_marketplace_order"))
		):
			return
		# The stored figures (the cascade has just changed them), not the copy being saved.
		option = outbound.get_platform_cancel_option(frappe.get_doc("Sales Order", doc.name))
		if option["already_requested"] or option["platform_cancelled"]:
			return
		if option["state"] != outbound.ELIGIBLE:
			frappe.msgprint(
				_("Sales Order {0} was cancelled, but the order was not cancelled on Shopify: {1} Handle it in Shopify.").format(
					doc.name, " ".join(option["reasons"]) or option["instruction"]
				),
				title=_("Shopify order"),
				indicator="orange",
			)
			return
		frappe.enqueue(
			"marketplace_integrations.services.order_cancel_cascade.run_shopify_cancel",
			queue="short",
			timeout=300,
			job_id=_SHOPIFY_JOB.format(doc.name),
			deduplicate=True,
			enqueue_after_commit=True,
			sales_order=doc.name,
			requested_by=frappe.session.user,
		)
		frappe.msgprint(_("The order is being cancelled on Shopify."), alert=True, indicator="blue")
	except Exception:
		frappe.log_error(title="Queueing the Shopify cancel failed", message=f"{doc.name}\n{frappe.get_traceback()}")


def run_shopify_cancel(sales_order, requested_by):
	"""The job of ``queue_shopify_cancel``, as the user who cancelled. A failure is flagged, never raised."""
	frappe.set_user(requested_by)
	try:
		# Not eligible any more (cancelled on Shopify meanwhile, fulfilled, a hold) is not a failed push: nothing to retry.
		option = outbound.get_platform_cancel_option(sales_order)
		if option["already_requested"] or option["platform_cancelled"]:
			return
		if option["state"] != outbound.ELIGIBLE:
			frappe.get_doc("Sales Order", sales_order).add_comment(
				"Comment",
				_("The order was not cancelled on Shopify: {0}").format(" ".join(option["reasons"]) or option["instruction"]),
			)
			frappe.db.commit()
			return
		outbound.request_platform_cancel(sales_order, SHOPIFY_REASON)
	except Exception as exc:
		# request_platform_cancel committed its own Order Action Error log row before it raised.
		frappe.db.rollback()
		if not isinstance(exc, frappe.ValidationError):
			frappe.log_error(title="Automatic Shopify cancel failed", message=f"{sales_order}\n{frappe.get_traceback()}")
		frappe.clear_messages()
		_flag_for_retry(sales_order, strip_html_tags(cstr(exc)).strip()[:300] or exc.__class__.__name__)


def _flag_for_retry(sales_order, reason):
	"""Put the Marketplace Order in Needs Action (Attention list) so the failed Shopify cancel is not forgotten; the
	Sales Order's "Cancel on Shopify" button retries it."""
	try:
		name = frappe.db.get_value("Sales Order", sales_order, "custom_marketplace_order")
		if not name:
			return
		# Lock, then load (commit first, nothing between it and the lock), so a sync saving the order meanwhile is not overwritten.
		frappe.db.commit()
		frappe.db.get_value("Marketplace Order", name, "name", for_update=True)
		order = frappe.get_doc("Marketplace Order", name)
		reasons.apply_sync_state(
			order,
			reasons.ORDER_INACTIVE,
			_("Sales Order {0} is cancelled but cancelling the order on Shopify failed: {1} Use Cancel on Shopify on the Sales Order to retry.").format(
				sales_order, reason
			),
		)
		order.flags.skip_sync_status_update = True
		order.flags.ignore_permissions = True
		order.save()
		frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		frappe.log_error(title="Flagging the failed Shopify cancel failed", message=f"{sales_order}\n{frappe.get_traceback()}")
