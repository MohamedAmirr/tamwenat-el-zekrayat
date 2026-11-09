# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Invoice a Bosta Delivery Note once Bosta reports it Delivered (#506).

Port of the prod Server Script "Auto Invoice - Bosta On Delivered". A scheduler job
(``hooks.py``, cron ``*/10 * * * *``) queues one deduplicated long job; it finds submitted Bosta
Delivery Notes whose ``custom_shipping_status`` is "Delivered" and that are not fully billed, builds
a Sales Invoice from each with ERPNext's ``make_sales_invoice``, and SUBMITS it (the prod behaviour,
kept on purpose). The invoice is dated by the Bosta delivery time, clamped between the note's posting
date and today. A failed note is rolled back, logged once and handed to a person as a ToDo.

No Bosta call is made: the job only reads what the webhook and the reconcile already stored.
"""

import frappe
from erpnext.stock.doctype.delivery_note.delivery_note import make_sales_invoice
from frappe import _
from frappe.query_builder import DocType
from frappe.utils import add_days, getdate, nowdate, strip_html
from pypika.terms import ExistsCriterion
from rq.timeouts import BaseTimeoutException

from marketplace_integrations.integrations.bosta.delivery_note_shipment import bosta_supplier

# Only notes of the last 60 days are looked at, and at most 50 per run (the job runs every 10 minutes).
LOOKBACK_DAYS = 60
BATCH_LIMIT = 50
# The prod Server Script assigned the failure ToDo to this user. The repo has no setting or convention
# for it, so it stays a constant until the owner decides (see the PR notes).
FAILURE_TODO_ASSIGNEE = "m7med1994@gmail.com"
# Stable, untranslated marker at the start of the ToDo description: the "one open ToDo per note" check
# matches on it, so it must not depend on the language of the job's user.
FAILURE_MARKER = "[auto-invoice]"
JOB_ID = "bosta-auto-invoice-delivered"


def invoice_delivered_notes():
	"""Scheduler entry (runs as Administrator): queue one deduplicated long job. Never raises."""
	try:
		frappe.enqueue(
			"marketplace_integrations.integrations.bosta.auto_invoice.run_invoice_delivered_notes",
			queue="long",
			timeout=1800,
			job_id=JOB_ID,
			deduplicate=True,
		)
	except Exception:
		frappe.log_error(title="Auto invoice (Bosta delivered): enqueue failed")


def run_invoice_delivered_notes():
	"""Invoice up to ``BATCH_LIMIT`` delivered Bosta notes, oldest created first."""
	supplier = bosta_supplier()
	if not supplier:
		return
	today = nowdate()
	for note in _candidate_notes(supplier, today):
		try:
			_invoice_note(note, today)
		except BaseTimeoutException:
			raise
		except Exception as error:
			frappe.db.rollback()
			_report_failure(note.name, error)


def _candidate_notes(supplier, today):
	"""The oldest ``BATCH_LIMIT`` delivered, unbilled Bosta notes with no invoice line yet.

	Notes that already have a non-cancelled invoice line are excluded in the query (NOT EXISTS), so a
	note the job would only skip cannot occupy one of the 50 slots. System sweep, so no permission filter.
	"""
	note = DocType("Delivery Note")
	line = DocType("Sales Invoice Item")
	billed = (
		frappe.qb.from_(line)
		.select(line.name)
		.where(line.delivery_note == note.name)
		.where(line.docstatus < 2)
	)
	return (
		frappe.qb.from_(note)
		.select(note.name, note.posting_date, note.custom_bosta_last_event_at, note.custom_last_sync_at)
		.where(note.docstatus == 1)
		.where(note.is_return == 0)
		.where(note.transporter == supplier)
		.where(note.custom_shipping_status == "Delivered")
		.where(note.per_billed < 100)
		.where(note.posting_date >= add_days(today, -LOOKBACK_DAYS))
		.where(ExistsCriterion(billed).negate())
		.orderby(note.creation)
		.limit(BATCH_LIMIT)
		.run(as_dict=True)
	)


def _invoice_note(note, today):
	"""Create and submit the invoice of one note, or skip it when it is already billed."""
	if _already_invoiced(note.name):
		return

	delivery_note = frappe.get_doc("Delivery Note", note.name)
	sales_orders = {row.against_sales_order for row in delivery_note.items if row.against_sales_order}
	if sales_orders and _all_billed(sales_orders):
		return

	invoice_date = _invoice_date(note, today)
	invoice = make_sales_invoice(source_name=note.name)
	if not invoice.items:
		return

	invoice.update_stock = 0
	invoice.allocate_advances_automatically = 1
	invoice.only_include_allocated_payments = 1
	invoice.set_posting_time = 1
	invoice.posting_date = invoice_date
	invoice.due_date = invoice_date
	for row in invoice.get("payment_schedule") or []:
		row.due_date = invoice_date
	# Background job with no person behind it: it must invoice whatever Bosta delivered, so permission
	# checks are skipped for both the insert and the submit.
	invoice.flags.ignore_permissions = True
	invoice.insert(ignore_permissions=True)
	invoice.submit()
	frappe.db.commit()


def _already_invoiced(delivery_note):
	"""True when a non-cancelled Sales Invoice line already bills this note (never bill twice)."""
	return bool(
		frappe.get_all(
			"Sales Invoice Item",
			filters={"delivery_note": delivery_note, "docstatus": ["<", 2]},
			limit=1,
			pluck="name",
		)
	)


def _all_billed(sales_orders):
	"""True when every Sales Order of the note is fully billed already."""
	for sales_order in sales_orders:
		if (frappe.db.get_value("Sales Order", sales_order, "per_billed") or 0) < 100:
			return False
	return True


def _invoice_date(note, today):
	"""Bosta delivery time (else last sync), clamped to between the note's posting date and today."""
	date = None
	for value in (note.custom_bosta_last_event_at, note.custom_last_sync_at):
		if not value:
			continue
		try:
			date = getdate(str(value)[:10])
			break
		except Exception:
			continue
	date = date or getdate(today)
	posting_date = getdate(note.posting_date)
	if date < posting_date:
		date = posting_date
	if date > getdate(today):
		date = getdate(today)
	return date


def _report_failure(delivery_note, error):
	"""Log the failure and open one High-priority ToDo per note, until a person closes it. Never raises."""
	try:
		if frappe.get_all(
			"ToDo",
			filters={
				"reference_type": "Delivery Note",
				"reference_name": delivery_note,
				"status": "Open",
				"description": ["like", f"{FAILURE_MARKER}%"],
			},
			limit=1,
			pluck="name",
		):
			return
		# The full text (HTML stripped, it can come from a platform) goes to the Error Log only.
		frappe.log_error(
			title=_("Auto invoice failed: {0}").format(delivery_note),
			message=f"Delivery Note {delivery_note}: {strip_html(str(error))}",
		)
		if frappe.db.exists("User", FAILURE_TODO_ASSIGNEE):
			frappe.get_doc(
				{
					"doctype": "ToDo",
					"allocated_to": FAILURE_TODO_ASSIGNEE,
					"reference_type": "Delivery Note",
					"reference_name": delivery_note,
					"priority": "High",
					"description": f"{FAILURE_MARKER} "
					+ _(
						"Auto-invoice (Bosta delivered) failed for Delivery Note {0}. "
						"See the Error Log. Please create the invoice manually."
					).format(delivery_note),
				}
			).insert(ignore_permissions=True)
		frappe.db.commit()
	except BaseTimeoutException:
		raise
	except Exception:
		frappe.db.rollback()
		frappe.log_error(title=f"Auto invoice: could not report the failure of {delivery_note}")
