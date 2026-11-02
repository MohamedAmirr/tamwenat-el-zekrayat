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
def export_catalog(account: str):
	"""Download the full live Shopify variant catalog as an XLSX file."""
	return products.export_catalog(account)


@frappe.whitelist()
def get_catalog_sweep_status(job_id: str):
	"""Poll a previously queued catalog sweep job's status/progress/result.
	Owner-scoped: only the user who queued the sweep may read its status."""
	return products.get_sweep_status(job_id)


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
