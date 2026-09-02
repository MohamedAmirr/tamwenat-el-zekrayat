# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Post-commit ERPNext signals for Talabat catalog synchronization.

Handlers only identify current database scopes and enqueue work. Workers
always rebuild the catalog payload from current ERPNext state through #6.
"""

import json
from hashlib import sha256

import frappe
from frappe.utils import cstr

from marketplace_integrations.integrations.talabat import catalog_sync

COALESCE_SECONDS = 60
QUEUE = "long"


def stock_ledger_entry_after_insert(doc, _method=None):
	item_code = cstr(doc.item_code).strip()
	warehouse = cstr(doc.warehouse).strip()
	if item_code and warehouse:
		queue_item_warehouse_sync(item_code, warehouse)


def item_price_after_insert(doc, _method=None):
	_queue_general_price_scope(doc)


def item_price_after_delete(doc, _method=None):
	_queue_general_price_scope(doc)


def item_price_on_update(doc, _method=None):
	previous = doc.get_doc_before_save()
	if not previous or not _price_fields_changed(doc, previous):
		return
	_queue_general_price_scope(previous)
	_queue_general_price_scope(doc)


def item_on_update(doc, _method=None):
	previous = doc.get_doc_before_save()
	if not previous:
		return
	relevant_uoms = _mapped_listing_uoms(doc.name, doc.stock_uom, previous.stock_uom)
	if _item_catalog_signature(doc, relevant_uoms) != _item_catalog_signature(previous, relevant_uoms):
		queue_item_mappings(doc.name)


def marketplace_product_mapping_on_update(doc, _method=None):
	previous = doc.get_doc_before_save()
	if not previous or not _mapping_fields_changed(doc, previous):
		return
	previous_state = _mapping_state(previous) if _mapping_identity_changed(doc, previous) else None
	queue_mapping_sync(doc.name, previous_state)


def marketplace_account_on_update(doc, _method=None):
	previous = doc.get_doc_before_save()
	if not previous:
		return
	if not _account_fields_changed(doc, previous):
		return
	if previous.enabled and not doc.enabled:
		return
	if doc.enabled:
		queue_account_sync(doc.name)


def queue_item_warehouse_sync(item_code, warehouse):
	_enqueue_once(
		f"marketplace_catalog:stock:{item_code}:{warehouse}",
		sync_item_warehouse,
		item_code=item_code,
		warehouse=warehouse,
	)


def queue_item_price_scope(item_code, price_list, uom=None):
	uom = cstr(uom).strip()
	_enqueue_once(
		f"marketplace_catalog:price:{item_code}:{price_list}:{uom}",
		sync_item_price_scope,
		item_code=item_code,
		price_list=price_list,
		uom=uom or None,
	)


def queue_item_mappings(item_code):
	_enqueue_once(f"marketplace_catalog:item:{item_code}", sync_item_mappings, item_code=item_code)


def queue_mapping_sync(mapping_name, previous_state=None):
	identity_key = _identity_key(previous_state) if previous_state else "current"
	_enqueue_once(
		f"marketplace_catalog:mapping:{mapping_name}:{identity_key}",
		sync_mapping,
		mapping_name=mapping_name,
		previous_state=previous_state,
	)


def queue_account_sync(account_name):
	_enqueue_once(f"marketplace_catalog:account:{account_name}", sync_account, account_name=account_name)


def sync_item_warehouse(item_code, warehouse):
	accounts = frappe.get_all(
		"Marketplace Account",
		filters={
			"platform": "Talabat",
			"enabled": 1,
			"connection_doctype": "Talabat Connection",
			"warehouse": warehouse,
		},
		pluck="name",
	)
	for account_name in accounts:
		mapping_names = frappe.get_all(
			"Marketplace Product Mapping",
			filters={"marketplace_account": account_name, "item": item_code},
			pluck="name",
		)
		_sync_mapping_names(account_name, mapping_names)


def sync_item_price_scope(item_code, price_list, uom=None):
	price_list_doc = frappe.get_doc("Price List", price_list)
	item = frappe.get_doc("Item", item_code)
	for account_name in frappe.get_all(
		"Marketplace Account",
		filters={
			"platform": "Talabat",
			"enabled": 1,
			"connection_doctype": "Talabat Connection",
			"erpnext_selling_price_list": price_list_doc.name,
		},
		pluck="name",
	):
		mappings = frappe.get_all(
			"Marketplace Product Mapping",
			filters={"marketplace_account": account_name, "item": item.name},
			fields=["name", "sales_uom"],
		)
		mapping_names = [
			mapping.name
			for mapping in mappings
			if _price_scope_affects_mapping(
				mapping, item.stock_uom, uom, price_list_doc.price_not_uom_dependent
			)
		]
		_sync_mapping_names(account_name, mapping_names)


def sync_item_mappings(item_code):
	for account_name in _enabled_talabat_accounts():
		mapping_names = frappe.get_all(
			"Marketplace Product Mapping",
			filters={"marketplace_account": account_name, "item": item_code},
			pluck="name",
		)
		_sync_mapping_names(account_name, mapping_names)


def sync_mapping(mapping_name, previous_state=None):
	if not frappe.db.exists("Marketplace Product Mapping", mapping_name):
		return
	mapping = frappe.get_doc("Marketplace Product Mapping", mapping_name)
	if previous_state:
		_sync_previous_identity(mapping, previous_state)
	_sync_mapping_names(mapping.marketplace_account, [mapping.name])


def sync_account(account_name):
	_sync_mapping_names(account_name, None)


def _enqueue_once(cache_key, method, **kwargs):
	cache = frappe.cache()
	try:
		claimed = cache.set(cache.make_key(cache_key), "1", nx=True, ex=COALESCE_SECONDS)
	except Exception:
		claimed = True
	if not claimed:
		return False
	frappe.enqueue(method, queue=QUEUE, timeout=3600, enqueue_after_commit=True, **kwargs)
	return True


def _queue_general_price_scope(doc):
	if not _is_general_price(doc):
		return
	item_code = cstr(doc.item_code).strip()
	price_list = cstr(doc.price_list).strip()
	if item_code and price_list:
		queue_item_price_scope(item_code, price_list, cstr(doc.uom).strip() or None)


def _sync_mapping_names(account_name, mapping_names):
	try:
		if mapping_names == []:
			return
		catalog_sync.sync_catalog(account_name, mapping_names)
	except Exception:
		frappe.log_error(title="Talabat catalog event sync failed")


def _sync_previous_identity(mapping, previous_state):
	previous_account = cstr(previous_state.get("marketplace_account")).strip()
	if not previous_account:
		return
	previous_mapping = frappe._dict(
		{"doctype": "Marketplace Product Mapping", **previous_state, "name": mapping.name, "enabled": 0}
	)
	try:
		product = catalog_sync.build_catalog_product(previous_account, previous_mapping)
		catalog_sync.submit_catalog_products(
			previous_account, [{"mapping_name": mapping.name, "product": product}]
		)
	except Exception:
		frappe.log_error(title="Talabat previous catalog identity deactivation failed")


def _enabled_talabat_accounts():
	return frappe.get_all(
		"Marketplace Account",
		filters={"platform": "Talabat", "enabled": 1, "connection_doctype": "Talabat Connection"},
		pluck="name",
	)


def _mapped_listing_uoms(item_code, current_stock_uom, previous_stock_uom):
	mapped_uoms = {
		cstr(sales_uom).strip()
		for sales_uom in frappe.get_all(
			"Marketplace Product Mapping", filters={"item": item_code}, pluck="sales_uom"
		)
	}
	if "" in mapped_uoms:
		mapped_uoms.discard("")
		mapped_uoms.update({cstr(current_stock_uom).strip(), cstr(previous_stock_uom).strip()})
	return mapped_uoms


def _price_scope_affects_mapping(mapping, stock_uom, affected_uom, price_not_uom_dependent):
	listing_uom = cstr(mapping.sales_uom).strip() or cstr(stock_uom).strip()
	return (
		not affected_uom
		or listing_uom == affected_uom
		or (bool(price_not_uom_dependent) and affected_uom == stock_uom)
	)


def _item_catalog_signature(doc, relevant_uoms):
	return (
		bool(doc.disabled),
		cstr(doc.stock_uom).strip(),
		_first_barcode(doc),
		tuple(
			sorted(
				(cstr(row.uom).strip(), cstr(row.conversion_factor).strip())
				for row in (doc.get("uoms") or [])
				if cstr(row.uom).strip() in relevant_uoms
			)
		),
	)


def _first_barcode(doc):
	barcodes = sorted(doc.get("barcodes") or [], key=lambda row: row.idx or 0)
	return cstr(barcodes[0].barcode).strip() if barcodes else ""


def _price_fields_changed(doc, previous):
	return any(
		doc.get(field) != previous.get(field)
		for field in (
			"item_code",
			"price_list",
			"uom",
			"price_list_rate",
			"valid_from",
			"valid_upto",
			"customer",
			"supplier",
		)
	)


def _mapping_fields_changed(doc, previous):
	return any(
		doc.get(field) != previous.get(field)
		for field in (
			"enabled",
			"item",
			"sales_uom",
			"external_sku",
			"external_reference",
			"marketplace_account",
		)
	)


def _mapping_identity_changed(doc, previous):
	return any(
		doc.get(field) != previous.get(field)
		for field in ("item", "sales_uom", "external_sku", "external_reference", "marketplace_account")
	)


def _account_fields_changed(doc, previous):
	return any(
		doc.get(field) != previous.get(field)
		for field in ("warehouse", "erpnext_selling_price_list", "enabled")
	)


def _mapping_state(doc):
	return {
		"marketplace_account": doc.marketplace_account,
		"item": doc.item,
		"external_sku": doc.external_sku,
		"external_reference": doc.external_reference,
		"sales_uom": doc.sales_uom,
	}


def _identity_key(previous_state):
	return sha256(json.dumps(previous_state, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _is_general_price(doc):
	return not cstr(doc.customer).strip() and not cstr(doc.supplier).strip()
