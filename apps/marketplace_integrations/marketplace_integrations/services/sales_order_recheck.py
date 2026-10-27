# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Re-check a Marketplace Order after a person fixed its blocker in ERPNext (#364).

A Needs Action or Sync Error order should not need a Retry Sync click once the blocker is
resolved.  Two triggers feed the normal debounced sync (``bump_sync_debounce()``); neither
syncs, plans or calls a platform itself:

- ``bump_linked_orders()`` is the ``doc_events`` handler (``hooks.py``) for Delivery Note, Sales
  Invoice, Payment Entry (submit, cancel, trash) and Sales Order (update after submit, cancel).
  It finds the Marketplace Orders those documents belong to (through the Sales Order link, or as the
  order's recorded blocking or prepared document), sets Recheck Queued and bumps the debounce
  with the trigger source ``ERPNext Document Change``.
- ``bump_orders_for_mapping()`` is called by the Marketplace Product Mapping controller after a
  save (service code, not a ``doc_events`` entry): orders of that account in Sync Error with a
  mapping or price reason are bumped.

The handler runs inside the transaction of whoever submitted the document (a Stock User with no
Marketplace Order permission, for instance), so it reads with ``get_all`` and writes through the
debounce's ``frappe.db.set_value()``: both skip permission checks, which is the point.  It
never raises (a failure here must not block a Delivery Note submit) and makes no HTTP call.
The debounce bump itself is queued with ``enqueue_after_commit`` (``bump_queued_orders``), so it
never writes inside the submitter's transaction.  The order whose sync is running right now is
skipped, but only when that sync runs in the same process (``frappe.flags``): a sync running in
a worker is not seen here, and its own finish (the debounce window) absorbs the bump.

Everything else (the sweep as the fallback for missed events, retry, Close/Reopen) stays in
``services/sales_order_sync_debounce.py``.
"""

import frappe

from marketplace_integrations.services.sales_order_sync_debounce import bump_sync_debounce
from marketplace_integrations.services.sync_reason_codes import MAPPING_OR_PRICE_REASONS
from marketplace_integrations.services.sync_status import NEEDS_ACTION, RECHECK_QUEUED, SYNC_ERROR

ORDER_DOCTYPE = "Marketplace Order"
TRIGGER_SOURCE = "ERPNext Document Change"
RECHECK_STATUSES = (NEEDS_ACTION, SYNC_ERROR)
# Most orders one document event or mapping save re-queues; the hourly sweep covers the rest.
MAX_ORDERS_PER_EVENT = 200


def bump_linked_orders(doc, method=None):
	"""``doc_events`` handler: queue a re-check for the orders ``doc`` belongs to.  Never raises."""
	try:
		names = []
		for order_name in linked_marketplace_orders(doc):
			if order_name in (frappe.flags.get("marketplace_syncing_orders") or ()):
				continue
			names.append(order_name)
		_queue_bumps(names, recheck_queued=True)
	except Exception:
		_log_failure(doc)


def linked_marketplace_orders(doc):
	"""Names of Marketplace Orders in Needs Action or Sync Error that ``doc`` belongs to."""
	sales_orders = _sales_orders_of(doc)
	# A list of filter triplets: a dict in ``or_filters`` only uses its first key.
	or_filters = [
		["sync_blocking_document", "=", doc.name],
		["sync_prepared_document", "=", doc.name],
	]
	if sales_orders:
		or_filters.append(["erpnext_sales_order", "in", sales_orders])
	rows = frappe.get_all(
		ORDER_DOCTYPE,
		filters={"erpnext_sync_status": ["in", list(RECHECK_STATUSES)]},
		or_filters=or_filters,
		fields=[
			"name",
			"erpnext_sales_order",
			"sync_blocking_doctype",
			"sync_blocking_document",
			"sync_prepared_doctype",
			"sync_prepared_document",
		],
		limit=MAX_ORDERS_PER_EVENT,
	)
	return [
		row.name
		for row in rows
		if row.erpnext_sales_order in sales_orders
		or (row.sync_blocking_doctype, row.sync_blocking_document) == (doc.doctype, doc.name)
		or (row.sync_prepared_doctype, row.sync_prepared_document) == (doc.doctype, doc.name)
	]


def _sales_orders_of(doc):
	if doc.doctype == "Sales Order":
		return [doc.name]
	if doc.doctype == "Delivery Note":
		names = [row.against_sales_order for row in doc.get("items") or []]
	elif doc.doctype == "Sales Invoice":
		names = [row.sales_order for row in doc.get("items") or []]
	elif doc.doctype == "Payment Entry":
		names = [
			row.reference_name
			for row in doc.get("references") or []
			if row.reference_doctype == "Sales Order"
		]
	else:
		names = []
	return sorted({name for name in names if name})


def bump_orders_for_mapping(mapping, method=None):
	"""After a Marketplace Product Mapping save: re-sync the account's orders stuck on a mapping or
	price problem.  Never raises; a disabled mapping fixes nothing, so it bumps nothing."""
	try:
		if not mapping.get("enabled"):
			return
		names = frappe.get_all(
			ORDER_DOCTYPE,
			filters={
				"marketplace_account": mapping.marketplace_account,
				"erpnext_sync_status": SYNC_ERROR,
				"sync_reason_code": ["in", list(MAPPING_OR_PRICE_REASONS)],
			},
			pluck="name",
			limit=MAX_ORDERS_PER_EVENT,
		)
		_queue_bumps(names, recheck_queued=False)
	except Exception:
		_log_failure(mapping)


def _queue_bumps(order_names, recheck_queued):
	"""Queue the debounce bump to run after the caller's transaction commits.

	The handler runs inside the submitter's transaction.  Writing the order row there would
	take a row lock that lives until the submitter commits, and a rollback of the submit would
	leave a re-check queued for a change that never happened.  ``enqueue_after_commit`` runs
	the job only if the transaction commits.
	"""
	if not order_names:
		return
	frappe.enqueue(
		"marketplace_integrations.services.sales_order_recheck.bump_queued_orders",
		queue="short",
		enqueue_after_commit=True,
		order_names=list(order_names),
		recheck_queued=recheck_queued,
	)


def bump_queued_orders(order_names, recheck_queued=False):
	"""Background job: bump the debounce of the queued orders that are still waiting.

	An order whose status changed between the event and this job (synced, or already
	Syncing) is left alone.  Makes no HTTP call.  The "sync running right now" check is not
	repeated here: it reads ``frappe.flags`` of the submitting process, which this job does not
	share, and a running sync's own debounce window absorbs the bump.
	"""
	extra = {"sync_action_state": RECHECK_QUEUED} if recheck_queued else None
	for order_name in frappe.get_all(
		ORDER_DOCTYPE,
		filters={"name": ["in", list(order_names)], "erpnext_sync_status": ["in", list(RECHECK_STATUSES)]},
		pluck="name",
	):
		bump_sync_debounce(order_name, TRIGGER_SOURCE, extra_values=extra)


def _log_failure(doc):
	try:
		frappe.log_error(title=f"Marketplace sync recheck failed for {doc.doctype} {doc.name}")
	except Exception:
		pass
