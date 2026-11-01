# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Refuse item edits on a submitted Noon Sales Order (#368).

Noon has no order-level update, so a quantity, item or rate changed on the ERPNext Sales Order
could never reach Noon and the two would silently diverge.  The rule is per platform:
``BLOCKED_PLATFORMS`` lists the platforms that cannot take the edit.  Shopify and Talabat already
send edits to the platform and keep today's behaviour.

The check sits on the **Sales Order Item rows**, not on the parent.  ERPNext's
``update_child_qty_rate`` (the "Update Items" dialog) saves, inserts and deletes the child rows
first and only then reloads and saves the parent, so a parent-level
``before_update_after_submit`` hook would see no difference.  ``hooks.py`` registers, for
``Sales Order Item``: ``before_update_after_submit`` (an existing row's ``item_code``, ``uom``, ``qty``,
``rate``, ``conversion_factor`` or ``stock_qty`` changed; a row of a submitted order has ``docstatus`` 1, so Frappe saves it with the
``update_after_submit`` action, which runs only that hook and never ``before_save``), ``before_insert`` (a row added) and ``on_trash`` (a row removed).

A row is locked only when its parent Sales Order is submitted (``docstatus`` 1), is linked to a
Noon Marketplace Order and that order is not being synced right now.  So these stay allowed:

- Draft Sales Orders and Sales Orders under creation or submit: their rows are written by the
  parent's own save, which runs no row hooks, and ``docstatus`` is not 1 yet.
- The app's own writes.  ``services/sales_order_sync.py::sync_sales_order`` registers the
  Marketplace Order in ``frappe.flags.marketplace_syncing_orders`` for the whole run, and the
  reconciliation planner (``sales_order_reconciliation.execute``) only runs inside it.  The same
  marker already lets ``services/sales_order_recheck.py`` ignore the sync's own writes.  Changes
  made from Mark as Out of Stock on a Sales Order item row reach the Sales Order through that planner, so
  they pass.
- Header and custom fields: only the row fields above are compared, and the parent is not
  checked at all.
"""

import frappe
from frappe import _
from frappe.utils import flt

BLOCKED_PLATFORMS = ("Noon",)
ROW_DOCTYPE = "Sales Order Item"
# Besides item_code and uom: a different pack size (conversion_factor, stock_qty) is an item edit too.
COMPARED_NUMBERS = ("qty", "rate", "conversion_factor", "stock_qty")


def block_row_change(doc, method=None):
	"""``before_update_after_submit``: refuse a changed item, uom, quantity, rate or pack size of an existing row."""
	if not doc.get("name"):
		return
	before = frappe.db.get_value(ROW_DOCTYPE, doc.name, ["item_code", *COMPARED_NUMBERS, "uom"], as_dict=True)
	if not before:
		return  # not stored yet: a new row, handled by block_row_add
	if (
		before.item_code == doc.item_code
		and before.uom == doc.uom
		and all(flt(before[field], 6) == flt(doc.get(field), 6) for field in COMPARED_NUMBERS)
	):
		return
	_refuse_if_locked(doc)


def block_row_add(doc, method=None):
	"""``before_insert``: refuse a row added to a submitted Noon Sales Order."""
	_refuse_if_locked(doc)


def block_row_remove(doc, method=None):
	"""``on_trash``: refuse removing a row of a submitted Noon Sales Order."""
	_refuse_if_locked(doc)


def _refuse_if_locked(row):
	if not locked_marketplace_order(row):
		return
	frappe.throw(
		_(
			"Items, quantities and rates of a Noon order cannot be changed in ERPNext because Noon "
			"has no order update. Use Mark as Out of Stock on an item row of the Sales Order, or "
			"cancel and amend the Delivery Note."
		),
		title=_("Noon order items are locked"),
	)


def locked_marketplace_order(row):
	"""The Noon Marketplace Order that locks ``row``, or ``None`` when the row may change."""
	if row.get("parenttype") != "Sales Order" or not row.get("parent"):
		return None
	values = frappe.db.get_value("Sales Order", row.parent, ["docstatus", "custom_marketplace_order"], as_dict=True)
	if not values or values.docstatus != 1 or not values.custom_marketplace_order:
		return None
	marketplace_order = values.custom_marketplace_order
	if marketplace_order in (frappe.flags.get("marketplace_syncing_orders") or ()):
		return None
	if frappe.db.get_value("Marketplace Order", marketplace_order, "platform") not in BLOCKED_PLATFORMS:
		return None
	return marketplace_order
