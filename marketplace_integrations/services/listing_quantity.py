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

``get_stock_quantities()`` is the plain bulk read: one ``Bin`` query for a
batch of items in one warehouse, always at the Item's own **stock UOM**
(no ``sales_uom`` conversion - Shopify variants have no UOM concept, #107
decision 5), negatives clamped to zero, floored to an integer.

Product Bundle awareness (#368): a mapped Item with an enabled ERPNext Product
Bundle is a non-stock parent with no Bin, so its sellable quantity is derived
from its components - ``min(floor(component stock / component qty))``, stock
UOM, clamped at zero (``resolve_stock_quantities()``). An Item without a
bundle keeps the plain Bin read. An invalid bundle (no components, missing or
non-stock component, component that is itself a bundle) is reported as a
``BundleStockError``, never turned into zero.
"""

from decimal import ROUND_FLOOR, Decimal

import frappe
from frappe import _

from marketplace_integrations.services.listing_price import CatalogBuildError, conversion_factor, to_decimal


class BundleStockError(CatalogBuildError):
	"""A Product Bundle whose stock cannot be computed (reported, not pushed)."""


def get_sellable_qty(item_code, warehouse, listing_uom):
	"""Return the sellable quantity in listing units, floored at zero.

	Plain Item: current Bin.actual_qty. Item with a Product Bundle: the bundle
	quantity from its components (see ``resolve_stock_quantities``). Raises
	``BundleStockError`` for an invalid bundle.
	"""
	item = frappe.get_doc("Item", item_code)
	quantities, problems = resolve_stock_quantities(warehouse, [item.name])
	if item.name in problems:
		raise BundleStockError(problems[item.name])
	factor = conversion_factor(item, listing_uom)
	return int((Decimal(quantities[item.name]) / factor).to_integral_value(rounding=ROUND_FLOOR))


def _actual_quantities(warehouse, item_codes, subtract_reserved=False):
	"""``{item_code: Decimal}`` of ``Bin.actual_qty`` clamped at zero, one query.
	An Item with no Bin row reads 0. Every requested code is present.

	``subtract_reserved=True`` (opt-in, Shopify inventory sync only) reads
	``Bin.actual_qty - Bin.reserved_qty`` instead, still clamped at zero.
	``reserved_qty`` is the Sales Order reservation; ``reserved_stock`` (Stock
	Reservation Entry) is deliberately not subtracted."""
	item_codes = list(dict.fromkeys(item_codes))
	if not item_codes:
		return {}
	rows = frappe.get_all(
		"Bin",
		filters={"warehouse": warehouse, "item_code": ["in", item_codes]},
		fields=["item_code", "actual_qty", "reserved_qty"],
	)
	qty_by_item = {}
	for row in rows:
		qty = to_decimal(row.actual_qty or 0)
		if subtract_reserved:
			qty -= to_decimal(row.reserved_qty or 0)
		qty_by_item[row.item_code] = qty
	return {item_code: max(qty_by_item.get(item_code, Decimal(0)), Decimal(0)) for item_code in item_codes}


def get_stock_quantities(warehouse, item_codes):
	"""Return ``{item_code: int}`` - the sellable stock-UOM quantity of every
	given Item in ``warehouse``, using ONE ``Bin`` query for the whole batch.

	``floor(max(Bin.actual_qty, 0))``: negative stock reads as 0, fractional
	stock is floored, and an Item with no Bin row in the warehouse reads as 0.
	Every requested item code is present in the result. Plain Bin read only; a
	Product Bundle parent reads 0 here - use ``resolve_stock_quantities()``.
	"""
	return {
		item_code: int(qty.to_integral_value(rounding=ROUND_FLOOR))
		for item_code, qty in _actual_quantities(warehouse, item_codes).items()
	}


def get_bundle_components(item_codes):
	"""``{parent_item: [component rows]}`` for every given Item that has an
	enabled Product Bundle (ERPNext's own definition, ``is_product_bundle``).
	A bundle with no component rows maps to an empty list. Items without a
	bundle are absent. Two queries for the whole batch."""
	item_codes = list(dict.fromkeys(item_codes))
	if not item_codes:
		return {}
	bundles = frappe.get_all(
		"Product Bundle",
		filters={"new_item_code": ["in", item_codes], "disabled": 0},
		fields=["name", "new_item_code"],
	)
	if not bundles:
		return {}
	parent_by_bundle = {bundle.name: bundle.new_item_code for bundle in bundles}
	rows = frappe.get_all(
		"Product Bundle Item",
		filters={"parent": ["in", list(parent_by_bundle)], "parenttype": "Product Bundle"},
		fields=["parent", "item_code", "qty"],
		order_by="idx asc",
	)
	components = {parent: [] for parent in parent_by_bundle.values()}
	for row in rows:
		components[parent_by_bundle[row.parent]].append(row)
	return components


def resolve_stock_quantities(warehouse, item_codes, subtract_reserved=False):
	"""Return ``(quantities, problems)`` for a batch of Items in ``warehouse``.

	``subtract_reserved=True`` (Shopify inventory sync only) uses
	``Bin.actual_qty - Bin.reserved_qty`` wherever ``Bin.actual_qty`` is named
	below, for plain Items and for every bundle component alike.

	``quantities`` is ``{item_code: int}`` in the Item's own stock UOM;
	``problems`` is ``{item_code: message}`` for an invalid bundle, which is
	then absent from ``quantities``. Every requested code is in exactly one.

	Plain Item: ``floor(max(Bin.actual_qty, 0))``. Bundle parent:
	``min(floor(component actual_qty / component qty))`` over its components,
	clamped at zero, from the same warehouse only. The component qty is in the
	component's stock UOM (ERPNext unpacks a bundle on a Delivery Note in the
	component's stock UOM and ignores the bundle row's uom).
	"""
	item_codes = list(dict.fromkeys(item_codes))
	bundles = get_bundle_components(item_codes)
	component_codes = sorted({row.item_code for rows in bundles.values() for row in rows})
	plain_codes = [code for code in item_codes if code not in bundles]
	actual = _actual_quantities(warehouse, plain_codes + component_codes, subtract_reserved=subtract_reserved)
	component_items = {}
	nested = set()
	if component_codes:
		component_items = {
			row.name: row
			for row in frappe.get_all(
				"Item", filters={"name": ["in", component_codes]}, fields=["name", "is_stock_item"]
			)
		}
		nested = set(get_bundle_components(component_codes))

	quantities = {code: int(actual[code].to_integral_value(rounding=ROUND_FLOOR)) for code in plain_codes}
	problems = {}
	for parent, rows in bundles.items():
		try:
			quantities[parent] = _bundle_quantity(parent, rows, component_items, nested, actual)
		except BundleStockError as error:
			problems[parent] = str(error)
	return quantities, problems


def _bundle_quantity(parent, rows, component_items, nested, actual):
	if not rows:
		raise BundleStockError(_("Product Bundle {0} has no component Items.").format(parent))
	available = []
	for row in rows:
		component = component_items.get(row.item_code)
		if not component:
			raise BundleStockError(
				_("Product Bundle {0}: component Item {1} does not exist.").format(parent, row.item_code)
			)
		if not component.is_stock_item:
			raise BundleStockError(
				_("Product Bundle {0}: component Item {1} is not a stock Item.").format(parent, row.item_code)
			)
		if row.item_code in nested:
			raise BundleStockError(
				_("Product Bundle {0}: component Item {1} is itself a Product Bundle.").format(
					parent, row.item_code
				)
			)
		required = to_decimal(row.qty or 0)
		if required <= 0:
			raise BundleStockError(
				_("Product Bundle {0}: component Item {1} needs a positive quantity.").format(
					parent, row.item_code
				)
			)
		available.append((actual[row.item_code] / required).to_integral_value(rounding=ROUND_FLOOR))
	return int(max(min(available), Decimal(0)))
