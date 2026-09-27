# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Authorized Shopify order import and refresh endpoints (#109)."""

import frappe
from frappe import _

from marketplace_integrations.integrations.shopify.orders import (
	get_order_import_status as _get_order_import_status,
	import_order,
	queue_order_import as _queue_order_import,
)


@frappe.whitelist(methods=["POST"])
def queue_order_import(account: str, start: str, end: str):
	return _queue_order_import(account, start, end)


@frappe.whitelist(methods=["POST"])
def get_order_import_status(job_id: str):
	return _get_order_import_status(job_id)


@frappe.whitelist(methods=["POST"])
def refresh_order(marketplace_order: str):
	doc = frappe.get_doc("Marketplace Order", marketplace_order)
	doc.check_permission("write")
	if doc.platform != "Shopify":
		frappe.throw(_("Marketplace Order {0} is not a Shopify order.").format(doc.name))
	updated = import_order(doc.marketplace_account, doc.external_order_id)
	return {"name": updated.name if updated else doc.name}
