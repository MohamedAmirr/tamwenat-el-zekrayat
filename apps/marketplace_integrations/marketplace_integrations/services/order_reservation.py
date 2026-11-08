# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Stock Reservation of a Shopify Sales Order and Waiting for Stock (epic #502, issue #499).

``reserve_or_wait(sales_order)``: the whole order is reserved in the warehouse of its rows (the Marketplace
Account's warehouse) or nothing is. When every stock row is fully reserved the stage of the Sales Order is
Confirmed; no Delivery Note is made automatically (rolled back by decision: a person creates it). When any row is
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
  alone here (no reservation): how bundles are reserved is an open decision of the epic.
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

# Results (returned for tests and logs)
SKIPPED = "skipped"
DISABLED = "disabled"
WAITING = "waiting"
RESERVED = "reserved"


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


def reserve_or_wait(sales_order_name, refuse_partial=False, keep_reservation=False):
	"""Reserve the whole order or put it in Waiting for Stock.

	Idempotent: a second run reserves nothing twice and makes no second ToDo. Does not commit
	(the caller does); the Bin and Sales Order row locks live until then. Returns one of the result constants.

	``refuse_partial`` (a person's edit of a reserved order, #500): a shortfall raises a validation error
	instead of moving the order to Waiting for Stock, so the caller's transaction undoes the edit. Without it,
	a shortfall also cancels any partial reservation the edit left (ERPNext re-reserves per row after
	``update_child_qty_rate``), so the order is never partly reserved.

	``keep_reservation`` (a person's edit of an order that is already Waiting for Stock because Shopify raised a
	quantity, #500): a shortfall leaves the Stock Reservation Entries as they are (nothing is released) and only
	keeps the stage and the ToDo."""
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
		if refuse_partial:
			frappe.throw(_describe_shortfall(_available_for(short)), title=_("Not enough stock"))
		# the rollback also undid reserve_stock on the in-memory rows' db values; nothing else to restore
		if not keep_reservation:
			_release_reservations(order.name)
		set_stage(order.name, WAITING_FOR_STOCK)
		_open_todo(order.name, _available_for(short))
		return WAITING

	set_stage(order.name, CONFIRMED)
	_close_todo(order.name)
	return RESERVED


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
	from erpnext.stock.doctype.stock_reservation_entry.stock_reservation_entry import (
		get_available_qty_to_reserve,
	)

	return [
		(row.item_code, flt(row.stock_qty), max(flt(get_available_qty_to_reserve(row.item_code, row.warehouse)), 0))
		for row, _unreserved in short
	]


def _describe_shortfall(lines):
	"""``lines``: ``[(item_code, needed, available)]``. Plain text for an error."""
	return "; ".join(
		_("Item {0}: needed {1}, available {2}").format(item, _fmt(needed), _fmt(available))
		for item, needed, available in lines
	)


def _release_reservations(sales_order):
	"""Cancel the order's Stock Reservation Entries that nothing was delivered against (a partial reservation
	left by ERPNext's per-row re-reservation after an edit). System write: the caller has authorized the edit."""
	for name in frappe.get_all(
		"Stock Reservation Entry",
		filters={"voucher_type": "Sales Order", "voucher_no": sales_order, "docstatus": 1, "delivered_qty": 0},
		pluck="name",
		order_by="creation desc",
		limit_page_length=0,
	):
		entry = frappe.get_doc("Stock Reservation Entry", name)
		entry.flags.ignore_permissions = True
		entry.cancel()


def hold_for_edit(sales_order, short):
	"""Shopify raised a quantity the stock cannot cover (#500): the stage becomes Waiting for Stock and ONE Stock
	Manager ToDo lists item, needed and available. The order's own reservation (the old quantities) is kept.
	``short``: the result of ``edit_shortfalls``. Does not commit."""
	set_stage(sales_order, WAITING_FOR_STOCK)
	_open_todo(sales_order, short)


def edit_shortfalls(order, new_qty, added=(), lock=False):
	"""Dry run of an edit (#500): ``[(item_code, needed_extra, available)]`` of the (item, warehouse) pairs for
	which the order would need more free stock than exists.

	``order`` is the Sales Order document as stored, ``new_qty`` ``{row name: new quantity}`` (a row missing from
	it keeps its quantity; 0 releases the row). The extra need of a pair is its new total stock quantity minus
	what the order's rows hold in Stock Reservation Entries now, so a smaller row and a bigger one of the same
	pair net out. The free stock is ERPNext's ``get_available_qty_to_reserve`` (actual minus every reservation,
	this order's included, which is why only the extra is compared). ``lock``: lock the Bin rows first (the caller
	commits before and after, never across an external call). Empty when reservation is off or the order has a
	Product Bundle row (left alone, see the module docstring)."""
	if not reservation_enabled():
		return []
	rows = _stock_rows(order)
	if not rows:
		return []
	from erpnext.stock.doctype.stock_reservation_entry.stock_reservation_entry import (
		get_available_qty_to_reserve,
		get_sre_reserved_qty_details_for_voucher,
	)

	# Added rows (``added``: objects with item_code, qty, warehouse; stock UOM, so the stock quantity is qty)
	new_stock = [
		(row.item_code, row.warehouse, flt(row.qty))
		for row in added
		if row.warehouse
		and frappe.get_cached_value("Item", row.item_code, "is_stock_item")
		and not order.has_product_bundle(row.item_code)
	]
	if lock:
		_lock_bins({(row.item_code, row.warehouse) for row in rows} | {(i, w) for i, w, _q in new_stock})
	held = get_sre_reserved_qty_details_for_voucher("Sales Order", order.name)
	extra = {}
	for item_code, warehouse, stock_qty in new_stock:
		extra[(item_code, warehouse)] = extra.get((item_code, warehouse), 0) + stock_qty
	for row in rows:
		stock_qty = flt(new_qty.get(row.name, row.qty)) * flt(row.conversion_factor or 1)
		key = (row.item_code, row.warehouse)
		extra[key] = extra.get(key, 0) + stock_qty - flt(held.get(row.name))
	short = []
	for (item_code, warehouse), needed in sorted(extra.items()):
		available = max(flt(get_available_qty_to_reserve(item_code, warehouse)), 0)
		if needed > 0.0001 and needed - available > 0.0001:
			short.append((item_code, needed, available))
	return short


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
			"Stock Settings 'Enable Stock Reservation' is off, so Shopify orders are not reserved. "
			"Turn it on (Auto Reserve Stock stays off)."
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
			if _bins_changed_since(name, since) and not _rerun_held_update(name):
				reserve_or_wait(name)
			frappe.db.commit()
		except Exception:
			frappe.db.rollback()
			frappe.log_error(title="Order reservation retry failed", message=f"{name}\n{frappe.get_traceback()}")
	if not more:
		frappe.cache().set_value(_WATERMARK_KEY, tick)


def _rerun_held_update(sales_order):
	"""True when the order waits because Shopify raised a quantity the stock could not cover (#500): the
	Marketplace Order's planner is re-run (its debounce is bumped) instead of reserving the OLD quantities, which
	would show the order as Confirmed while Shopify's increase is still pending. The planner holds it again or
	applies the change and settles the reservation itself."""
	from marketplace_integrations.services import sync_reason_codes
	from marketplace_integrations.services.sales_order_sync_debounce import bump_sync_debounce

	marketplace_order = frappe.db.get_value("Sales Order", sales_order, "custom_marketplace_order")
	if not marketplace_order:
		return False
	if frappe.db.get_value("Marketplace Order", marketplace_order, "sync_reason_code") != (
		sync_reason_codes.WAITING_FOR_STOCK_EDIT
	):
		return False
	bump_sync_debounce(marketplace_order, "Scheduled Sweep")
	return True


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
