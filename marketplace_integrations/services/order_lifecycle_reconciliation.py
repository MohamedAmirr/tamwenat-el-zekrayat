# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Nightly read-only reconciliation of the Shopify order lifecycle (epic #502, issue #501).

It compares what ERPNext holds with itself and with the last Shopify import, for every synced, open Shopify Sales
Order, and writes ONE Needs Attention row per mismatch. It changes no business document and calls no API (the
Bosta status and the Shopify order were stored by their own jobs; this job runs after them).

Mismatches (``KINDS``):

- ``stage``: a Delivery Note's stage (``workflow_state``) is not the one its statuses lead to
  (``delivery_note_workflow.state_of_row``, the one mapping). A stage that sits ABOVE the mapped one is not a
  mismatch where the live write holds it there on purpose (``statuses.STAGE_RANK``: an old or repeated event never
  pulls a note back; only the exception codes and a Delivered after Lost / Damaged may), so the check uses the
  same rank rule.
- ``quantity``: a Sales Order row's quantity is not Shopify's current quantity of its line.
- ``total``: the Sales Order total is not Shopify's current total (items, shipping lines, taxes and discounts are all
  inside it; the row text lists them).
- ``note_rows``: a draft Delivery Note ships MORE of an order row than is left to deliver, or a row of the order that
  no longer exists. A partial note, or a note shared with other orders, is legitimate and never flagged (only the
  rows of the order itself are judged).

The Bosta COD check is not here: "Bosta COD Differs" is computed live from the note and its request snapshot (a
Needs Attention row with Retry, ``marketplace_attention``), so it is never a day late. Neither are "Picked up but
not submitted" and the Lost / Damaged parcel: their live Needs Attention rows already show them. Orders in the stage
Waiting for Stock are left out of the quantity and total checks: Shopify's higher quantity is held back on purpose.

Bound of the check: an order is checked only while it can still change by an edit, so a Completed or fully
delivered Sales Order and an order Shopify shows refunded or partly refunded (a refund adjusts Shopify's lines and
total after the fact; ERPNext follows through the refund planner, not the edit flow) are left out.

