# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Authorized API boundary for the Marketplace Talabat catalog page."""

import frappe

from marketplace_integrations.integrations.talabat import catalog, catalog_sync
from marketplace_integrations.services.mapping_push import group_mappings_by_account, parse_mapping_names


@frappe.whitelist()
def get_catalog_page(marketplace_account, page=1, page_size=20):
	return catalog.get_catalog_page(marketplace_account, page, page_size)


@frappe.whitelist()
def get_mapped_items(marketplace_account):
	return catalog.get_mapped_items(marketplace_account)


@frappe.whitelist()
def upsert_mapping(marketplace_account, external_reference, item):
	return catalog.upsert_mapping(marketplace_account, external_reference, item)


@frappe.whitelist()
def disable_mapping(marketplace_account, external_reference):
	return catalog.disable_mapping(marketplace_account, external_reference)


@frappe.whitelist(methods=["POST"])
def queue_catalog_sync(account: str):
	"""Authorize and enqueue a Talabat catalog update without remote HTTP here."""
	account_doc = frappe.get_doc("Marketplace Account", account)
	account_doc.check_permission("write")
	if not frappe.has_permission("Marketplace Product Mapping", ptype="write"):
		frappe.throw(frappe._("Not permitted"), frappe.PermissionError)
	catalog_sync.validate_catalog_sync_account(account_doc)
	frappe.enqueue(
		catalog_sync.sync_catalog,
		queue="long",
		timeout=3600,
		enqueue_after_commit=True,
		account=account_doc.name,
	)
	return {"queued": True}


@frappe.whitelist(methods=["POST"])
def queue_mapping_catalog_sync(mapping: str):
	"""Authorize and enqueue one Mapping's current Talabat catalog state."""
	_queue_mapping_catalog_syncs([mapping])
	return {"queued": True}


@frappe.whitelist(methods=["POST"])
def queue_mappings_catalog_sync(mapping_names):
	"""Authorize and enqueue selected Mappings' current Talabat catalog state."""
	mapping_names = parse_mapping_names(mapping_names)
	accounts = _queue_mapping_catalog_syncs(mapping_names)
	return {"queued": len(mapping_names), "jobs": len(accounts)}


def _queue_mapping_catalog_syncs(mapping_names):
	"""Check every selected Mapping before enqueueing one job per Account."""
	accounts = group_mappings_by_account(mapping_names)

	for selected in accounts.values():
		catalog_sync.validate_catalog_sync_account(selected["account"])

	for selected in accounts.values():
		frappe.enqueue(
			catalog_sync.sync_catalog,
			queue="long",
			timeout=3600,
			enqueue_after_commit=True,
			account=selected["account"].name,
			mapping_names=selected["mapping_names"],
		)
	return accounts
