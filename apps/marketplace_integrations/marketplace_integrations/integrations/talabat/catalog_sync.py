# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Deterministic ERPNext-to-Talabat catalog update batches.

This module owns the catalog-update contract only.  It deliberately does not
poll Talabat's beta job endpoint or mark mappings as ``Synced``: Talabat's
asynchronous catalog feedback is the authority for that later transition.
"""

from datetime import date
from decimal import ROUND_FLOOR, Decimal, InvalidOperation
from urllib.parse import quote

import frappe
from frappe import _
from frappe.utils import cstr, getdate, now_datetime

from marketplace_integrations.integrations.talabat.client import TalabatClient

CATALOG_BATCH_SIZE = 10_000
REMOTE_ERROR = _("Talabat did not accept the catalog update.")


class CatalogBuildError(frappe.ValidationError):
	"""A local mapping problem that must not prevent other mappings syncing."""


def validate_catalog_sync_account(account):
	"""Return the fully configured Talabat account, connection, and price list."""
	account_doc = _as_doc("Marketplace Account", account)
	if account_doc.platform != "Talabat":
		frappe.throw(_("Marketplace Account {0} is not a Talabat account.").format(account_doc.name))
	if account_doc.connection_doctype != "Talabat Connection" or not account_doc.connection:
		frappe.throw(_("Marketplace Account {0} does not use a Talabat Connection.").format(account_doc.name))
	if not account_doc.enabled:
		frappe.throw(_("Marketplace Account {0} is disabled.").format(account_doc.name))
	if not account_doc.warehouse:
		frappe.throw(
			_("Marketplace Account {0} requires a Warehouse for catalog sync.").format(account_doc.name)
		)
	if not account_doc.erpnext_selling_price_list:
		frappe.throw(
			_("Marketplace Account {0} requires an ERPNext Selling Price List for catalog sync.").format(
				account_doc.name
			)
		)

	connection = frappe.get_doc("Talabat Connection", account_doc.connection)
	if not connection.enabled:
		frappe.throw(_("Talabat Connection {0} is disabled.").format(connection.name))

	price_list = frappe.get_doc("Price List", account_doc.erpnext_selling_price_list)
	if not price_list.enabled:
		frappe.throw(_("Price List {0} is disabled.").format(price_list.name))
	if not price_list.selling:
		frappe.throw(_("Price List {0} is not a selling price list.").format(price_list.name))
	return account_doc, connection, price_list


def get_sellable_qty(item_code, warehouse, listing_uom):
	"""Return current Bin.actual_qty in listing units, floored at zero."""
	item = frappe.get_doc("Item", item_code)
	actual_qty = frappe.db.get_value("Bin", {"item_code": item.name, "warehouse": warehouse}, "actual_qty")
	qty = max(_decimal(actual_qty or 0), Decimal(0))
	conversion_factor = _conversion_factor(item, listing_uom)
	return int((qty / conversion_factor).to_integral_value(rounding=ROUND_FLOOR))


def get_listing_price(account, mapping, item, on_date=None):
	"""Return the configured selling price list's current generic Item Price."""
	_account_doc, _connection, price_list = validate_catalog_sync_account(account)
	mapping_doc = _as_doc("Marketplace Product Mapping", mapping)
	item_doc = _as_doc("Item", item)
	listing_uom = _listing_uom(mapping_doc, item_doc)
	on_date = getdate(on_date) if on_date else getdate()
	prices = _current_generic_prices(price_list.name, item_doc.name, on_date)

	price = _pick_price(prices, listing_uom)
	if price:
		return _decimal(price.price_list_rate)
	if not price_list.price_not_uom_dependent:
		raise CatalogBuildError(
			_("Item {0} has no current Item Price for listing UOM {1} in Price List {2}.").format(
				item_doc.name, listing_uom, price_list.name
			)
		)

	stock_price = _pick_price(prices, item_doc.stock_uom)
	if not stock_price:
		raise CatalogBuildError(
			_("Item {0} has no current Item Price in Price List {1}.").format(item_doc.name, price_list.name)
		)
	return _decimal(stock_price.price_list_rate) * _conversion_factor(item_doc, listing_uom)


def build_catalog_product(account, mapping):
	"""Build one Talabat EditProductRequest without changing local sync state."""
	account_doc, _connection, _price_list = validate_catalog_sync_account(account)
	mapping_doc = _as_doc("Marketplace Product Mapping", mapping)
	if mapping_doc.marketplace_account != account_doc.name:
		raise CatalogBuildError(
			_("Marketplace Product Mapping {0} belongs to another account.").format(mapping_doc.name)
		)

	item = frappe.get_doc("Item", mapping_doc.item)
	sku = cstr(mapping_doc.external_sku).strip() or cstr(mapping_doc.external_reference).strip()
	if not sku:
		raise CatalogBuildError(
			_("Marketplace Product Mapping {0} requires a Talabat SKU.").format(mapping_doc.name)
		)

	listing_uom = _listing_uom(mapping_doc, item)
	active = bool(mapping_doc.enabled) and not bool(item.disabled)
	product = {
		"sku": sku,
		"active": active,
		"quantity": get_sellable_qty(item.name, account_doc.warehouse, listing_uom) if active else 0,
		"price": float(get_listing_price(account_doc, mapping_doc, item)),
	}
	if barcode := _first_barcode(item.name):
		product["barcode"] = barcode
	return product


