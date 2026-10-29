# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""One draft Sales Invoice per Noon Sales Order, built from submitted Delivery Notes (#377).

``integrations/noon/fulfillment.py`` calls ``queue_sales_invoice()`` after it submits a
shipment's Delivery Note (a draft note never queues it).  The job, ``sync_sales_invoice()``:

- no invoice yet: makes a draft from a submitted Delivery Note with ERPNext's own
  ``make_sales_invoice`` mapper (``update_stock`` stays 0, so a credit note can be made against
  it later) and stores it in ``Marketplace Order.noon_sales_invoice``;
- invoice still a draft: appends the rows of every submitted Delivery Note that is not on it yet.
  The mapper caps each row at what the note still has unbilled, and a note that already has a row
  on a live Sales Invoice adds nothing, so no quantity is billed twice;
- invoice submitted or cancelled: adds nothing.  ``uninvoiced_notes()`` lists the submitted notes
  that are still not billed, and ``services/marketplace_attention.py`` shows them as Needs
  Attention until they are billed by hand.

The app never submits the invoice and never sends anything to Noon.  A failure is stored escaped
in ``noon_sales_invoice_error`` (shown in Needs Attention) and retried by the next Delivery Note
submit or the next order import.  Both Marketplace Order and Sales Order rows are locked
(in that order, as the sync does) before anything is read or created, so two jobs cannot create
two invoices.
"""

import frappe
from frappe import _
from frappe.utils import cstr, escape_html, strip_html

RQ_JOB_PREFIX = "marketplace-noon-sales-invoice"
ORDER_DOCTYPE = "Marketplace Order"


def queue_sales_invoice(marketplace_order, sales_order):
	"""Queue ``sync_sales_invoice`` for one order: after commit, deduplicated per Sales Order."""
	return frappe.enqueue(
		sync_sales_invoice,
		queue="long",
		timeout=300,
		enqueue_after_commit=True,
		job_id=f"{RQ_JOB_PREFIX}-{sales_order}",
		deduplicate=True,
		marketplace_order=marketplace_order,
	)


def submitted_notes(sales_order):
	"""Names of the submitted, non-return Delivery Notes against ``sales_order``, oldest first.

	``get_all`` on purpose: the job runs as the system and must see every note.
	"""
	rows = frappe.get_all(
		"Delivery Note Item",
		filters={"against_sales_order": sales_order},
		fields=["parent"],
		parent_doctype="Delivery Note",
		limit_page_length=0,
	)
	names = sorted({row.parent for row in rows})
	if not names:
		return []
	return frappe.get_all(
		"Delivery Note",
		filters={"name": ["in", names], "docstatus": 1, "is_return": 0},
		order_by="creation asc, name asc",
		pluck="name",
	)


def _notes_on_live_invoices(notes):
	"""Subset of ``notes`` that has a row on a draft or submitted Sales Invoice."""
	if not notes:
		return set()
	rows = frappe.get_all(
		"Sales Invoice Item",
		filters={"delivery_note": ["in", list(notes)], "docstatus": ["<", 2]},
		fields=["delivery_note"],
		parent_doctype="Sales Invoice",
		limit_page_length=0,
	)
	return {row.delivery_note for row in rows}


def uninvoiced_notes(sales_order):
	"""Submitted Delivery Notes of ``sales_order`` that are not billed and not on a live invoice."""
	notes = submitted_notes(sales_order)
	if not notes:
		return []
	on_invoice = _notes_on_live_invoices(notes)
	billed = set(
		frappe.get_all("Delivery Note", filters={"name": ["in", notes], "per_billed": [">=", 100]}, pluck="name")
	)
	return [name for name in notes if name not in on_invoice and name not in billed]


def sync_sales_invoice(marketplace_order):
	"""Background job: create or extend the draft Sales Invoice of one Noon order (see the module
	docstring).

	Runs as the system: it is queued by the fulfillment worker or an import, detached from the
	session, and must create the invoice whatever the user whose action submitted the Delivery
	Note may do (the app never submits the invoice).
	"""
	if not frappe.db.exists(ORDER_DOCTYPE, marketplace_order):
		return
	frappe.set_user("Administrator")
	# Lock order: Marketplace Order, then Sales Order (the same as the Sales Order sync).
	frappe.db.commit()
	frappe.db.get_value(ORDER_DOCTYPE, marketplace_order, "name", for_update=True)
	order = frappe.db.get_value(
		ORDER_DOCTYPE,
		marketplace_order,
		["erpnext_sales_order", "noon_sales_invoice", "noon_sales_invoice_error"],
		as_dict=True,
	)
	sales_order = order.erpnext_sales_order
	if not sales_order:
		frappe.db.commit()
		return
	frappe.db.get_value("Sales Order", sales_order, "name", for_update=True)

	savepoint = "noon_sales_invoice"
	frappe.db.savepoint(savepoint)
	error = None
	invoice = order.noon_sales_invoice
	try:
		invoice = _create_or_extend(sales_order, invoice)
		frappe.db.release_savepoint(savepoint)
	except Exception as exc:
		frappe.db.rollback(save_point=savepoint)
		frappe.log_error(
			title="Noon Sales Invoice failed", message=f"{marketplace_order}\n{frappe.get_traceback()}"
		)
		error = escape_html(strip_html(cstr(exc)).strip() or _("The Sales Invoice could not be created."))[:500]
	values = {}
	if (invoice or None) != (order.noon_sales_invoice or None):
		values["noon_sales_invoice"] = invoice
	if (error or None) != (order.noon_sales_invoice_error or None):
		values["noon_sales_invoice_error"] = error
	if values:
		# The row is locked above, so a plain write is safe; ``modified`` is bumped so a stale
		# copy of the order cannot be saved over it unnoticed.
		frappe.db.set_value(ORDER_DOCTYPE, marketplace_order, values)
	frappe.db.commit()


def _create_or_extend(sales_order, invoice_name):
	"""Return the name of the order's draft invoice after adding every uninvoiced note to it."""
	status = frappe.db.get_value("Sales Invoice", invoice_name, "docstatus") if invoice_name else None
	if status in (1, 2):
		return invoice_name  # a person owns it now; the leftover notes show in Needs Attention
	notes = uninvoiced_notes(sales_order)
	if not notes:
		return invoice_name if status == 0 else None
	if status is None:
		from erpnext.stock.doctype.delivery_note.delivery_note import make_sales_invoice

		invoice = make_sales_invoice(notes[0])
		invoice.update_stock = 0
		invoice.insert()
		notes = notes[1:]
	else:
		invoice = frappe.get_doc("Sales Invoice", invoice_name)
	if notes:
		_append_notes(invoice, notes)
		invoice.save()
	return invoice.name


def _append_notes(invoice, notes):
	"""Append the unbilled rows of each of ``notes`` to the draft ``invoice`` (in memory)."""
	from erpnext.stock.doctype.delivery_note.delivery_note import make_sales_invoice

	for note in notes:
		if any(row.delivery_note == note for row in invoice.items):
			continue
		for row in make_sales_invoice(note).items:
			invoice.append("items", row.as_dict(no_default_fields=True))
