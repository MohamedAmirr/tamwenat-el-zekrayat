# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Bulk "create Delivery Notes" from the Sales Order list, shipped by Bosta (docs/marketplace-integrations/
delivery-note-first.md, "Bulk path").

``api/bulk_delivery_notes.py`` authorizes and queues ``run_bulk``; the job handles one order at a time and
one order never stops the others. Everything per order (eligibility, guards, payment position and COD, the draft note,
the stock check, the shipment request) is ``services/order_shipment.py``, the same service as the Sales Order form's
"Create Shipment": this module only loops, watches and reports. THIS MODULE NEVER SUBMITS A DELIVERY NOTE and never
calls Bosta itself; no Sales Invoice is made (decided 2026-11-09: a Bosta order is invoiced when it is Delivered).

When every order was handled the job watches the queued notes for a bounded time (``WAIT_SECONDS``): a note whose
Integration Status reached Created is "accepted by Bosta" (#500: the draft is the end state); Unknown is a failure
with Bosta's reason and the draft KEPT; a definite refusal is a failure whose draft note the create job already DELETED
(``carrier_shipments.discard_draft``: the row no longer links it; read through ``order_shipment.shipment_outcome``); a
note still Creating when the wait ends is reported "still creating".

Progress and summary go to the requesting user on the realtime event ``EVENT`` (one payload shape, ``stage``
"creating" / "waiting" / "done"); the final summary is also kept as a native Notification Log.

If the shipment request is refused (a missing city, phone or address ...) for a note this run made, the draft note is
deleted and the row names the reason (a reused draft note of the person is kept and named).
"""

import time

import frappe
from frappe import _
from frappe.desk.doctype.notification_log.notification_log import enqueue_create_notification
from frappe.utils import cstr, escape_html, strip_html_tags

from marketplace_integrations.services.order_shipment import (
	ACCEPTED,
	BOSTA,
	CREATING,
	FAILED,
	OUTCOME_ACCEPTED,
	OUTCOME_DISCARDED,
	OUTCOME_FAILED,
	OUTCOME_MISSING,
	SENT,
	SKIPPED,
	STILL_CREATING,
	WAITING,
	Refused,
	_invoices,
	_result,
	check_permissions,
	ineligible_reason,
	live_note,
	payment_plan,
	process_order,
	ship_order,
	shipment_outcome,
)

MAX_ORDERS = 50
#: The shipping methods the dialog offers. Noon and Manual delivery are decided later.
SHIPPING_METHODS = (BOSTA,)
EVENT = "bulk_delivery_note_progress"
JOB_ID = "bulk-delivery-notes|{}"
#: How long the job watches the queued notes, and how often. The create jobs run on the short queue, so this
#: job (long queue) never waits for itself.
WAIT_SECONDS = 120
POLL_SECONDS = 3


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
			if rows[index]["status"] == SKIPPED:
				hide_unreadable_note(rows[index])
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
	while True:
		changed = False
		# A new read view each round, or the job would not see the status the create job commits.
		frappe.db.commit()
		for row in rows:
			if row["status"] != SENT:
				continue
			outcome = shipment_outcome(row["delivery_note"])
			state = outcome["state"]
			if state == OUTCOME_ACCEPTED:
				# #500: a Bosta note stays a draft until pickup, so Created at Bosta is the accepted end state.
				row["status"], row["message"] = ACCEPTED, _("Accepted by Bosta")
			elif state == OUTCOME_FAILED:
				# Refused or unknown: the draft note is kept, the row links it.
				row["status"] = FAILED
				row["message"] = _("Bosta did not accept the shipment: {0}").format(outcome["reason"][:300])
			elif state == OUTCOME_DISCARDED:
				# Refused for good: the draft note was deleted, so the row no longer links it.
				row["message"] = _("Bosta did not accept the shipment: {0}. The Delivery Note {1} was deleted.").format(
					outcome["reason"][:300], row["delivery_note"]
				)
				row["status"], row["delivery_note"] = FAILED, None
			elif state == OUTCOME_MISSING:
				row["status"] = FAILED
				row["message"] = _("The Delivery Note {0} no longer exists.").format(row["delivery_note"])
				row["delivery_note"] = None
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


def _can_read_note(name):
	"""True when the Delivery Note exists and the current user may read it (a name is not shown otherwise)."""
	return bool(frappe.db.exists("Delivery Note", name)) and bool(
		frappe.has_permission("Delivery Note", "read", doc=name)
	)


def hide_unreadable_note(row):
	"""A refusal that names a Delivery Note the user cannot read (a colleague's note in the way) must not reveal
	it: the row loses the link and gets a message without the name. Returns the row."""
	if row.get("delivery_note") and not _can_read_note(row["delivery_note"]):
		row["delivery_note"] = None
		row["message"] = _("A Delivery Note that you cannot open already exists for this order.")
	return row


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
				if row.get(key) and (key != "delivery_note" or _can_read_note(row[key]))
			)
			lines.append(
				f"<li>{escape_html(row['sales_order'])}: {escape_html(row['message'])}"
				+ (f" ({escape_html(documents)})" if documents else "")
				+ "</li>"
			)
		# Link a note that stays: an accepted one, else a kept (failed or skipped) one. Never a still-creating note,
		# which the create job may delete, and never one the user cannot read.
		linked = next(
			(
				row["delivery_note"]
				for status in (ACCEPTED, FAILED, SKIPPED)
				for row in rows
				if row["status"] == status and row.get("delivery_note") and _can_read_note(row["delivery_note"])
			),
			None,
		)
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
