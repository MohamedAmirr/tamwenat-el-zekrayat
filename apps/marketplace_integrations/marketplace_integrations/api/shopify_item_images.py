# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Authorized Desk API boundary for the manual Shopify missing-image pull (#526).
Thin delegation only - see integrations/shopify/item_images.py for the permission checks and
business logic.
"""

import frappe

from marketplace_integrations.integrations.shopify import item_images


@frappe.whitelist(methods=["POST"])
def queue_pull_missing_images(account: str):
	"""Authorize and enqueue a missing-image pull for one Shopify Marketplace Account. Local checks
	only in this request. See integrations/shopify/item_images.py::queue_pull_missing_images()."""
	return item_images.queue_pull_missing_images(account)


@frappe.whitelist()
def get_pull_missing_images_status(job_id: str):
	"""Poll a queued image pull job. Owner-scoped, same as api/shopify_inventory.py's
	get_inventory_sync_status()."""
	return item_images.get_pull_status(job_id)
