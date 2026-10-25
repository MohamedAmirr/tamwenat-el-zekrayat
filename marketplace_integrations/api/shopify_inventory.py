# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Authorized Desk API boundary for the manual Shopify inventory sync (#107).
Thin delegation only - see integrations/shopify/inventory_sync.py for the
actual permission checks and business logic.
"""

import frappe

from marketplace_integrations.integrations.shopify import inventory_pull, inventory_sync


@frappe.whitelist(methods=["POST"])
def queue_inventory_sync(mapping_names):
	"""Authorize and enqueue a Shopify inventory sync for the given Marketplace
	Product Mapping names. Only local (non-network) checks run in this request -
	the Location re-check and every Shopify call happen in the background worker.
	See integrations/shopify/inventory_sync.py::queue_inventory_sync()."""
	return inventory_sync.queue_inventory_sync(mapping_names)


@frappe.whitelist(methods=["POST"])
def queue_account_reconciliation(account: str):
	"""Authorize and enqueue a full inventory reconciliation of one Marketplace
	Account. Local checks only in this request. See
	integrations/shopify/inventory_sync.py::queue_account_reconciliation()."""
	return inventory_sync.queue_account_reconciliation(account)


@frappe.whitelist(methods=["POST"])
def queue_inventory_pull(account: str):
	"""Authorize and enqueue the one-time "Pull Shopify Inventory to Draft Stock
	Reconciliation" job. See integrations/shopify/inventory_pull.py."""
	return inventory_pull.queue_inventory_pull(account)


@frappe.whitelist()
def get_inventory_pull_drafts(account: str):
	"""Existing draft Stock Reconciliations made by the pull for this account's
	warehouse (shown as a warning before queuing another)."""
	return inventory_pull.find_existing_drafts(account)


@frappe.whitelist()
def get_inventory_sync_status(job_id: str):
	"""Poll a previously queued inventory sync job. Owner-scoped, same as
	api/shopify_catalog_push.py's get_catalog_push_status()."""
	return inventory_sync.get_sync_status(job_id)
