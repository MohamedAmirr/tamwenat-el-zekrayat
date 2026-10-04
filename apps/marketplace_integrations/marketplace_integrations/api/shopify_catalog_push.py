# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Authorized Desk API boundary for the Shopify price-only catalog push
(#106). Thin delegation only - see integrations/shopify/catalog_push.py for
the actual permission checks and business logic.
"""

import frappe

from marketplace_integrations.integrations.shopify import catalog_push


@frappe.whitelist(methods=["POST"])
def queue_catalog_push(mapping_names):
	"""Authorize and enqueue a Shopify price push for the given Marketplace
	Product Mapping names. Only fast, local (non-network) account/connection/
	Price List checks run in this request - the Shopify shop-currency check
	and the actual price push both happen only in the background worker, not
	here. See integrations/shopify/catalog_push.py::queue_catalog_push()."""
	return catalog_push.queue_catalog_push(mapping_names)


@frappe.whitelist(methods=["POST"])
def queue_account_catalog_push(account: str):
	"""Authorize and enqueue a Shopify price push for every enabled mapping of
	one Marketplace Account (#168 "Sync All"). See
	integrations/shopify/catalog_push.py::queue_account_catalog_push()."""
	return catalog_push.queue_account_catalog_push(account)


@frappe.whitelist()
def get_catalog_push_status(job_id: str):
	"""Poll a previously queued catalog push job's status/progress/result.
	Owner-scoped, same as integrations/shopify/products.py's
	get_sweep_status()."""
	return catalog_push.get_push_status(job_id)
