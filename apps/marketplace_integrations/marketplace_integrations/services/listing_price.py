# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Shared, platform-neutral listing-price resolution.

Extracted out of ``integrations/talabat/catalog_sync.py`` (#106 decision 4) -
a pure extraction with no behavior change to Talabat's own catalog sync,
verified by ``integrations/talabat/test_catalog_sync.py`` staying green both
before and after. Talabat's own account/connection/price-list validation
(``validate_catalog_sync_account()``) stays in ``catalog_sync.py`` - this
module only resolves a price given an already-validated Price List document,
so it can be reused by Shopify's price-only push
(``integrations/shopify/catalog_push.py``, #106) without pulling in any
Talabat-specific account validation.

Shopify diverges from Talabat in one deliberate way (decision 4): it always
prices at the Item's stock UOM, ignoring
``Marketplace Product Mapping.sales_uom`` (Shopify variants have no UOM
concept) - callers choose the ``listing_uom`` they pass to
``resolve_listing_price()``; this module never infers which UOM to use for a
Talabat vs. a Shopify caller.
"""

from datetime import date
from decimal import Decimal, InvalidOperation

import frappe
from frappe import _
from frappe.utils import cstr, getdate


class CatalogBuildError(frappe.ValidationError):
	"""A local mapping/pricing problem that must not prevent other mappings syncing."""


def listing_uom(mapping, item):
	"""Return the Talabat-style listing UOM: mapping.sales_uom, falling back to
	the Item's own stock UOM."""
	uom = cstr(mapping.sales_uom).strip() or cstr(item.stock_uom).strip()
	if not uom:
		raise CatalogBuildError(_("Item {0} requires a stock UOM.").format(item.name))
	return uom


def conversion_factor(item, listing_uom_value):
	"""Return the Decimal conversion factor from item.stock_uom to listing_uom_value."""
	if listing_uom_value == item.stock_uom:
		return Decimal(1)
	factor = frappe.db.get_value(
		"UOM Conversion Detail", {"parent": item.name, "uom": listing_uom_value}, "conversion_factor"
	)
	if not factor or to_decimal(factor) <= 0:
		raise CatalogBuildError(
			_("Item {0} requires a positive conversion factor for listing UOM {1}.").format(
				item.name, listing_uom_value
			)
		)
	return to_decimal(factor)


def resolve_listing_price(price_list, item, listing_uom_value, on_date=None):
	"""Return the Decimal current generic Item Price for item at listing_uom_value.

	``price_list`` must already be a validated (enabled, selling) Price List
	document - this function does no account/connection/price-list validation
	of its own, that stays the caller's responsibility (Talabat's
	``validate_catalog_sync_account()``, or Shopify's own account/currency
	validation - ``validate_push_account()``).
	"""
	price_list_doc = _as_price_list(price_list)
	on_date = getdate(on_date) if on_date else getdate()
	prices = _current_generic_prices(price_list_doc.name, item.name, on_date)

	price = _pick_price(prices, listing_uom_value)
	if price:
		return to_decimal(price.price_list_rate)
	if not price_list_doc.price_not_uom_dependent:
		raise CatalogBuildError(
			_("Item {0} has no current Item Price for listing UOM {1} in Price List {2}.").format(
				item.name, listing_uom_value, price_list_doc.name
			)
		)

	stock_price = _pick_price(prices, item.stock_uom)
	if not stock_price:
		raise CatalogBuildError(
			_("Item {0} has no current Item Price in Price List {1}.").format(item.name, price_list_doc.name)
		)
	return to_decimal(stock_price.price_list_rate) * conversion_factor(item, listing_uom_value)


def _as_price_list(price_list):
	if getattr(price_list, "doctype", None) == "Price List":
		return price_list
	return frappe.get_doc("Price List", price_list)


def _current_generic_prices(price_list, item_code, on_date):
	rows = frappe.get_all(
		"Item Price",
		filters={"price_list": price_list, "item_code": item_code, "selling": 1},
		fields=[
			"name",
			"uom",
			"price_list_rate",
			"valid_from",
			"valid_upto",
			"creation",
			"customer",
			"supplier",
		],
		order_by="creation desc",
	)
	return [
		row
		for row in rows
		if not cstr(row.customer).strip()
		and not cstr(row.supplier).strip()
		and (not row.valid_from or getdate(row.valid_from) <= on_date)
		and (not row.valid_upto or getdate(row.valid_upto) >= on_date)
	]


def _pick_price(prices, uom):
	candidates = [row for row in prices if cstr(row.uom).strip() == uom]
	if not candidates:
		return None
	return max(
		candidates,
		key=lambda row: (getdate(row.valid_from) if row.valid_from else date.min, cstr(row.creation)),
	)


def to_decimal(value):
	try:
		return Decimal(str(value))
	except (InvalidOperation, TypeError, ValueError) as error:
		raise CatalogBuildError(_("A catalog price or quantity is not numeric.")) from error
