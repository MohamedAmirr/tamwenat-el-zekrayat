# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Authorized API boundary for the Marketplace Talabat catalog page."""

import frappe

from marketplace_integrations.integrations.talabat import catalog, catalog_sync


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
	mapping_names = _parse_mapping_names(mapping_names)
	accounts = _queue_mapping_catalog_syncs(mapping_names)
	return {"queued": len(mapping_names), "jobs": len(accounts)}


def _parse_mapping_names(mapping_names):
	try:
		mapping_names = frappe.parse_json(mapping_names) if isinstance(mapping_names, str) else mapping_names
	except (TypeError, ValueError):
		frappe.throw(frappe._("Marketplace Product Mappings must be valid JSON."))
	if not isinstance(mapping_names, list) or not mapping_names:
		frappe.throw(frappe._("Select at least one Marketplace Product Mapping."))
	if any(not isinstance(name, str) or not name.strip() for name in mapping_names):
		frappe.throw(frappe._("Marketplace Product Mappings must be valid names."))
	return list(dict.fromkeys(name.strip() for name in mapping_names))


def _queue_mapping_catalog_syncs(mapping_names):
	"""Check every selected Mapping before enqueueing one job per Account."""
	accounts = {}
	for mapping_name in mapping_names:
		mapping_doc = frappe.get_doc("Marketplace Product Mapping", mapping_name)
		mapping_doc.check_permission("write")
		account_doc = frappe.get_doc("Marketplace Account", mapping_doc.marketplace_account)
		account_doc.check_permission("write")
		accounts.setdefault(account_doc.name, {"account": account_doc, "mapping_names": []})[
			"mapping_names"
		].append(mapping_doc.name)

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
