# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Shared draft Delivery Note service for marketplace orders (#368).

No marketplace action creates a Delivery Note outside Bosta, so ERPNext stock never moves for
the other platforms.  ``create_draft_delivery_note()`` is the one place that turns "these units
left the warehouse" into ONE draft Delivery Note on the order's Sales Order.  A person submits
it; nothing here ever submits.  The per-platform code only decides the trigger and which units
to hand over (Noon: ``integrations/noon/fulfillment.py::mark_handed_over``; Shopify and
Talabat have no caller yet because their triggers are undecided).  Bosta keeps its own
Delivery Note path.

How it works:

1. Each unit is an ``{"external_sku" or "item", "qty"}`` dict.  An ``external_sku`` is resolved to
   an Item through the Marketplace Product Mapping (``services/mappings.py``); a unit with an
   explicit ``item`` uses it directly.  Quantities are counted per Item.  One unit equals one
   Sales Order row quantity, because the sync builds the Sales Order qty from the unit count in
   the mapping's sales UOM.
2. The count is capped per Sales Order row at what is still undelivered: row ``qty`` minus
   ``delivered_qty`` minus the quantity of draft Delivery Notes that already reference the row,
   so a second call never over-delivers.
3. ERPNext's own ``make_delivery_note`` mapper builds the draft from the Sales Order (a Product
   Bundle parent row is unpacked into packed items by ERPNext when the draft is saved); the rows
   are then limited to the capped quantities.
4. Units that could not be drafted come back in ``unresolved`` (``{"external_sku", "item",
   "qty", "reason"}``), never silently dropped: ``unmapped`` (no enabled mapping),
   ``not_on_order`` (the Item has no Sales Order row) and ``excess`` (the platform order has
   more units of the Item than the Sales Order row quantity).  The last two mean the Sales Order is
   behind the platform order, so, unless ``queue_sync`` is false, the order is queued for the
   normal debounced sync (in memory, like ``set_sync_status_from_current_snapshot``) and the
   caller drafts those units again once the Sales Order has the rows.  The caller persists the
   ``unresolved`` units and keeps the order in Needs Action while they stay unresolved (Noon:
   ``integrations/noon/handover_state.py``); this service writes no status itself.

