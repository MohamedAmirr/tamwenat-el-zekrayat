# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Permission-checked Desk boundary for manual Noon FBPI order import
(#145, decision 12).

Thin by design: authorizes the caller, then enqueues the actual import on
the ``long`` queue - this module never makes a Noon HTTP call in the Desk
request itself, and never calls persistence directly (that's
``integrations/noon/orders.py``'s job, via the enqueued worker).
"""

import frappe

from marketplace_integrations.integrations.noon.orders import queue_order_import, queue_order_refresh


@frappe.whitelist(methods=["POST"])
def import_orders(account: str):
	"""Queue a manual order-import run for one Noon Marketplace Account.

	Requires ``Marketplace Account`` write (checked again, redundantly with
	``queue_order_import()``'s own check, since that's the specific
	document being acted on) plus doctype-level ``Marketplace Order``
	create and write (decision 12) - the permissions the worker's writes
	actually need.
	"""
	account_doc = frappe.get_doc("Marketplace Account", account)
	account_doc.check_permission("write")
	if not (
		frappe.has_permission("Marketplace Order", ptype="create")
		and frappe.has_permission("Marketplace Order", ptype="write")
	):
		frappe.throw(frappe._("Not permitted"), frappe.PermissionError)

	return queue_order_import(account_doc.name)


@frappe.whitelist(methods=["POST"])
def refresh_order(marketplace_order: str):
	"""Queue a manual ``GetFbpiOrder`` refresh for one existing Marketplace
	Order - a troubleshooting action, not the normal polling path.

	Unlike ``import_orders()`` above (scoped to a Marketplace Account),
	this is scoped to write permission on the specific Marketplace Order
	being refreshed (checked again, redundantly with
	``queue_order_refresh()``'s own check).

	Also rejects a non-Noon order (review finding D) before queueing
	anything: without this, a Talabat/Shopify order would still get queued
	and only fail inside the worker - with no ``Marketplace Sync Log`` row,
	since that failure happens before ``run_order_refresh()`` ever reaches
	the savepoint-protected code in ``integrations/noon/orders.py``.
	"""
	doc = frappe.get_doc("Marketplace Order", marketplace_order)
	doc.check_permission("write")
	if doc.platform != "Noon":
		frappe.throw(
			frappe._("Marketplace Order {0} is not a Noon order.").format(doc.name), frappe.ValidationError
		)

	return queue_order_refresh(doc.name)
