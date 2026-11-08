# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Authorized API boundary for outbound order actions (#181).

Thin: authorizes the caller, then delegates to ``services/marketplace_outbound.py``,
which owns eligibility, the platform call and the echo-guard marker.
"""

import frappe
from frappe import _

from marketplace_integrations.services import marketplace_outbound, order_cancel, order_edit


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
def get_order_edit_preview(sales_order: str, items: str | list | None = None):
	"""Edit Order (#500): the editable rows, or what changing them would do, without any network call.

	Anyone who may edit the Sales Order may ask. ``items`` is ``[{"docname", "qty"}]``.
	"""
	sales_order_doc = frappe.get_doc("Sales Order", sales_order)
	sales_order_doc.check_permission("write")
	return order_edit.get_edit_preview(sales_order_doc, items)


@frappe.whitelist(methods=["POST"])
def edit_order(sales_order: str, items: str | list, fingerprint: str = ""):
	"""Edit Order (#500): send the reviewed quantities to Shopify, then change ERPNext in one transaction.

	Same permission as ERPNext's own Update Items (write and submit on the Sales Order, which
	``update_child_qty_rate`` checks on the session user); the Marketplace Order marker is a system write,
	so no Marketplace Order permission is needed. After the pickup nobody may edit (no override role).
	"""
	sales_order_doc = frappe.get_doc("Sales Order", sales_order)
	sales_order_doc.check_permission("write")
	sales_order_doc.check_permission("submit")
	return order_edit.edit_order(sales_order_doc, items, fingerprint)


@frappe.whitelist(methods=["POST"])
def cancel_order(sales_order: str, cancellation_reason: str = ""):
	"""Cancel a Shopify order before the carrier picked it up (#501): carrier shipment, draft Delivery Note, Sales
	Order, then Shopify, stopping at the first failure (call again to continue). Needs cancel permission on the
	Sales Order and write on the Marketplace Order (the platform cancel marks it); each step checks its own
	document's permission as the session user."""
	sales_order_doc = frappe.get_doc("Sales Order", sales_order)
	sales_order_doc.check_permission("cancel")
	marketplace_order = sales_order_doc.get("custom_marketplace_order")
	if not marketplace_order:
		frappe.throw(_("Sales Order {0} is not linked to a marketplace order.").format(sales_order_doc.name))
	frappe.get_doc("Marketplace Order", marketplace_order).check_permission("write")
	return order_cancel.cancel_order(sales_order_doc.name, cancellation_reason)


@frappe.whitelist(methods=["POST"])
def clear_shopify_edit_marker(marketplace_order: str):
	"""Clear a Marketplace Order's Shopify item edit marker (recovery action, System Manager only)."""
	frappe.only_for("System Manager")
	return marketplace_outbound.clear_edit_marker_manually(marketplace_order)
