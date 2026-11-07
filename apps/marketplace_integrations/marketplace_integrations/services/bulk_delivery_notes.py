# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Bulk "create Delivery Notes" from the Sales Order list, shipped by Bosta (docs/marketplace-integrations/
delivery-note-first.md, "Bulk path").

``api/bulk_delivery_notes.py`` authorizes and queues ``run_bulk``; the job handles one order at a time and
one order never stops the others. Per order (``ship_order``):

1. Under a Sales Order row lock: eligibility (never raises, returns a reason), then the payment position.
2. COD (the Sales Order GRAND total is the basis everywhere for Bosta, like ``delivery_note_shipment.default_cod``):
   an unpaid order ships with COD = ``grand_total``. A fully paid order (nothing outstanding on ``rounded_total`` else ``grand_total`` minus ``advance_paid``, ERPNext's own measure) gets a
   submitted Sales Invoice (its recorded Payment Entries applied) and COD 0. A partly paid order gets
   COD = ``grand_total`` minus the paid amount (never below 0) and no invoice (a rule decided by the user).
3. In ONE transaction (the commit before the lock to the commit after the note): that invoice (paid orders
   only), then a draft Delivery Note made with ERPNext's mapper, transporter Bosta, ``custom_cod_amount`` and
   ``custom_cod_confirmed = 1``. Then, still before the commit, the neutral pre-flight check
   ``carrier_shipments.check_deliverable`` (stock of each warehouse, quantity left on the Sales Order): an
   order it refuses is Skipped with its reason. Any failure or refusal rolls the whole order back (no draft,
   no invoice, no Bosta call).
4. After the commit (no lock held): the same ``carrier_shipments.request_create`` the form's Submit uses,
   with ``submit_as`` the requesting user. It runs the adapter guards (``validate_ready``) and queues the
   Bosta create job, which submits the note once Bosta confirms Created. THIS MODULE NEVER SUBMITS A
   DELIVERY NOTE and never calls Bosta itself.
5. When every order was handled the job watches the queued notes for a bounded time (``WAIT_SECONDS``): a
   note whose Integration Status reached Created is "accepted by Bosta"; Failed / Unknown is a failure with
   Bosta's reason; a note still Creating when the wait ends is reported "still creating", and a note Created at
   Bosta that is still a draft with no stored error "still being submitted" (the submit job is late, not failed).

Progress and summary go to the requesting user on the realtime event ``EVENT`` (one payload shape, ``stage``
"creating" / "waiting" / "done"); the final summary is also kept as a native Notification Log.

If step 4 refuses (a missing city, phone or address ...), the draft note (and the invoice of a paid order,
which is correct whatever the shipping) stays committed and the result names the draft: the person finishes
it with the normal Submit.
"""

import time

import frappe
from frappe import _
from frappe.desk.doctype.notification_log.notification_log import enqueue_create_notification
from frappe.utils import cstr, escape_html, flt, strip_html_tags

from marketplace_integrations.integrations.bosta import delivery_note_shipment as bosta_shipment
from marketplace_integrations.services import carrier_log, carrier_shipments

MAX_ORDERS = 50
BOSTA = "Bosta"
#: The shipping methods the dialog offers. Noon and Manual delivery are decided later.
SHIPPING_METHODS = (BOSTA,)
OPEN_STATUSES = ("To Deliver", "To Deliver and Bill")
EVENT = "bulk_delivery_note_progress"
JOB_ID = "bulk-delivery-notes|{}"
#: How long the job watches the queued notes, and how often. The create jobs run on the short queue, so this
#: job (long queue) never waits for itself.
WAIT_SECONDS = 120
POLL_SECONDS = 3

# Per-order states.
WAITING = "waiting"  # not handled yet
CREATING = "creating"  # documents being created
SENT = "sent"  # Delivery Note submitted to the Bosta create job, waiting for Bosta
ACCEPTED = "accepted"  # the shipment reached Created at Bosta
STILL_CREATING = "still_creating"  # the wait ended and Bosta had not answered
SKIPPED = "skipped"
FAILED = "failed"


class Refused(Exception):
	"""An order that is not eligible; the message is the reason shown to the user."""


# ---------------------------------------------------------------------------
# Request (called by the API)
# ---------------------------------------------------------------------------


def parse_orders(sales_orders):
	"""The distinct Sales Order names, in order. Raises a validation error for an empty, unreadable or
	oversized batch."""
	if isinstance(sales_orders, str):
		try:
			sales_orders = frappe.parse_json(sales_orders)
		except Exception:
			sales_orders = []
	names = []
	for name in sales_orders if isinstance(sales_orders, list | tuple) else []:
		name = cstr(name).strip()
		if name and name not in names:
			names.append(name)
	if not names:
		frappe.throw(_("Select at least one Sales Order."))
	if len(names) > MAX_ORDERS:
		frappe.throw(
			_("Select at most {0} Sales Orders at a time. You selected {1}.").format(MAX_ORDERS, len(names))
		)
	return names


def check_permissions():
	"""The user needs to create and submit both documents up front (the invoice of a paid order and the
	note). The per-document checks still run when each document is saved."""
	for doctype in ("Delivery Note", "Sales Invoice"):
		for ptype in ("create", "submit"):
			if not frappe.has_permission(doctype, ptype):
				frappe.throw(
					_("You need permission to create and submit {0} to use this action.").format(_(doctype)),
					frappe.PermissionError,
				)


def queue_bulk(sales_orders, shipping_method):
	"""Validate and queue the job. Returns ``{"queued": True, "count", "run_id", "orders"}``, or
	``{"queued": False, "message"}`` when a run of this user is already queued or running."""
	names = parse_orders(sales_orders)
	if shipping_method not in SHIPPING_METHODS:
		frappe.throw(_("Choose a shipping method."))
	check_permissions()
	run_id = frappe.generate_hash(length=10)
	job = frappe.enqueue(
		"marketplace_integrations.services.bulk_delivery_notes.run_bulk",
		queue="long",
		timeout=1800,
		job_id=JOB_ID.format(frappe.session.user),
		deduplicate=True,
		sales_orders=names,
		shipping_method=shipping_method,
		requested_by=frappe.session.user,
		lang=frappe.local.lang,
		run_id=run_id,
	)
	if job is None:
		# deduplicate=True answers None when a job with this id is still queued or running
		return {
			"queued": False,
			"message": _("Your previous bulk Delivery Note run is still in progress. Wait for its result."),
		}
	return {"queued": True, "count": len(names), "run_id": run_id, "orders": names}


# ---------------------------------------------------------------------------
# Job
# ---------------------------------------------------------------------------


def run_bulk(sales_orders, shipping_method, requested_by, lang=None, run_id=None):
	"""The job: one order at a time as the requesting user, a bounded watch of the queued notes, then the
	summary. Progress goes to the requesting user's Desk. Never raises: ``process_order`` catches every
	order."""
	frappe.set_user(requested_by)
	if lang:
		frappe.local.lang = lang
	rows = [_result(name, WAITING, "") for name in sales_orders]
	total = len(rows)

	def publish(stage, **extra):
		frappe.publish_realtime(
			EVENT,
			{
				"run_id": run_id,
				"shipping_method": shipping_method,
				"stage": stage,
				"total": total,
				"rows": [dict(row) for row in rows],
				**extra,
			},
			user=requested_by,
		)

	try:
		publish("creating", processed=0)
		for index, name in enumerate(sales_orders):
			rows[index]["status"] = CREATING
			publish("creating", processed=index)
			rows[index] = process_order(name, shipping_method)
			publish("creating", processed=index + 1)
		frappe.db.commit()

		sent_total = sum(1 for row in rows if row["status"] == SENT)
		if sent_total:
			publish("waiting", sent_total=sent_total, accepted=0)
			wait_for_bosta(
				rows, lambda: publish("waiting", sent_total=sent_total, accepted=_count(rows, ACCEPTED))
			)
	except Exception as exc:
		# A crash outside process_order must not leave the dialog on "creating": every order that was not
		# finished is reported failed, and the final event and the notification still go out.
		frappe.db.rollback()
		frappe.log_error(title="Bulk Delivery Note run failed", message=frappe.get_traceback())
		reason = strip_html_tags(cstr(exc)).strip()[:300] or exc.__class__.__name__
		for row in rows:
			if row["status"] in (WAITING, CREATING, SENT):
				row["status"] = FAILED
				row["message"] = _("The bulk run stopped unexpectedly: {0}").format(reason)
	summary = summarize(rows)
	publish("done", summary=summary)
	notify_summary(requested_by, summary, rows)
	return rows


def wait_for_bosta(rows, on_change):
	"""Watch the notes this run sent to the Bosta create job until each one is Created (accepted), Failed or
	Unknown, or ``WAIT_SECONDS`` pass (the rest are "still creating"). Only reads the Integration Status: no
	call to Bosta, nothing is created or submitted here. ``on_change`` is called after every change."""
	deadline = time.monotonic() + WAIT_SECONDS
	drafts = {}  # Created at Bosta but not submitted yet: delivery note -> the stored error, if any
	while True:
		changed = False
		# A new read view each round, or the job would not see the status the create job commits.
		frappe.db.commit()
		for row in rows:
			if row["status"] != SENT:
				continue
			values = frappe.db.get_value(
				"Delivery Note",
				row["delivery_note"],
				["custom_integration_status", "custom_last_integration_error", "docstatus"],
				as_dict=True,
			)
			status = cstr(values.custom_integration_status) if values else ""
			if status == carrier_log.CREATED:
				error = strip_html_tags(cstr(values.custom_last_integration_error)).strip()
				if values.docstatus == 1:
					row["status"], row["message"] = ACCEPTED, _("Accepted by Bosta")
				elif error:
					# The automatic submit failed (stock, credit limit, authorization ...): the draft stays.
					row["status"] = FAILED
					row["message"] = _(
						"Bosta accepted the shipment but Delivery Note {0} is still a draft: {1}"
					).format(row["delivery_note"], error[:300])
				else:
					drafts[row["delivery_note"]] = True
					continue
			elif status and status != carrier_log.CREATING:
				reason = strip_html_tags(cstr(values.custom_last_integration_error)).strip() or _(status)
				row["status"] = FAILED
				row["message"] = _("Bosta did not accept the shipment: {0}").format(reason[:300])
			else:
				continue
			changed = True
		if changed:
			on_change()
		if not any(row["status"] == SENT for row in rows):
			return
		if time.monotonic() >= deadline:
			for row in rows:
				if row["status"] != SENT:
					continue
				row["status"] = STILL_CREATING
				if drafts.get(row["delivery_note"]):
					# Created, no error: the submit job is only late, not failed.
					row["message"] = _(
						"Bosta accepted the shipment and Delivery Note {0} is still being submitted. Check the Delivery Note list later."
					).format(row["delivery_note"])
				else:
					row["message"] = _("Still creating at Bosta. Check the Delivery Note list later.")
			return
		time.sleep(POLL_SECONDS)


def _count(rows, status):
	return sum(1 for row in rows if row["status"] == status)


def summarize(rows):
	return {
		"total": len(rows),
		"accepted": _count(rows, ACCEPTED),
		"failed": _count(rows, FAILED),
		"skipped": _count(rows, SKIPPED),
		"still_creating": _count(rows, STILL_CREATING),
	}


def notify_summary(user, summary, rows):
	"""Keep the summary as a native Notification Log, so it survives closing or reloading Desk. Never raises."""
	try:
		problems = [row for row in rows if row["status"] in (FAILED, SKIPPED, STILL_CREATING)]
		lines = []
		for row in problems:
			documents = ", ".join(
				f"{label} {row[key]}"
				for key, label in (
					("delivery_note", _("Delivery Note")),
					("sales_invoice", _("Sales Invoice")),
				)
				if row.get(key)
			)
			lines.append(
				f"<li>{escape_html(row['sales_order'])}: {escape_html(row['message'])}"
				+ (f" ({escape_html(documents)})" if documents else "")
				+ "</li>"
			)
		linked = next((row["delivery_note"] for row in problems + rows if row.get("delivery_note")), None)
		doc = {
			"type": "Alert",
			"subject": _(
				"Bulk Delivery Notes: {0} accepted, {1} failed, {2} skipped, {3} still creating"
			).format(summary["accepted"], summary["failed"], summary["skipped"], summary["still_creating"]),
			"email_content": f"<ul>{''.join(lines)}</ul>" if lines else "",
		}
		if linked:
			doc.update({"document_type": "Delivery Note", "document_name": linked})
		enqueue_create_notification([user], doc)
	except Exception:
		frappe.log_error(
			title="Bulk Delivery Note summary notification failed", message=frappe.get_traceback()
		)


def process_order(name, shipping_method=BOSTA):
	"""One order: a result dict, never an exception. A failure rolls back whatever this order did since the
	last commit; the previous orders are already committed."""
	try:
		return ship_order(name, shipping_method)
	except Refused as refusal:
		frappe.db.rollback()
		return _result(name, SKIPPED, cstr(refusal))
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
	reason = ineligible_reason(order)
	if reason:
		raise Refused(reason)
	plan = payment_plan(order)

	# One transaction from the commit above to the commit below: the invoice and the note are all or nothing.
	invoice = _make_paid_invoice(order) if plan["needs_invoice"] else plan["invoice"]
	note = _make_note(order, plan["cod"])
	try:
		# The same pre-flight check ``request_create`` runs, but before the commit: an order ERPNext would
		# refuse to submit (short of stock, over-delivery) is skipped with no draft and no invoice.
		carrier_shipments.check_deliverable(note)
	except frappe.ValidationError as exc:
		frappe.clear_messages()
		raise Refused(strip_html_tags(cstr(exc)).strip()) from exc
	frappe.db.commit()  # releases the Sales Order lock before anything that may call out

	result = {"delivery_note": note.name, "sales_invoice": invoice, "cod": plan["cod"]}
	try:
		frappe.has_permission("Delivery Note", "submit", doc=note, throw=True)
		answer = carrier_shipments.request_create(note.name, submit_as=frappe.session.user)
	except Exception as exc:
		frappe.db.rollback()
		return _result(
			name,
			FAILED,
			_(
				"Delivery Note {0} was created as a draft but Bosta was not started. Open it and press Submit. Reason: {1}"
			).format(note.name, _failure_reason(exc, name)),
			**result,
		)
	if answer.get("needs_city"):
		return _result(
			name,
			FAILED,
			_(
				"Delivery Note {0} was created as a draft. The Bosta city could not be found from the address: open it, pick the city and press Submit."
			).format(note.name),
			**result,
		)
	return _result(name, SENT, _("Creating at Bosta"), **result)


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


def ineligible_reason(order):
	"""Why the Sales Order cannot be shipped by this action, or ``None``. Never raises."""
	if order.docstatus != 1:
		return _("Sales Order {0} is not submitted.").format(order.name)
	if not order.get("custom_marketplace_platform"):
		return _("Sales Order {0} is not a marketplace order.").format(order.name)
	platform = bosta_shipment.excluded_platform(order.name)
	if platform:
		return _("Bosta shipments cannot be created for {0} orders: {0} fulfils its own orders.").format(
			platform
		)
	if order.status not in OPEN_STATUSES:
		return _(
			"Sales Order {0} is {1}; only To Deliver and To Deliver and Bill orders can be shipped."
		).format(order.name, _(order.status))
	live = live_note(order.name)
	if live:
		return _("Delivery Note {0} already exists for this order.").format(live)
	if not bosta_shipment.bosta_supplier():
		return _("The Bosta carrier is not set up: there is no Supplier named Bosta.")
	return None


def live_note(sales_order):
	"""A non-return, non-cancelled Delivery Note (draft or submitted) of the order, else ``None``."""
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
		return None
	notes = frappe.get_all(
		"Delivery Note",
		filters={"name": ["in", parents], "is_return": 0, "docstatus": ["<", 2]},
		pluck="name",
		limit_page_length=1,
	)
	return notes[0] if notes else None


def _invoices(sales_order, docstatus):
	# Same reasoning as ``live_note``: an invoice the user cannot read must still be seen.
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
	"""``{"cod", "paid_in_full", "needs_invoice", "invoice"}`` from the stored documents (never from the
	client). Raises ``Refused`` when the position is ambiguous.

	- Fully paid is judged by ERPNext's own measure: nothing outstanding on ``(rounded_total or grand_total)
	  - advance_paid`` (ERPNext caps a recorded payment at the rounded total), COD 0. Not paid at all: COD =
	  ``grand_total`` (Shopify's total). Partly paid: COD = ``grand_total`` minus paid, never below 0.
	- No invoice: paid = the order's ``advance_paid``. A fully paid order needs an invoice (never a COD 0
	  without one); paid up to ``grand_total`` only (a rounding difference left) is refused.
	- One submitted Sales Invoice (reused, never a second one): it must cover the WHOLE order (same grand
	  total, order billed 100%) and have no credit note, else the order is refused. Paid so far = its total
	  minus its outstanding. With an outstanding AND a payment still recorded on the order the position is
	  ambiguous, so the order is refused.
	"""
	grand_total = flt(order.grand_total, 2)
	if grand_total <= 0:
		raise Refused(
			_("Sales Order {0} has a zero total; create its Delivery Note by hand.").format(order.name)
		)
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
		needs_invoice = False
		invoice_name = invoice.name
	else:
		paid = flt(order.advance_paid, 2)
		# ERPNext caps the recorded payment of an order at its rounded total (a grand total of 494.21 rounded to
		# 494 is fully paid at 494, the 0.21 is written off): paid in full when nothing is outstanding on
		# (rounded_total or grand_total) - advance_paid.
		paid_in_full = paid > 0 and flt((flt(order.rounded_total) or grand_total) - paid, 2) <= 0
		needs_invoice = paid_in_full
		invoice_name = None
	if needs_invoice and _invoices(order.name, 0):
		raise Refused(
			_("Sales Order {0} has a draft Sales Invoice. Submit or delete it first.").format(order.name)
		)
	if paid_in_full:
		cod = 0
	else:
		cod = grand_total if paid <= 0 else max(flt(grand_total - paid, 2), 0)
		if cod <= 0:
			# Paid up to the grand total but not to the rounded one: only a rounding difference is left, and
			# COD 0 without an invoice is never shipped.
			raise Refused(
				_(
					"The recorded payment of Sales Order {0} covers its total except a rounding difference; settle the difference first."
				).format(order.name)
			)
	return {"cod": cod, "paid_in_full": paid_in_full, "needs_invoice": needs_invoice, "invoice": invoice_name}


def _make_paid_invoice(order):
	"""A submitted Sales Invoice of a fully paid order, its recorded Payment Entries applied as advances. It
	does not move stock (the Delivery Note does). Raises if the invoice does not end paid; the caller rolls
	the whole order back.

	ERPNext's ``validate`` totals the invoice before it allocates the advances, so the totals right after
	``insert`` still show the full outstanding; only the submitted, reloaded invoice is trusted."""
	from erpnext.selling.doctype.sales_order.sales_order import make_sales_invoice

	invoice = make_sales_invoice(order.name)
	invoice.update_stock = 0
	# The mapper only fetches advances when this is on (ERPNext default: off); validate then allocates them.
	invoice.allocate_advances_automatically = 1
	# Only payments allocated to this order, never an unrelated unallocated payment of the same Customer.
	invoice.only_include_allocated_payments = 1
	# The mapper sets ignore_permissions on its result: the user's own Sales Invoice rights apply here.
	invoice.flags.ignore_permissions = False
	invoice.insert()
	invoice.submit()
	invoice.reload()
	if flt(invoice.total_advance, 2) <= 0:
		frappe.throw(_("The recorded payment could not be applied to the Sales Invoice."))
	if flt(invoice.outstanding_amount, 2) > 0:
		frappe.throw(
			_("Sales Invoice {0} was not fully paid by the recorded payment (outstanding {1}).").format(
				invoice.name, flt(invoice.outstanding_amount, 2)
			)
		)
	return invoice.name


def _make_note(order, cod):
	"""The draft Delivery Note: ERPNext's mapper, transporter Bosta, the COD confirmed. The adapter's own
	``guard_changes`` (run by ``insert``) applies the COD rule and limit; nothing is copied from it. It is
	left a draft: only the Bosta job submits it, once the shipment is Created."""
	from erpnext.selling.doctype.sales_order.sales_order import make_delivery_note

	note = make_delivery_note(order.name)
	note.transporter = bosta_shipment.bosta_supplier()
	note.custom_cod_amount = cod
	note.custom_cod_confirmed = 1
	note.flags.ignore_permissions = False
	note.insert()
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
		frappe.log_error(title="Bulk Delivery Note failed", message=f"{name}\n{frappe.get_traceback()}")
		reason = strip_html_tags(cstr(exc)).strip() or exc.__class__.__name__
	frappe.clear_messages()
	return reason[:300] or exc.__class__.__name__
