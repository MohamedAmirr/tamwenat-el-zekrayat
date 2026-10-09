# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Noon account binding and read-only warehouse discovery."""

import frappe
from frappe import _
from frappe.query_builder import DocType
from frappe.utils import cstr

from marketplace_integrations.integrations.noon.client import NoonClient
from marketplace_integrations.services.mappings import upsert_mapping_for_listing

WAREHOUSES_PATH = "/warehouse-platform/v1/warehouses/list"
MAPPING_DOCTYPE = "Marketplace Product Mapping"


def get_account(account_name):
	"""Load an enabled Noon Marketplace Account with its typed connection."""
	account = frappe.get_doc("Marketplace Account", account_name)
	if account.platform != "Noon" or account.connection_doctype != "Noon Connection":
		frappe.throw(_("Marketplace Account {0} is not bound to a Noon Connection.").format(account.name))
	if not account.enabled:
		frappe.throw(_("Marketplace Account {0} is disabled.").format(account.name))
	if not account.connection:
		frappe.throw(_("Marketplace Account {0} requires a Noon Connection.").format(account.name))
	return account


def validate_connection_enabled(account):
	"""Raise a visible setup error if the bound Noon Connection cannot run."""
	connection = frappe.get_doc("Noon Connection", account.connection)
	if not connection.enabled:
		frappe.throw(_("Noon Connection {0} is disabled.").format(connection.name))


def list_warehouses(connection_name):
	"""Read all warehouse pages for one enabled Noon Connection."""
	connection = frappe.get_doc("Noon Connection", connection_name)
	if not connection.enabled:
		frappe.throw(_("Noon Connection {0} is disabled.").format(connection.name))
	client = NoonClient(connection.name)
	warehouses = []
	next_token = None
	seen_tokens = set()
	while True:
		payload = {"next_token": next_token} if next_token else {}
		response = client.request("POST", WAREHOUSES_PATH, json=payload)
		if response.status_code != 200:
			frappe.throw(client.error_message(response))
		try:
			body = response.json()
		except ValueError:
			frappe.throw(_("Noon returned an invalid warehouse response."))
		if not isinstance(body, dict) or not isinstance(body.get("warehouses"), list):
			frappe.throw(_("Noon returned an invalid warehouse response."))
		for warehouse in body["warehouses"]:
			if not isinstance(warehouse, dict) or not all(
				key in warehouse
				for key in ("warehouse_code", "display_name", "fulfillment_system_code", "is_active")
			):
				frappe.throw(_("Noon returned an invalid warehouse response."))
			warehouses.append(
				{
					"warehouse_code": warehouse["warehouse_code"],
					"display_name": warehouse["display_name"],
					"fulfillment_system_code": warehouse["fulfillment_system_code"],
					"is_active": warehouse["is_active"],
				}
			)
		next_token = body.get("next_token")
		if not next_token:
			return warehouses
		if not isinstance(next_token, str) or next_token in seen_tokens:
			frappe.throw(_("Noon returned an invalid warehouse pagination cursor."))
		seen_tokens.add(next_token)


def validate_warehouse_code(connection_name, warehouse_code):
	"""Require the selected Noon warehouse to exist and be active."""
	if not warehouse_code:
		return
	if not any(
		row["warehouse_code"] == warehouse_code and row["is_active"]
		for row in list_warehouses(connection_name)
	):
		frappe.throw(
			_("Noon Warehouse {0} is not active on Connection {1}.").format(warehouse_code, connection_name)
		)


def list_unmapped_order_skus(account_name):
	"""Return distinct Noon ``partner_sku`` values (stored as
	``Marketplace Order Item.external_sku``) on this account's Marketplace
	Orders that have no enabled Marketplace Product Mapping yet (#168 "Needs
	Mapping" for Noon).

	Order-derived only: it does NOT enumerate the Noon catalog (see
	docs/noon-integration/catalog-export-capability.md - Noon catalog export is
	blocked). No Noon HTTP call is made. A SKU is "unmapped" exactly when the
	resolver ``services/mappings.py::_resolve()`` would find no enabled mapping
	for it: an enabled mapping on an enabled account with the same
	``external_reference`` (both are stored stripped, and resolution is exact,
	so a Python set difference matches the resolver). Two set-based queries
	replace a per-SKU resolver loop, and the child rows are joined to their
	parents on the account/platform filters rather than read via an IN list of
	every parent name.
	"""
	account = get_account(account_name)
	account.check_permission("read")
	if not frappe.has_permission(MAPPING_DOCTYPE, ptype="read"):
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	# Parent visibility stays permission-scoped via frappe.get_list. Marketplace
	# Order has no row-level permission hook, so a visible parent here means the
	# join below covers the same rows.
	if not frappe.get_list(
		"Marketplace Order",
		filters={"marketplace_account": account.name, "platform": "Noon"},
		pluck="name",
		limit_page_length=1,
	):
		return []

	ORDER = DocType("Marketplace Order")
	ITEM = DocType("Marketplace Order Item")
	MPM = DocType(MAPPING_DOCTYPE)
	ACCOUNT = DocType("Marketplace Account")

	order_skus = (
		frappe.qb.from_(ITEM)
		.join(ORDER)
		.on(ORDER.name == ITEM.parent)
		.select(ITEM.external_sku)
		.distinct()
		.where(ITEM.parenttype == "Marketplace Order")
		.where(ORDER.marketplace_account == account.name)
		.where(ORDER.platform == "Noon")
		.run(as_dict=True)
	)
	mapped_references = (
		frappe.qb.from_(MPM)
		.join(ACCOUNT)
		.on(ACCOUNT.name == MPM.marketplace_account)
		.select(MPM.external_reference)
		.where(MPM.marketplace_account == account.name)
		.where(MPM.enabled == 1)
		.where(ACCOUNT.enabled == 1)
		.run(as_dict=True)
	)
	mapped = {cstr(row.external_reference).strip() for row in mapped_references}
	return sorted({cstr(row.external_sku).strip() for row in order_skus} - mapped - {""})


def upsert_mapping(account_name, external_reference, item):
	"""Create or update one Noon ``partner_sku`` -> ERPNext Item mapping.

	``external_reference`` is the Noon ``partner_sku`` exactly as
	``integrations/noon/order_mapper.py`` resolves it, so the same value is
	also written to ``external_sku``.
	"""
	account = get_account(account_name)
	account.check_permission("read")
	external_reference = cstr(external_reference).strip()
	item = cstr(item).strip()
	if not external_reference:
		frappe.throw(_("Noon partner SKU is required."))
	if not item:
		frappe.throw(_("Item is required."))

	item_doc = frappe.get_doc("Item", item)
	item_doc.check_permission("read")
	mapping = upsert_mapping_for_listing(account.name, external_reference, item_doc.name)
	return {"name": mapping.name}
