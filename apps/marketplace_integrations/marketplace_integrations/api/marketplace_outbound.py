# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Authorized API boundary for outbound order actions (#181).

Thin: authorizes the caller, then delegates to ``services/marketplace_outbound.py``,
which owns eligibility, the platform call and the echo-guard marker.
"""

import frappe
from frappe import _

from marketplace_integrations.services import marketplace_outbound


@frappe.whitelist(methods=["POST"])
def get_platform_cancel_option(sales_order: str):
	"""Whether the marketplace order behind this Sales Order can be cancelled from ERPNext."""
	sales_order_doc = frappe.get_doc("Sales Order", sales_order)
	sales_order_doc.check_permission("read")
	marketplace_order = sales_order_doc.get("custom_marketplace_order")
	if marketplace_order:
		frappe.get_doc("Marketplace Order", marketplace_order).check_permission("read")
	return marketplace_outbound.get_platform_cancel_option(sales_order_doc)


@frappe.whitelist(methods=["POST"])
def request_platform_cancel(sales_order: str, cancellation_reason: str = ""):
	"""Ask the marketplace to cancel the order behind this Sales Order.

	Needs submit permission on the Sales Order (it is the Sales Order that is being
	closed or cancelled) and write permission on the Marketplace Order (it is changed).
	"""
	sales_order_doc = frappe.get_doc("Sales Order", sales_order)
	sales_order_doc.check_permission("submit")
	marketplace_order = sales_order_doc.get("custom_marketplace_order")
	if not marketplace_order:
		frappe.throw(_("Sales Order {0} is not linked to a marketplace order.").format(sales_order_doc.name))
	frappe.get_doc("Marketplace Order", marketplace_order).check_permission("write")
	return marketplace_outbound.request_platform_cancel(sales_order_doc.name, cancellation_reason)


@frappe.whitelist(methods=["POST"])
def get_shopify_order_edit_preview(sales_order: str):
	"""Return a read-only preview of staff item changes eligible for Shopify.

	A user who may not read the linked Marketplace Order gets ``not_applicable``, not an
	error: this runs on every Sales Order form load, and a permission error would open
	Frappe's "Not permitted" popup.  Sending stays strict.
	"""
	sales_order_doc = frappe.get_doc("Sales Order", sales_order)
	sales_order_doc.check_permission("write")
	marketplace_order = sales_order_doc.get("custom_marketplace_order")
	if marketplace_order and not frappe.has_permission(
		"Marketplace Order", "read", doc=marketplace_order, throw=False
	):
		return {"state": marketplace_outbound.NOT_APPLICABLE, "changes": [], "reasons": []}
	return marketplace_outbound.get_shopify_order_edit_preview(sales_order_doc)


@frappe.whitelist(methods=["POST"])
def send_shopify_order_edit(sales_order: str, fingerprint: str = ""):
	"""Send the Shopify item changes staff reviewed; ``fingerprint`` comes from the preview.

	Needs write permission on the Sales Order and on the Marketplace Order (it is changed).
	"""
	sales_order_doc = frappe.get_doc("Sales Order", sales_order)
	sales_order_doc.check_permission("write")
	marketplace_order = sales_order_doc.get("custom_marketplace_order")
	if not marketplace_order:
		frappe.throw(_("Sales Order {0} is not linked to a marketplace order.").format(sales_order_doc.name))
	frappe.get_doc("Marketplace Order", marketplace_order).check_permission("write")
	return marketplace_outbound.send_shopify_order_edit(sales_order_doc.name, fingerprint)


@frappe.whitelist(methods=["POST"])
def clear_shopify_edit_marker(marketplace_order: str):
	"""Clear a Marketplace Order's Shopify item edit marker (recovery action, System Manager only)."""
	frappe.only_for("System Manager")
	return marketplace_outbound.clear_edit_marker_manually(marketplace_order)