Rows are Marketplace Sync Log rows (sync type Lifecycle Check, status Error) keyed by ``kind|Sales Order|Delivery
Note``: a mismatch that is still there keeps its row untouched, a new one gets a row, one that is gone is closed
(status Success). Running it twice creates nothing twice.
"""

import json

import frappe
from frappe import _
from frappe.utils import cstr, flt, now_datetime

from marketplace_integrations.services import delivery_note_workflow as lifecycle
from marketplace_integrations.services import order_reservation

SYNC_TYPE = "Lifecycle Check"
TRIGGER_SOURCE = "Scheduled Reconciliation"
JOB_ID = "marketplace-order-lifecycle-reconciliation"

STAGE = "stage"
QUANTITY = "quantity"
TOTAL = "total"
NOTE_ROWS = "note_rows"
KINDS = (STAGE, QUANTITY, TOTAL, NOTE_ROWS)

#: Shopify's total against the Sales Order's grand total: a difference under half a currency unit is rounding (the
#: Sales Order's ROUNDED total, the amount that is billed, differs from its grand total by up to half a unit), not a
#: mismatch.
TOTAL_TOLERANCE = 0.5
CHUNK_SIZE = 200
_CLOSED_ORDER_STATUSES = ("Closed", "Cancelled", "On Hold", "Completed")
#: Shopify financial statuses after a refund: left out of the check (see the module docstring).
_REFUNDED_FINANCIAL_STATUSES = ("REFUNDED", "PARTIALLY_REFUNDED")


def enqueue_nightly():
	"""Cron entry (``hooks.py``): one deduplicated job on the long queue."""
	frappe.set_user("Administrator")
	frappe.enqueue(
		"marketplace_integrations.services.order_lifecycle_reconciliation.run",
		queue="long",
		timeout=3600,
		job_id=JOB_ID,
		deduplicate=True,
	)


def run():
	"""Find the mismatches and write the rows. Returns ``{"found", "opened", "closed"}``."""
	frappe.set_user("Administrator")
	findings = find_mismatches()
	opened, closed = write_rows(findings)
	frappe.db.commit()
	return {"found": len(findings), "opened": opened, "closed": closed}


def describe(kind, args):
	"""The text of a mismatch (translated for the reader of the Needs Attention report)."""
	args = list(args or [])
	templates = {
		STAGE: _("Delivery Note {0} is in the stage {1}, but its statuses lead to {2}."),
		QUANTITY: _("Sales Order {0} and Shopify have different item quantities: {1}"),
		TOTAL: _(
			"Sales Order {0} has the total {1} but Shopify has {2} (items {3}, shipping {4}, taxes {5}, discounts {6})."
		),
		NOTE_ROWS: _("Draft Delivery Note {0} does not match Sales Order {1}: {2}"),
	}
	return templates[kind].format(*args)


# ---------------------------------------------------------------------------
# Finding mismatches (read only)
# ---------------------------------------------------------------------------


def _chunks(values):
	values = list(values)
	for start in range(0, len(values), CHUNK_SIZE):
		yield values[start : start + CHUNK_SIZE]


def _add(findings, kind, order, sales_order, delivery_note=None, args=()):
	key = f"{kind}|{sales_order}|{delivery_note or ''}"
	findings[key] = {
		"key": key,
		"kind": kind,
		"marketplace_order": order.name,
		"marketplace_account": order.marketplace_account,
		"platform": order.platform,
		"sales_order": sales_order,
		"delivery_note": delivery_note,
		"args": [cstr(arg) for arg in args],
	}


def find_mismatches():
	"""``{key: finding}`` for every synced, open, not-cancelled, not-refunded and not fully delivered Shopify Sales
	Order (the bound in the module docstring). Reads only."""
	findings = {}
	orders = frappe.get_all(
		"Marketplace Order",
		filters={
			"platform": "Shopify",
			"erpnext_sales_order": ["is", "set"],
			"erpnext_sync_status": "Synced",
			"cancelled_at": ["is", "not set"],
			"external_financial_status": ["not in", list(_REFUNDED_FINANCIAL_STATUSES)],
		},
		fields=[
			"name",
			"marketplace_account",
			"platform",
			"erpnext_sales_order",
			"order_total",
			"sub_total",
			"shipping_total",
			"total_taxes",
			"discount",
			"last_synced_snapshot",
		],
		limit_page_length=0,
	)
	for chunk in _chunks(orders):
		_check_chunk(chunk, findings)
	return findings


def _check_chunk(orders, findings):
	names = [order.erpnext_sales_order for order in orders]
	stage_field = [order_reservation.STAGE_FIELD] if order_reservation.stage_installed() else []
	sales_orders = {
		row.name: row
		for row in frappe.get_all(
			"Sales Order",
			filters={
				"name": ["in", names],
				"docstatus": 1,
				"status": ["not in", list(_CLOSED_ORDER_STATUSES)],
				"per_delivered": ["<", 100],
			},
			fields=["name", "grand_total", *stage_field],
			limit_page_length=0,
		)
	}
	if not sales_orders:
		return
	# Shopify's current quantities: one query for the chunk, not one per order
	current = {}
	for line in frappe.get_all(
		"Marketplace Order Item",
		filters={"parent": ["in", [order.name for order in orders]], "parenttype": "Marketplace Order"},
		fields=["parent", "external_item_id", "current_quantity"],
		limit_page_length=0,
	):
		current.setdefault(line.parent, {})[line.external_item_id] = flt(line.current_quantity)
	rows = {}
	for row in frappe.get_all(
		"Sales Order Item",
		filters={"parent": ["in", list(sales_orders)], "parenttype": "Sales Order"},
		fields=["name", "parent", "item_code", "qty", "delivered_qty", "delivered_by_supplier"],
		limit_page_length=0,
	):
		rows.setdefault(row.parent, {})[row.name] = row
	notes = _notes_by_order(list(sales_orders))
	for order in orders:
		sales_order = sales_orders.get(order.erpnext_sales_order)
		if not sales_order:
			continue
		waiting = bool(stage_field) and (
			sales_order.get(order_reservation.STAGE_FIELD) == order_reservation.WAITING_FOR_STOCK
		)
		order_rows = rows.get(sales_order.name, {})
		if not waiting:
			_check_total(findings, order, sales_order)
			_check_quantities(findings, order, sales_order, order_rows, current.get(order.name, {}))
		for note in notes.get(sales_order.name, []):
			_check_note(findings, order, sales_order, note, order_rows)


def _notes_by_order(sales_orders):
	"""``{sales order: [note with its rows]}``: the non-cancelled, non-return Delivery Notes of the orders."""
	parent_rows = {}
	for row in frappe.get_all(
		"Delivery Note Item",
		filters={"against_sales_order": ["in", sales_orders], "docstatus": ["<", 2], "parenttype": "Delivery Note"},
		fields=["parent", "against_sales_order", "so_detail", "qty"],
		limit_page_length=0,
	):
		parent_rows.setdefault(row.parent, []).append(row)
	if not parent_rows:
		return {}
	fields = ["name", "docstatus", "is_return", "transporter_name", "custom_integration_status", "custom_shipping_status"]
	if lifecycle.is_installed():
		fields.append(lifecycle.FIELD)
	by_order = {}
	for note in frappe.get_all(
		"Delivery Note",
		filters={"name": ["in", list(parent_rows)], "docstatus": ["<", 2], "is_return": 0},
		fields=fields,
		limit_page_length=0,
	):
		note.rows = parent_rows[note.name]
		for sales_order in {row.against_sales_order for row in note.rows}:
			by_order.setdefault(sales_order, []).append(note)
	return by_order


def _check_total(findings, order, sales_order):
	if abs(flt(sales_order.grand_total) - flt(order.order_total)) <= TOTAL_TOLERANCE:
		return
	_add(
		findings,
		TOTAL,
		order,
		sales_order.name,
		args=(
			sales_order.name,
			f"{flt(sales_order.grand_total):.2f}",
			f"{flt(order.order_total):.2f}",
			f"{flt(order.sub_total):.2f}",
			f"{flt(order.shipping_total):.2f}",
			f"{flt(order.total_taxes):.2f}",
			f"{flt(order.discount):.2f}",
		),
	)


def _snapshot_lines(order, current):
	"""``[(Sales Order row name, Shopify's current quantity, item code)]`` for the lines of the synced baseline that
	name their Sales Order row. ``current``: ``{external item id: Shopify's current quantity}`` from the Marketplace
	Order's own lines (loaded once per chunk)."""
	try:
		snapshot = frappe.parse_json(order.last_synced_snapshot)
	except (TypeError, ValueError):
		return []
	if not isinstance(snapshot, dict):
		return []
	return [
		(line["sales_order_item"], current.get(line.get("external_item_id"), 0), line.get("item_code"))
		for line in snapshot.get("items") or []
		if line.get("sales_order_item")
	]


def _check_quantities(findings, order, sales_order, rows, current):
	differences = []
	for row_name, shopify_qty, item_code in _snapshot_lines(order, current):
		row = rows.get(row_name)
		erp_qty = flt(row.qty) if row else 0
		if erp_qty != shopify_qty:
			differences.append(f"{item_code or row_name}: {erp_qty:g} / {shopify_qty:g}")
	if differences:
		_add(findings, QUANTITY, order, sales_order.name, args=(sales_order.name, "; ".join(differences[:5])))


def _check_note(findings, order, sales_order, note, rows):
	if lifecycle.is_installed() and note.get(lifecycle.FIELD):
		expected = lifecycle.state_of_row(note)
		stored = note.get(lifecycle.FIELD)
		if expected != stored and not _held_by_rank(stored, expected, cstr(note.custom_shipping_status)):
			_add(findings, STAGE, order, sales_order.name, note.name, (note.name, stored, expected))
	if note.docstatus == 0:
		problem = _note_rows_problem(note, sales_order.name, rows)
		if problem:
			_add(findings, NOTE_ROWS, order, sales_order.name, note.name, (note.name, sales_order.name, problem))


def _held_by_rank(stored, expected, shipping_status):
	"""The same rule as the live write (``statuses._forward_only``): a stored stage above the mapped one stays,
	unless the shipping status is one that may move a stage back (``statuses.may_move_back``; the previous status
	is unknown here, so a Delivered stage left at Returned / Failed counts as a mismatch)."""
	from marketplace_integrations.integrations.bosta import statuses

	if stored not in statuses.STAGE_RANK or expected not in statuses.STAGE_RANK:
		return False
	code = next((c for c, name in statuses.BOSTA_STATE_NAMES.items() if name == shipping_status), None)
	if code in statuses.EXCEPTION_CODES or code == 45:
		return False
	return statuses.STAGE_RANK[expected] < statuses.STAGE_RANK[stored]


def _note_rows_problem(note, sales_order, rows):
	"""What is wrong with the draft note's rows OF THIS ORDER, else ``""``: a row that ships more than the order has
	left to deliver, or one that points at a Sales Order row that is gone (or is delivered by the supplier). A note
	that ships LESS than the order, or only some of its rows, is a legitimate partial note (and a note shared with
	other orders is judged on this order's rows only), so it is not a mismatch."""
	shipped = {}
	for row in note.rows:
		if row.against_sales_order == sales_order and row.so_detail:
			shipped[row.so_detail] = shipped.get(row.so_detail, 0) + flt(row.qty)
	problems = []
	for name, qty in shipped.items():
		row = rows.get(name)
		if row is None or row.delivered_by_supplier:
			problems.append(f"{name}: {flt(qty):g} / 0")
			continue
		pending = flt(row.qty) - flt(row.delivered_qty)
		if flt(qty, 6) > flt(pending, 6):
			problems.append(f"{row.item_code}: {flt(qty):g} / {flt(pending):g}")
	return "; ".join(problems[:5])


# ---------------------------------------------------------------------------
# Writing the rows (the only writes)
# ---------------------------------------------------------------------------


def _key_of(request_data):
	try:
		data = json.loads(request_data or "{}")
	except ValueError:
		return None
	return data.get("key") if isinstance(data, dict) else None


def write_rows(findings):
	"""Open a row for each new finding, close the rows of mismatches that are gone, leave the others. Returns
	``(opened, closed)``."""
	now = now_datetime()
	open_rows = frappe.get_all(
		"Marketplace Sync Log",
		filters={"sync_type": SYNC_TYPE, "status": "Error"},
		fields=["name", "request_data"],
		order_by="creation asc",
		limit_page_length=0,
	)
	existing = {}
	for row in open_rows:
		key = _key_of(row.request_data)
		if key and key not in existing:
			existing[key] = row.name
	opened = 0
	for key, finding in findings.items():
		if key in existing:
			continue
		frappe.get_doc(
			{
				"doctype": "Marketplace Sync Log",
				"sync_type": SYNC_TYPE,
				"marketplace_account": finding["marketplace_account"],
				"platform": finding["platform"],
				"marketplace_order": finding["marketplace_order"],
				"status": "Error",
				"trigger_source": TRIGGER_SOURCE,
				"attempted_at": now,
				"request_data": frappe.as_json(finding, ensure_ascii=False),
			}
		).insert(ignore_permissions=True)
		opened += 1
	closed = 0
	for row in open_rows:
		key = _key_of(row.request_data)
		# A second open row of a key still found (a double run) is closed too: one row per mismatch.
		if key is None or key not in findings or existing.get(key) != row.name:
			frappe.db.set_value(
				"Marketplace Sync Log", row.name, {"status": "Success", "completed_at": now}, update_modified=False
			)
			closed += 1
	return opened, closed
