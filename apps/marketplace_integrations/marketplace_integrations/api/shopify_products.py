# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Authorized Desk API boundary for the Shopify catalog mapping sweep and
its results grid (#105). Thin delegation only - see
integrations/shopify/products.py for the actual permission checks and
business logic.

Permission shape (gap-check #4): every function here operates on an
already-saved Marketplace Account, so the correct precedent is
integrations/talabat/catalog.py::get_account() (a per-document ``read``
check on the loaded account) plus a doctype-level check on Marketplace
Product Mapping - NOT api/shopify_locations.py's doctype-level-only
pattern, which is specifically for an action that must also work on a
brand-new, unsaved Marketplace Account.
"""

import frappe

from marketplace_integrations.integrations.shopify import products


@frappe.whitelist(methods=["POST"])
def queue_catalog_sweep(account: str):
	"""Authorize and enqueue a background catalog sweep for one Shopify
	Marketplace Account. No Shopify HTTP call happens in this request."""
	return products.queue_catalog_sweep(account)


@frappe.whitelist()
def get_catalog_sweep_status(job_id: str):
	"""Poll a previously queued catalog sweep job's status/progress/result.
	Owner-scoped, same as services/item_import_jobs.py::get_status()."""
	return products.get_sweep_status(job_id)


@frappe.whitelist()
def get_last_catalog_sweep(account: str):
	"""Return the most recently completed/failed catalog sweep for one
	Marketplace Account (#139 section 1), for the reconciliation dashboard.
	Unlike get_catalog_sweep_status(), this is NOT owner-scoped to whoever
	queued the run - it's permission-checked the same way as get_account()
	instead, since any user with read access to the account/mapping doctype
	should be able to see the account's last sweep outcome, not just the user
	who happened to trigger it."""
	return products.get_last_catalog_sweep(account)


@frappe.whitelist(methods=["POST"])
def upsert_mapping(
	account: str,
	external_reference: str,
	item: str,
	external_product_id: str | None = None,
	external_sku: str | None = None,
	external_barcode: str | None = None,
):
	"""Create or update one Shopify variant GID -> ERPNext Item mapping from
	the catalog sweep results grid."""
	return products.upsert_mapping(
		account,
		external_reference,
		item,
		external_product_id=external_product_id,
		external_sku=external_sku,
		external_barcode=external_barcode,
	)