Permission: the session user needs create permission on Delivery Note (checked here, server
side).  The caller owns the transaction: this function never commits, and takes the Sales Order
row lock itself so two concurrent calls cannot both cap against the same undelivered quantity.
It takes a Marketplace Order document and leaves saving it (including the in-memory sync
queue) to the caller.
"""

import frappe
from frappe import _
from frappe.utils import cstr, flt

from marketplace_integrations.services.mappings import resolve_item

ORDER_DOCTYPE = "Marketplace Order"
# Reasons of an unresolved unit (see the module docstring).
UNMAPPED = "unmapped"
NOT_ON_ORDER = "not_on_order"
EXCESS = "excess"
# ``pending_sync_trigger_source`` option used when a handover queues a Sales Order sync.
RESYNC_TRIGGER = "ERPNext Document Change"


def create_draft_delivery_note(marketplace_order, units, queue_sync=True, expected_units=None, add_to=None):
	"""Create (or extend) a draft Delivery Note for ``units`` of ``marketplace_order``; never submits.

	``marketplace_order`` is a Marketplace Order document.  Returns ``{"delivery_note": name or
	None, "added_to_existing": bool, "delivered": {item: qty}, "unresolved": [entry, ...],
	"covered": {item: qty}}``.  ``delivery_note`` is ``None`` when nothing could be drafted.

	``add_to`` is the name of a Delivery Note that is still a draft: the granted rows are added to
	it (a row of the same Sales Order row gets its quantity raised) instead of creating a new
	note.  ``expected_units`` ({item: units the platform order has of that Item in total}) lets
	the caller tell "the Sales Order is behind" from "already covered by a Delivery Note someone
	else made": a shortfall that fits inside the Sales Order row quantity is ``covered`` (resolved,
	reported in ``covered``), and only the part the Sales Order really lacks is an ``excess``
	entry.  Without it every shortfall is an excess.
	"""
	frappe.has_permission("Delivery Note", "create", throw=True)
	if add_to:
		frappe.has_permission("Delivery Note", "write", throw=True)
	order = marketplace_order
	if not order.erpnext_sales_order:
		frappe.throw(_("Marketplace Order {0} has no Sales Order yet.").format(order.name))

	# Lock first so concurrent calls serialise before reading what is left to deliver.
	frappe.db.get_value("Sales Order", order.erpnext_sales_order, "name", for_update=True)
	sales_order = frappe.get_doc("Sales Order", order.erpnext_sales_order)
	if sales_order.docstatus != 1:
		frappe.throw(_("Sales Order {0} is not submitted.").format(sales_order.name))

	wanted, unresolved = _count_units(order, sales_order, units)
	grants, shortfall = _cap_to_undelivered(sales_order, wanted)
	covered = {}
	for item, qty in shortfall.items():
		row_qty = sum(flt(row.qty) for row in sales_order.items if row.item_code == item)
		missing = qty
		if expected_units is not None:
			missing = min(qty, max(flt(expected_units.get(item)) - row_qty, 0))
			if qty - missing > 0:
				covered[item] = qty - missing
		if missing > 0:
			unresolved.append(
				{"external_sku": wanted[item]["sku"], "item": item, "qty": missing, "reason": EXCESS}
			)

	result = {
		"delivery_note": None,
		"added_to_existing": False,
		"delivered": {},
		"unresolved": unresolved,
		"covered": covered,
	}
	if any(flt(qty) > 0 for qty in grants.values()):
		delivery_note = _build_draft(sales_order, grants)
		if delivery_note.items:
			if add_to:
				target = _merge_into_draft(add_to, delivery_note.items)
				result["added_to_existing"] = True
			else:
				delivery_note.insert()
				target = delivery_note
			result["delivery_note"] = target.name
			for row in delivery_note.items:
				item = _item_of_row(sales_order, row.so_detail)
				result["delivered"][item] = result["delivered"].get(item, 0) + flt(row.qty)
	if queue_sync and any(entry["reason"] in (EXCESS, NOT_ON_ORDER) for entry in unresolved):
		# The Sales Order is behind the platform order: let the planner bring it up to date.
		from marketplace_integrations.services.sales_order_sync import queue_resync_in_memory

		queue_resync_in_memory(order, RESYNC_TRIGGER)
	return result


def _merge_into_draft(name, rows):
	"""Add the mapped ``rows`` to the draft Delivery Note ``name`` and save it.

	A row of a Sales Order row the note already has raises that row's quantity; others are
	appended.  Saving the note rebuilds its packed items (ERPNext does that on validate).
	"""
	target = frappe.get_doc("Delivery Note", name)
	if target.docstatus != 0:
		frappe.throw(_("Delivery Note {0} is not a draft.").format(name))
	existing = {row.so_detail: row for row in target.items if row.so_detail}
	for row in rows:
		current = existing.get(row.so_detail)
		if current:
			current.qty = flt(current.qty) + flt(row.qty)
			current.stock_qty = flt(current.qty) * (flt(current.conversion_factor) or 1)
		else:
			target.append("items", row.as_dict(no_default_fields=True))
	target.save()
	return target


def _count_units(order, sales_order, units):
	"""``({item: {"qty", "sku"}}, unresolved)`` for the units, counted per Item.

	An ``external_sku`` is resolved once per SKU.  Units that cannot be matched to a Sales Order
	row are returned as unresolved entries, aggregated per SKU or Item.
	"""
	row_items = {row.item_code for row in sales_order.items}
	resolved_skus = {}
	wanted = {}
	lost = {}
	for unit in units or []:
		qty = flt(unit.get("qty"))
		sku = cstr(unit.get("external_sku")).strip()
		item = cstr(unit.get("item")).strip()
		if qty <= 0:
			continue
		reason = None
		if not item:
			if sku not in resolved_skus:
				resolved_skus[sku] = resolve_item(order.marketplace_account, sku)
			item = resolved_skus[sku]
			if not item:
				reason = UNMAPPED
		if not reason and item not in row_items:
			reason = NOT_ON_ORDER
		if reason:
			key = (sku, item or "", reason)
			lost[key] = lost.get(key, 0) + qty
			continue
		entry = wanted.setdefault(item, {"qty": 0, "sku": sku})
		entry["qty"] += qty
	unresolved = [
		{"external_sku": sku, "item": item, "qty": qty, "reason": reason}
		for (sku, item, reason), qty in lost.items()
	]
	return wanted, unresolved


def _draft_quantities(row_names):
	"""Quantity already on draft Delivery Notes per Sales Order row.

	``get_all`` on purpose: the cap must count every draft whatever the caller may read, or a
	user who cannot see a colleague's draft would over-deliver.  Return Delivery Notes carry a
	negative quantity and are excluded.
	"""
	if not row_names:
		return {}
	rows = frappe.get_all(
		"Delivery Note Item",
		filters={"so_detail": ["in", list(row_names)], "docstatus": 0, "qty": [">", 0]},
		fields=["so_detail", "qty"],
		parent_doctype="Delivery Note",
	)
	drafts = {}
	for row in rows:
		drafts[row.so_detail] = drafts.get(row.so_detail, 0) + flt(row.qty)
	return drafts


def _cap_to_undelivered(sales_order, wanted):
	"""``({so_detail: qty to deliver}, {item: qty that did not fit})`` for the wanted counts."""
	drafts = _draft_quantities([row.name for row in sales_order.items])
	grants = {}
	capped = {}
	for item, entry in wanted.items():
		remaining_to_place = flt(entry["qty"])
		for row in sales_order.items:
			if row.item_code != item or remaining_to_place <= 0:
				continue
			left = flt(row.qty) - flt(row.delivered_qty) - flt(drafts.get(row.name))
			grant = min(remaining_to_place, max(left, 0))
			if grant > 0:
				grants[row.name] = grant
				remaining_to_place -= grant
		if remaining_to_place > 0:
			capped[item] = remaining_to_place
	return grants, capped


def _build_draft(sales_order, grants):
	"""ERPNext's mapper output limited to the granted Sales Order rows and quantities."""
	from erpnext.selling.doctype.sales_order.sales_order import make_delivery_note

	delivery_note = make_delivery_note(sales_order.name)
	rows = []
	for row in delivery_note.items:
		grant = grants.get(row.so_detail)
		if not grant:
			continue
		row.qty = flt(grant)
		row.stock_qty = flt(row.qty) * (flt(row.conversion_factor) or 1)
		rows.append(row)
	delivery_note.set("items", rows)
	return delivery_note


def _item_of_row(sales_order, so_detail):
	return next((row.item_code for row in sales_order.items if row.name == so_detail), so_detail)
