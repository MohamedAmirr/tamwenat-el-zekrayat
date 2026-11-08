# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Stock Reservation of a Shopify Sales Order, Waiting for Stock and the automatic draft Delivery Note
(epic #502, issue #499).

``reserve_or_wait(sales_order)``: the whole order is reserved in the warehouse of its rows (the Marketplace
Account's warehouse) or nothing is. When every stock row is fully reserved, a draft Delivery Note is made
(transporter Bosta, workflow stage Order Received) and the stage of the Sales Order is Confirmed. When any row is
short, the reservation is rolled back, the stage is Waiting for Stock and ONE open ToDo for the Stock Manager role
lists item, quantity needed and quantity available. ``sweep_changed_bins`` retries those orders through the same
function when a Bin they need changed. Shopify only: Noon, Talabat and InstaShop never reach this module.

ERPNext behaviour relied on (ERPNext 16.51.1 dev; docs/frappe-best-practices.md, Version compatibility):

- ``SalesOrder._create_stock_reservation_entries`` reserves PER ROW and silently skips a short row (a message
  only). The whole-order rule is therefore checked here, with ``get_unreserved_qty`` per row after reserving; any
  shortfall rolls back to a savepoint.
- The availability query takes no lock when no reservation row exists yet, so the Bin rows of the order are
  locked ``FOR UPDATE`` in sorted order first (two orders for the last unit: exactly one wins, no deadlock).
- A Product Bundle parent is a non-stock item and is never reserved. An order with a Product Bundle row is left
  alone here (no reservation, no automatic note): how bundles are reserved is an open decision of the epic.
- ``Stock Reservation Entry`` changes ``Bin.reserved_stock``, not ``Bin.reserved_qty``, so the Shopify
  available quantity (``listing_quantity``, which subtracts ``reserved_qty``) is unaffected.
- ``Stock Settings.enable_stock_reservation`` must be on. This module never flips that global setting: when it
  is off it does nothing and writes one Error Log entry per day.
"""

import frappe
from frappe import _
from frappe.query_builder.functions import Sum
from frappe.utils import add_to_date, cint, flt, now_datetime

from marketplace_integrations.integrations.shopify.constants import SHOPIFY_INVENTORY_SWEEP_OVERLAP_MINUTES

STAGE_FIELD = "custom_order_stage"
WAITING_FOR_STOCK = "Waiting for Stock"
CONFIRMED = "Confirmed"
LOCKED = "Locked"
CLOSED = "Closed"

STOCK_MANAGER_ROLE = "Stock Manager"
OPEN_STATUSES = ("To Deliver", "To Deliver and Bill")

#: Orders one sweep run retries (oldest first). The rest wait for the next tick.
SWEEP_BATCH_LIMIT = 50
SWEEP_JOB_ID = "marketplace_integrations:order_reservation_sweep"
_WATERMARK_KEY = "marketplace_integrations:order_reservation_sweep_watermark"
_DISABLED_LOG_KEY = "marketplace_integrations:order_reservation_disabled_logged_date"
_SAVEPOINT = "marketplace_order_reservation"
_NOTE_SAVEPOINT = "marketplace_order_reservation_note"

# Results (returned for tests and logs)
SKIPPED = "skipped"
DISABLED = "disabled"
WAITING = "waiting"
RESERVED = "reserved"
NOTE_CREATED = "note_created"


def reservation_enabled():
	return bool(cint(frappe.get_single_value("Stock Settings", "enable_stock_reservation")))


def stage_installed():
	return frappe.get_meta("Sales Order").has_field(STAGE_FIELD)


def set_stage(sales_order, stage):
	"""Write the derived stage (no hooks, ``modified`` kept). Only this module and the later lifecycle issues write it."""
	if stage_installed() and (frappe.db.get_value("Sales Order", sales_order, STAGE_FIELD) or "") != (stage or ""):
		frappe.db.set_value("Sales Order", sales_order, STAGE_FIELD, stage or "", update_modified=False)


# ---------------------------------------------------------------------------
# Reserve or wait
# ---------------------------------------------------------------------------


def reserve_or_wait(sales_order_name):
	"""Reserve the whole order or put it in Waiting for Stock; create the draft Delivery Note when reserved.

	Idempotent: a second run reserves nothing twice, makes no second note and no second ToDo. Does not commit
	(the caller does); the Bin and Sales Order row locks live until then. Returns one of the result constants."""
	if not reservation_enabled():
		_log_disabled_once()
		return DISABLED
	# Lock the order first, then its Bins: every caller takes the locks in this order.
	frappe.db.get_value("Sales Order", sales_order_name, "name", for_update=True)
	order = frappe.get_doc("Sales Order", sales_order_name)
	if not _eligible(order):
		return SKIPPED
	rows = _stock_rows(order)
	if not rows:
		return SKIPPED

	_lock_bins({(row.item_code, row.warehouse) for row in rows})
	frappe.db.savepoint(_SAVEPOINT)
	try:
		_reserve_rows(order, rows)
		short = _shortfalls(order, rows)
	except Exception:
		frappe.db.rollback(save_point=_SAVEPOINT)
		raise
	if short:
		frappe.db.rollback(save_point=_SAVEPOINT)
		# the rollback also undid reserve_stock on the in-memory rows' db values; nothing else to restore
		set_stage(order.name, WAITING_FOR_STOCK)
		_open_todo(order.name, _available_for(short))
		return WAITING

	set_stage(order.name, CONFIRMED)
	_close_todo(order.name)
	return _make_note_if_none(order)


def _eligible(order):
	"""A submitted, open Shopify Sales Order. Never raises."""
	return (
		order.docstatus == 1
		and order.get("custom_marketplace_platform") == "Shopify"
		and order.status in OPEN_STATUSES
	)


def _stock_rows(order):
	"""The rows to reserve (stock items, not delivered by a supplier), or [] when the order has a Product Bundle row
	(left alone: open decision)."""
	rows = []
	for row in order.items:
		if order.has_product_bundle(row.item_code):
			return []
		if row.delivered_by_supplier or not row.warehouse:
			continue
		if frappe.get_cached_value("Item", row.item_code, "is_stock_item"):
			rows.append(row)
	return rows


def _lock_bins(pairs):
	"""Lock the Bin rows of (item, warehouse) pairs FOR UPDATE in sorted order."""
	bin_ = frappe.qb.DocType("Bin")
	for item_code, warehouse in sorted(pairs):
		(
			frappe.qb.from_(bin_)
			.select(bin_.name)
			.where((bin_.item_code == item_code) & (bin_.warehouse == warehouse))
			.for_update()
			.run()
		)


def _reserve_rows(order, rows):
	"""ERPNext's own per-row reservation (internal entry point, no permission check)."""
	for row in rows:
		if not cint(row.reserve_stock):
			row.reserve_stock = 1
			frappe.db.set_value("Sales Order Item", row.name, "reserve_stock", 1, update_modified=False)
	order._create_stock_reservation_entries(notify=False)
	frappe.clear_messages()


def _shortfalls(order, rows):
	"""``[(row, unreserved_stock_qty)]`` of the rows ERPNext did not fully reserve."""
	from erpnext.selling.doctype.sales_order.sales_order import get_unreserved_qty
	from erpnext.stock.doctype.stock_reservation_entry.stock_reservation_entry import (
		get_sre_reserved_qty_details_for_voucher,
	)

	details = get_sre_reserved_qty_details_for_voucher("Sales Order", order.name)
	short = []
	for row in rows:
		unreserved = flt(get_unreserved_qty(row, details), 6)
		if unreserved > 0.0001:
			short.append((row, unreserved))
	return short


def _available_for(short):
	"""``[(item_code, needed, available)]`` after the rollback: the stock free to reserve, other reservations
	already subtracted. Needed is the row's whole stock quantity."""
	from erpnext.stock.doctype.stock_reservation_entry.stock_reservation_entry import get_available_qty_to_reserve

	return [
		(row.item_code, flt(row.stock_qty), max(flt(get_available_qty_to_reserve(row.item_code, row.warehouse)), 0))
		for row, _unreserved in short
	]


# ---------------------------------------------------------------------------
# ToDo for the stock manager
# ---------------------------------------------------------------------------


def _todo_description(sales_order, lines):
	parts = [
		_("Item {0}: needed {1}, available {2}").format(item, _fmt(needed), _fmt(available))
		for item, needed, available in lines
	]
	return _("Waiting for Stock: Sales Order {0} cannot be reserved in full.").format(sales_order) + " " + "; ".join(parts)


def _fmt(value):
	return ("%.3f" % flt(value)).rstrip("0").rstrip(".") or "0"


def _open_todo_name(sales_order):
	return frappe.db.get_value(
		"ToDo",
		{"reference_type": "Sales Order", "reference_name": sales_order, "status": "Open", "role": STOCK_MANAGER_ROLE},
		"name",
	)


def _open_todo(sales_order, lines):
	"""One open ToDo per order (role Stock Manager); a repeat run only refreshes its description."""
	description = _todo_description(sales_order, lines)
	name = _open_todo_name(sales_order)
	if name:
		if frappe.db.get_value("ToDo", name, "description") != description:
			frappe.db.set_value("ToDo", name, "description", description)
		return name
	todo = frappe.get_doc(
		{
			"doctype": "ToDo",
			"reference_type": "Sales Order",
			"reference_name": sales_order,
			"role": STOCK_MANAGER_ROLE,
			"description": description,
			"priority": "Medium",
			"status": "Open",
		}
	)
	todo.flags.ignore_permissions = True
	todo.insert()
	return todo.name


def _close_todo(sales_order):
	name = _open_todo_name(sales_order)
	if name:
		frappe.db.set_value("ToDo", name, "status", "Closed")


# ---------------------------------------------------------------------------
# Draft Delivery Note
# ---------------------------------------------------------------------------


def _make_note_if_none(order):
	"""The draft note of a fully reserved order, through the bulk path's own payment plan, invoice and note maker
	(never a second note). A refusal keeps the reservation, leaves no note and goes to the Error Log: the person
	creates the note by hand."""
	from marketplace_integrations.services import bulk_delivery_notes as bulk

	if bulk.live_note(order.name):
		return RESERVED
	if not bulk.bosta_shipment.bosta_supplier():
		frappe.log_error(
			title="Automatic Delivery Note: no Bosta Supplier", message=f"Sales Order {order.name}"
		)
		return RESERVED
	frappe.db.savepoint(_NOTE_SAVEPOINT)
	try:
		plan = bulk.payment_plan(order)
		if plan["needs_invoice"]:
			bulk._make_paid_invoice(order)
		note = bulk._make_note(order, plan["cod"])
		frappe.clear_messages()
	except Exception as exc:
		frappe.db.rollback(save_point=_NOTE_SAVEPOINT)
		frappe.clear_messages()
		if not isinstance(exc, bulk.Refused):
			frappe.log_error(title="Automatic Delivery Note failed", message=f"{order.name}\n{frappe.get_traceback()}")
		else:
			frappe.log_error(title="Automatic Delivery Note refused", message=f"{order.name}\n{exc}")
		return RESERVED
	return NOTE_CREATED if note else RESERVED


# ---------------------------------------------------------------------------
# Disabled setting notice
# ---------------------------------------------------------------------------


def _log_disabled_once():
	today = now_datetime().date().isoformat()
	if frappe.cache().get_value(_DISABLED_LOG_KEY) == today:
		return
	frappe.cache().set_value(_DISABLED_LOG_KEY, today)
	frappe.log_error(
		title="Stock reservation is off",
		message=(
			"Stock Settings 'Enable Stock Reservation' is off, so Shopify orders are neither reserved nor given an "
			"automatic Delivery Note. Turn it on (Auto Reserve Stock stays off)."
		),
	)


# ---------------------------------------------------------------------------
# Retry sweep
# ---------------------------------------------------------------------------


def sweep_changed_bins():
	"""Scheduler tick: enqueue ONE deduplicated job that retries Waiting for Stock orders whose Bins changed."""
	if not reservation_enabled() or not stage_installed():
		return
	if not frappe.db.exists("Sales Order", {STAGE_FIELD: WAITING_FOR_STOCK, "docstatus": 1}):
		return
	frappe.enqueue(retry_waiting_orders, queue="short", job_id=SWEEP_JOB_ID, deduplicate=True)


def retry_waiting_orders():
	"""Retry (bounded batch, oldest first) every Waiting for Stock order that has a Bin changed since the
	watermark minus the overlap. Each order commits on its own; one failure never stops the others."""
	tick = now_datetime()
	watermark = frappe.cache().get_value(_WATERMARK_KEY) or tick
	since = add_to_date(watermark, minutes=-SHOPIFY_INVENTORY_SWEEP_OVERLAP_MINUTES)
	orders = frappe.get_all(
		"Sales Order",
		filters={STAGE_FIELD: WAITING_FOR_STOCK, "docstatus": 1},
		pluck="name",
		order_by="creation asc",
		limit_page_length=SWEEP_BATCH_LIMIT + 1,
	)
	more = len(orders) > SWEEP_BATCH_LIMIT
	for name in orders[:SWEEP_BATCH_LIMIT]:
		try:
			if _bins_changed_since(name, since):
				reserve_or_wait(name)
			frappe.db.commit()
		except Exception:
			frappe.db.rollback()
			frappe.log_error(title="Order reservation retry failed", message=f"{name}\n{frappe.get_traceback()}")
	if not more:
		frappe.cache().set_value(_WATERMARK_KEY, tick)


def _bins_changed_since(sales_order, since):
	pairs = {
		(row.item_code, row.warehouse)
		for row in frappe.get_all(
			"Sales Order Item", filters={"parent": sales_order}, fields=["item_code", "warehouse"]
		)
		if row.warehouse
	}
	for item_code, warehouse in pairs:
		if frappe.db.exists("Bin", {"item_code": item_code, "warehouse": warehouse, "modified": [">", since]}):
			return True
	return False


# ---------------------------------------------------------------------------
# Pre-flight support (carrier_shipments.check_deliverable)
# ---------------------------------------------------------------------------


def reserved_by_others(warehouse, item_codes, own_sales_orders=()):
	"""``{item_code: qty}`` reserved in ``warehouse`` by Stock Reservation Entries of OTHER vouchers (the given
	Sales Orders are the note's own and stay available to it). Zero for everything when reservation is off."""
	item_codes = list(dict.fromkeys(item_codes))
	if not item_codes or not reservation_enabled():
		return dict.fromkeys(item_codes, 0)
	sre = frappe.qb.DocType("Stock Reservation Entry")
	query = (
		frappe.qb.from_(sre)
		.select(sre.item_code, Sum(sre.reserved_qty - sre.delivered_qty - sre.transferred_qty - sre.consumed_qty))
		.where((sre.docstatus == 1) & (sre.warehouse == warehouse) & (sre.item_code.isin(item_codes)))
		.groupby(sre.item_code)
	)
	own = [name for name in own_sales_orders if name]
	if own:
		query = query.where(~((sre.voucher_type == "Sales Order") & (sre.voucher_no.isin(own))))
	found = {item_code: flt(qty) for item_code, qty in query.run()}
	return {item_code: max(found.get(item_code, 0), 0) for item_code in item_codes}
