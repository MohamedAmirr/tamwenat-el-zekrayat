# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Shared, platform-neutral listing-quantity resolution.

``get_sellable_qty()`` was extracted out of
``integrations/talabat/catalog_sync.py`` (#107 decision 11) - a pure move, no
behavior change to Talabat's own catalog sync, verified by
``integrations/talabat/test_catalog_sync.py`` staying green both before and
after (same pattern as ``services/listing_price.py``, #106). Talabat still
calls it through ``catalog_sync.get_sellable_qty`` (re-imported there), so its
call path, error text and ordering are unchanged.

``get_stock_quantities()`` is the new bulk read used by Shopify's inventory
sync (``integrations/shopify/inventory_sync.py``): one ``Bin`` query for a
batch of items in one warehouse, always at the Item's own **stock UOM**
(no ``sales_uom`` conversion - Shopify variants have no UOM concept, #107
decision 5), negatives clamped to zero, floored to an integer.
"""

from decimal import ROUND_FLOOR, Decimal

import frappe

from marketplace_integrations.services.listing_price import conversion_factor, to_decimal


def get_sellable_qty(item_code, warehouse, listing_uom):
	"""Return current Bin.actual_qty in listing units, floored at zero."""
	item = frappe.get_doc("Item", item_code)
	actual_qty = frappe.db.get_value("Bin", {"item_code": item.name, "warehouse": warehouse}, "actual_qty")
	qty = max(to_decimal(actual_qty or 0), to_decimal(0))
	factor = conversion_factor(item, listing_uom)
	return int((qty / factor).to_integral_value(rounding=ROUND_FLOOR))


def get_stock_quantities(warehouse, item_codes):
	"""Return ``{item_code: int}`` - the sellable stock-UOM quantity of every
	given Item in ``warehouse``, using ONE ``Bin`` query for the whole batch.

	``floor(max(Bin.actual_qty, 0))``: negative stock reads as 0, fractional
	stock is floored, and an Item with no Bin row in the warehouse reads as 0.
	Every requested item code is present in the result.
	"""
	item_codes = list(dict.fromkeys(item_codes))
	if not item_codes:
		return {}
	rows = frappe.get_all(
		"Bin",
		filters={"warehouse": warehouse, "item_code": ["in", item_codes]},
		fields=["item_code", "actual_qty"],
	)
	actual_by_item = {row.item_code: row.actual_qty for row in rows}
	return {
		item_code: int(
			max(to_decimal(actual_by_item.get(item_code) or 0), Decimal(0)).to_integral_value(
				rounding=ROUND_FLOOR
			)
		)
		for item_code in item_codes
	}