def build_catalog_batch(account, mapping_names=None):
	"""Build valid batches and collect per-mapping local build errors without writes."""
	account_doc, _connection, _price_list = validate_catalog_sync_account(account)
	valid = []
	errors = []
	for mapping in _mappings_for_account(account_doc.name, mapping_names):
		try:
			valid.append(
				{"mapping_name": mapping.name, "product": build_catalog_product(account_doc, mapping)}
			)
		except (CatalogBuildError, frappe.DoesNotExistError) as error:
			errors.append({"mapping_name": mapping.name, "error": _build_error(mapping, error)})

	batches = [
		valid[index : index + CATALOG_BATCH_SIZE] for index in range(0, len(valid), CATALOG_BATCH_SIZE)
	]
	return {"batches": batches, "errors": errors}


def submit_catalog_batch(account, mapping_names=None):
	"""Submit every valid catalog batch and persist only Queued/Error states."""
	account_doc, connection, _price_list = validate_catalog_sync_account(account)
	built = build_catalog_batch(account_doc, mapping_names)
	for error in built["errors"]:
		_mark_error(error["mapping_name"], error["error"])

	return {
		"batches": _submit_catalog_records(account_doc, connection, built["batches"]),
		"errors": built["errors"],
	}


def submit_catalog_products(account, records):
	"""Submit already-built products for a real mapping state transition.

	The event path uses this only to deactivate a mapping's previous external
	identity after it has been changed locally. Normal sync continues to build
	from current database state through ``submit_catalog_batch``.
	"""
	account_doc, connection, _price_list = validate_catalog_sync_account(account)
	if not records:
		return {"batches": []}
	batches = [
		records[index : index + CATALOG_BATCH_SIZE] for index in range(0, len(records), CATALOG_BATCH_SIZE)
	]
	return {"batches": _submit_catalog_records(account_doc, connection, batches)}


def _submit_catalog_records(account_doc, connection, batches):
	results = []
	for batch in batches:
		mapping_names_in_batch = [record["mapping_name"] for record in batch]
		try:
			response = TalabatClient(connection.name).request(
				"PUT",
				_catalog_path(connection.chain_id, account_doc.external_account_id),
				json={"products": [record["product"] for record in batch]},
			)
			job_id = _accepted_job_id(response)
		except Exception:
			for mapping_name in mapping_names_in_batch:
				_mark_error(mapping_name, REMOTE_ERROR)
			results.append({"mapping_names": mapping_names_in_batch, "ok": False})
			continue

		for mapping_name in mapping_names_in_batch:
			_mark_queued(mapping_name, job_id)
		results.append({"mapping_names": mapping_names_in_batch, "ok": True, "job_id": job_id})
	return results


def sync_catalog(account, mapping_names=None):
	"""Run a manual or later event-driven Talabat catalog synchronization."""
	return submit_catalog_batch(account, mapping_names)


def _as_doc(doctype, value):
	return value if getattr(value, "doctype", None) == doctype else frappe.get_doc(doctype, value)


def _mappings_for_account(account_name, mapping_names):
	filters = {"marketplace_account": account_name}
	if mapping_names is not None:
		if isinstance(mapping_names, str):
			mapping_names = [mapping_names]
		filters["name"] = ["in", list(mapping_names)]
	return [
		frappe.get_doc("Marketplace Product Mapping", row.name)
		for row in frappe.get_all(
			"Marketplace Product Mapping", filters=filters, fields=["name"], order_by="name asc"
		)
	]


def _listing_uom(mapping, item):
	listing_uom = cstr(mapping.sales_uom).strip() or cstr(item.stock_uom).strip()
	if not listing_uom:
		raise CatalogBuildError(_("Item {0} requires a stock UOM.").format(item.name))
	return listing_uom


def _conversion_factor(item, listing_uom):
	if listing_uom == item.stock_uom:
		return Decimal(1)
	factor = frappe.db.get_value(
		"UOM Conversion Detail", {"parent": item.name, "uom": listing_uom}, "conversion_factor"
	)
	if not factor or _decimal(factor) <= 0:
		raise CatalogBuildError(
			_("Item {0} requires a positive conversion factor for listing UOM {1}.").format(
				item.name, listing_uom
			)
		)
	return _decimal(factor)


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


def _first_barcode(item_code):
	barcode = frappe.db.get_value(
		"Item Barcode", {"parent": item_code, "parenttype": "Item"}, "barcode", order_by="idx asc"
	)
	return cstr(barcode).strip() or None


def _catalog_path(chain_id, vendor_id):
	return f"/v2/chains/{quote(cstr(chain_id), safe='')}/vendors/{quote(cstr(vendor_id), safe='')}/catalog"


def _accepted_job_id(response):
	if response.status_code != 202:
		raise CatalogBuildError(REMOTE_ERROR)
	try:
		body = response.json()
	except ValueError as error:
		raise CatalogBuildError(REMOTE_ERROR) from error
	job_id = cstr(body.get("job_id") if isinstance(body, dict) else "").strip()
	if not job_id:
		raise CatalogBuildError(REMOTE_ERROR)
	return job_id


def _mark_queued(mapping_name, job_id):
	frappe.db.set_value(
		"Marketplace Product Mapping",
		mapping_name,
		{
			"sync_status": "Queued",
			"last_sync_reference": job_id,
			"last_synced_on": now_datetime(),
			"last_error": None,
		},
		update_modified=False,
	)


def _mark_error(mapping_name, error):
	frappe.db.set_value(
		"Marketplace Product Mapping",
		mapping_name,
		{"sync_status": "Error", "last_sync_reference": None, "last_error": cstr(error).strip()},
		update_modified=False,
	)


def _build_error(mapping, error):
	message = cstr(error).strip()
	if message:
		return message
	return _(
		"Marketplace Product Mapping {0} could not be built from its ERPNext Item configuration."
	).format(mapping.name)


def _decimal(value):
	try:
		return Decimal(str(value))
	except (InvalidOperation, TypeError, ValueError) as error:
		raise CatalogBuildError(_("A catalog price or quantity is not numeric.")) from error
