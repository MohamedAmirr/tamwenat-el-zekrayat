# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Talabat catalog browsing and mapping operations.

This module owns Talabat-specific catalog semantics: a Marketplace Account's
Talabat vendor ID is used with its dynamic Talabat Connection and a listing's
SKU is the canonical external reference.  Whitelisted methods only authorize
and delegate here; they do not contain catalog business logic.
"""

import frappe
from frappe import _
from frappe.utils import cint, cstr

from marketplace_integrations.integrations.talabat.client import TalabatClient

MAX_PAGE_SIZE = 500


def _require_mapping_permission(ptype):
	if not frappe.has_permission("Marketplace Product Mapping", ptype=ptype):
		frappe.throw(_("Not permitted"), frappe.PermissionError)


def get_account(account_name):
	"""Return an authorized Talabat Marketplace Account and its connection."""
	account = frappe.get_doc("Marketplace Account", account_name)
	account.check_permission("read")
	if account.platform != "Talabat":
		frappe.throw(_("Marketplace Account {0} is not a Talabat account.").format(account.name))
	if account.connection_doctype != "Talabat Connection":
		frappe.throw(_("Marketplace Account {0} does not use a Talabat Connection.").format(account.name))
	connection = frappe.get_doc("Talabat Connection", account.connection)
	connection.check_permission("read")
	return account, connection


def get_catalog_page(account_name, page=1, page_size=20):
	"""Fetch one live Talabat catalog page with current local mapping state."""
	account, connection = get_account(account_name)
	_require_mapping_permission("read")
	page = max(cint(page), 1)
	page_size = min(max(cint(page_size), 1), MAX_PAGE_SIZE)
	response = TalabatClient(connection.name).request(
		"GET",
		f"/v2/chains/{connection.chain_id}/vendors/{account.external_account_id}/catalog",
		params={"page": page, "page_size": page_size},
	)
	if not response.ok:
		frappe.throw(_("Failed to fetch Talabat's product catalog for this marketplace account."))

	body = response.json()
	products = body.get("products", [])
	page_skus = [product.get("sku") for product in products if product.get("sku")]
	item_by_sku = {}
	if page_skus:
		item_by_sku = {
			row.external_reference: row.item
			for row in frappe.get_all(
				"Marketplace Product Mapping",
				filters={"marketplace_account": account.name, "external_reference": ["in", page_skus]},
				fields=["external_reference", "item"],
			)
		}

	return {
		"rows": [
			{
				"sku": product.get("sku"),
				"title": product.get("title"),
				"price": product.get("price"),
				"item": item_by_sku.get(product.get("sku")),
			}
			for product in products
		],
		"has_more": cint(body.get("total_pages", 1)) > page,
	}


def get_mapped_items(account_name):
	"""Return all local mappings keyed by external reference for the account."""
	account, _connection = get_account(account_name)
	_require_mapping_permission("read")
	return {
		row.external_reference: row.item
		for row in frappe.get_all(
			"Marketplace Product Mapping",
			filters={"marketplace_account": account.name},
			fields=["external_reference", "item"],
		)
	}


def upsert_mapping(account_name, external_reference, item):
	"""Explicitly create or update one Talabat SKU → ERPNext Item mapping."""
	account, _connection = get_account(account_name)
	external_reference = cstr(external_reference).strip()
	item = cstr(item).strip()
	if not external_reference:
		frappe.throw(_("Talabat SKU is required."))
	if not item:
		frappe.throw(_("Item is required."))

	item_doc = frappe.get_doc("Item", item)
	item_doc.check_permission("read")
	existing = frappe.db.get_value(
		"Marketplace Product Mapping",
		{"marketplace_account": account.name, "external_reference": external_reference},
		"name",
	)
	if existing:
		mapping = frappe.get_doc("Marketplace Product Mapping", existing)
		mapping.check_permission("write")
		mapping.item = item_doc.name
		mapping.save()
	else:
		_require_mapping_permission("create")
		mapping = frappe.get_doc(
			{
				"doctype": "Marketplace Product Mapping",
				"marketplace_account": account.name,
				"external_reference": external_reference,
				"external_sku": external_reference,
				"item": item_doc.name,
			}
		)
		mapping.insert()
	return {"name": mapping.name}
