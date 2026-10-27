# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Permission-checked Desk boundary for Noon warehouse discovery."""

import frappe

from marketplace_integrations.integrations.noon.catalog import get_account, list_unmapped_order_skus
from marketplace_integrations.integrations.noon.catalog import list_warehouses as discover_warehouses
from marketplace_integrations.integrations.noon.catalog import upsert_mapping as upsert_noon_mapping
from marketplace_integrations.integrations.noon.inventory import push_stock
from marketplace_integrations.integrations.noon.pricing import push_pricing
from marketplace_integrations.integrations.noon.sync import authorize_selection, enqueue_push


@frappe.whitelist(methods=["POST"])
def list_warehouses(connection: str):
	if not frappe.has_permission("Marketplace Account", "write"):
		raise frappe.PermissionError
	if not frappe.db.exists("Noon Connection", connection):
		frappe.throw(frappe._("Noon Connection {0} does not exist.").format(connection))
	# The doc-less Marketplace Account check above passes for any platform user (#322); the
	# outbound call uses this Connection's credentials, so the caller must be able to read it.
	frappe.has_permission("Noon Connection", "read", doc=connection, throw=True)
	return discover_warehouses(connection)


@frappe.whitelist(methods=["POST"])
def queue_account_sync(account: str, mapping_names=None):
	"""Manual Desk entry point: queue both updates for one account; each worker owns its
	statuses. Runs once whatever the account push switches say (they govern only the
	automatic triggers); "switch_off" in each result flags that the switch was bypassed."""
	mapping_names = authorize_selection(account, mapping_names)
	get_account(account)
	return {
		kind: enqueue_push(worker, kind, account, mapping_names, manual=True)
		for kind, worker in (("pricing", push_pricing), ("stock", push_stock))
	}


@frappe.whitelist()
def get_unmapped_order_skus(account: str):
	"""Noon "Needs Mapping" list derived from order imports (#168). Not a catalog
	export - see integrations/noon/catalog.py::list_unmapped_order_skus()."""
	return list_unmapped_order_skus(account)


@frappe.whitelist(methods=["POST"])
def upsert_mapping(account: str, external_reference: str, item: str):
	"""Create or update one Noon partner SKU -> ERPNext Item mapping (#168)."""
	return upsert_noon_mapping(account, external_reference, item)
