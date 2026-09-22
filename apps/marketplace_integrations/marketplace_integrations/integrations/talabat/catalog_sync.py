# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Deterministic ERPNext-to-Talabat catalog update batches.

This module owns the catalog-update contract only.  It deliberately does not
poll Talabat's beta job endpoint or mark mappings as ``Synced``: Talabat's
asynchronous catalog feedback is the authority for that later transition.

Listing-price resolution (``get_listing_price()`` and its helpers) was
extracted to ``services/listing_price.py`` (#106 decision 4) - a pure
extraction, no behavior change - so it can be shared with Shopify's
price-only push (``integrations/shopify/catalog_push.py``). This module keeps
its own Talabat-specific account/connection/price-list validation
(``validate_catalog_sync_account()``) and quantity logic (``get_sellable_qty()``),
neither of which is shared with Shopify.
"""

from decimal import ROUND_FLOOR
from urllib.parse import quote

import frappe
from frappe import _
from frappe.utils import cstr, getdate, now_datetime

from marketplace_integrations.integrations.talabat.client import TalabatClient
from marketplace_integrations.services.account_warehouse import validate_account_warehouse
from marketplace_integrations.services.listing_price import (
	CatalogBuildError,
	conversion_factor,
	resolve_listing_price,
	to_decimal,
)
from marketplace_integrations.services.listing_price import (
	listing_uom as compute_listing_uom,
)

CATALOG_BATCH_SIZE = 10_000
REMOTE_ERROR = _("Talabat did not accept the catalog update.")


def validate_catalog_sync_account(account):
	"""Return the fully configured Talabat account, connection, and price list."""
	account_doc = _as_doc("Marketplace Account", account)
	if account_doc.platform != "Talabat":
		frappe.throw(_("Marketplace Account {0} is not a Talabat account.").format(account_doc.name))
	if account_doc.connection_doctype != "Talabat Connection" or not account_doc.connection:
		frappe.throw(_("Marketplace Account {0} does not use a Talabat Connection.").format(account_doc.name))
	if not account_doc.enabled:
		frappe.throw(_("Marketplace Account {0} is disabled.").format(account_doc.name))
	validate_account_warehouse(account_doc, context=_("catalog sync"))
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
	qty = max(to_decimal(actual_qty or 0), to_decimal(0))
	factor = conversion_factor(item, listing_uom)
	return int((qty / factor).to_integral_value(rounding=ROUND_FLOOR))


def get_listing_price(account, mapping, item, on_date=None):
	"""Return the configured selling price list's current generic Item Price."""
	_account_doc, _connection, price_list = validate_catalog_sync_account(account)
	mapping_doc = _as_doc("Marketplace Product Mapping", mapping)
	item_doc = _as_doc("Item", item)
	listing_uom_value = compute_listing_uom(mapping_doc, item_doc)
	return resolve_listing_price(price_list, item_doc, listing_uom_value, on_date)


def effective_outbound_sku(external_sku, external_reference):
	"""Return the Talabat sku a mapping actually gets sent as.

	A non-blank ``external_sku`` wins over ``external_reference``. Shared with
	``integrations/talabat/catalog_feedback.py`` so a feedback CSV's ``sku``
	column - which echoes back whatever was actually sent - is correlated
	against exactly this same value rather than a separately-maintained copy
	of this precedence rule.
	"""
	return cstr(external_sku).strip() or cstr(external_reference).strip()


def build_catalog_product(account, mapping):
	"""Build one Talabat EditProductRequest without changing local sync state."""
	account_doc, _connection, _price_list = validate_catalog_sync_account(account)
	mapping_doc = _as_doc("Marketplace Product Mapping", mapping)
	if mapping_doc.marketplace_account != account_doc.name:
		raise CatalogBuildError(
			_("Marketplace Product Mapping {0} belongs to another account.").format(mapping_doc.name)
		)

	item = frappe.get_doc("Item", mapping_doc.item)
	sku = effective_outbound_sku(mapping_doc.external_sku, mapping_doc.external_reference)
	if not sku:
		raise CatalogBuildError(
			_("Marketplace Product Mapping {0} requires a Talabat SKU.").format(mapping_doc.name)
		)

	listing_uom_value = compute_listing_uom(mapping_doc, item)
	active = bool(mapping_doc.enabled) and not bool(item.disabled)
	product = {
		"sku": sku,
		"active": active,
		"quantity": get_sellable_qty(item.name, account_doc.warehouse, listing_uom_value) if active else 0,
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
