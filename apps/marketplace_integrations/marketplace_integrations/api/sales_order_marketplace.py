# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Permission-aware Marketplace Order summary for the linked Sales Order tab."""

import frappe
from frappe import _
from frappe.utils import cstr, flt


@frappe.whitelist()
def get_marketplace_order_summary(sales_order: str):
	"""Return only the readable Marketplace Order linked to a readable Sales Order."""
	frappe.has_permission("Sales Order", "read", throw=True)
	if not frappe.db.exists("Sales Order", sales_order):
		frappe.throw(_("Sales Order {0} was not found. It may have been deleted.").format(sales_order))
	frappe.has_permission("Sales Order", "read", doc=sales_order, throw=True)

	marketplace_order = frappe.db.get_value("Sales Order", sales_order, "custom_marketplace_order")
	if not marketplace_order or not frappe.has_permission("Marketplace Order", "read"):
		return None

	# get_list applies both Marketplace Order document permissions and the platform query filter
	# (#322). The exact name and Sales Order link are both constrained so a different order
	# cannot be returned even if the link changes between the initial lookup and this query.
	rows = frappe.get_list(
		"Marketplace Order",
		filters={"name": marketplace_order, "erpnext_sales_order": sales_order},
		fields=[
			"name", "platform", "platform_order_reference", "external_order_id", "external_status",
			"external_financial_status", "external_fulfillment_status", "customer_first_name",
			"customer_last_name", "customer_phone_number", "customer_email", "customer_formatted_address",
			"customer_city", "payment_type", "payment_transactions", "sub_total", "discount",
			"order_total", "total_taxes", "shipping_total", "duties_total", "delivery_fee",
			"service_fee", "container_charge", "difference_to_minimum", "collect_at_pickup",
			"shop_currency", "erpnext_sales_order", "erpnext_sync_status", "last_sync_error",
			"cancelled_at", "cancellation_reason", "received_on", "last_platform_update_on",
			"last_erpnext_sync_on",
		],
		limit_page_length=1,
	)
	if not rows:
		return None
	order = rows[0]
	return {
		"name": order.name,
		"platform": order.platform or "",
		"reference": order.platform_order_reference or order.external_order_id or "",
		"external_order_id": order.external_order_id or "",
		"status": order.external_status or "",
		"financial_status": order.external_financial_status or "",
		"fulfillment_status": order.external_fulfillment_status or "",
		"cancelled_at": order.cancelled_at,
		"cancellation_reason": order.cancellation_reason or "",
		"received_on": order.received_on,
		"last_platform_update_on": order.last_platform_update_on,
		"customer": " ".join(filter(None, [order.customer_first_name, order.customer_last_name])),
		"phone": order.customer_phone_number or "",
		"email": order.customer_email or "",
		"address": order.customer_formatted_address or "",
		"city": order.customer_city or "",
		"amounts": {
			"sub_total": flt(order.sub_total), "discount": flt(order.discount),
			"order_total": flt(order.order_total), "total_taxes": flt(order.total_taxes),
			"shipping_total": flt(order.shipping_total), "duties_total": flt(order.duties_total),
			"delivery_fee": flt(order.delivery_fee), "service_fee": flt(order.service_fee),
			"container_charge": flt(order.container_charge), "difference_to_minimum": flt(order.difference_to_minimum),
			"collect_at_pickup": flt(order.collect_at_pickup),
			# Shopify stores shopMoney amounts, so the shop currency is the right one. Talabat's
			# payload has no currency and Noon stores no order amounts, so none is claimed for them.
			"currency": (order.shop_currency or "") if order.platform == "Shopify" else "",
		},
		"payment_type": order.payment_type or "",
		"payment_transactions": _json_list(order.payment_transactions),
		"mirrored_entries": _mirrored_entries(order.name),
		"latest_payment_log": _latest_payment_log(order.name),
		"erpnext_sales_order": order.erpnext_sales_order or "",
		"erpnext_sync_status": order.erpnext_sync_status or "",
		"last_erpnext_sync_on": order.last_erpnext_sync_on,
		"last_sync_error": cstr(order.last_sync_error),
	}


def _json_list(value):
	if not value:
		return []
	try:
		parsed = frappe.parse_json(value)
		return parsed if isinstance(parsed, list) else []
	except (TypeError, ValueError):
		return []


def _mirrored_entries(marketplace_order):
	if not frappe.has_permission("Payment Entry", "read"):
		return []
	return frappe.get_list(
		"Payment Entry",
		filters={"custom_marketplace_order": marketplace_order},
		fields=["name", "docstatus", "posting_date", "paid_amount", "custom_marketplace_transaction_id"],
		order_by="posting_date desc, creation desc",
		limit_page_length=50,
	)


def _latest_payment_log(marketplace_order):
	if not frappe.has_permission("Marketplace Sync Log", "read"):
		return None
	rows = frappe.get_list(
		"Marketplace Sync Log",
		filters={"marketplace_order": marketplace_order, "sync_type": "Payment"},
		fields=["status", "attempted_at", "response_data"],
		order_by="attempted_at desc, creation desc",
		limit_page_length=1,
	)
	if not rows:
		return None
	row = rows[0]
	try:
		response = frappe.parse_json(row.response_data) if row.response_data else {}
	except (TypeError, ValueError):
		response = {}
	message = response.get("message") if isinstance(response, dict) else None
	return {"status": row.status, "attempted_at": row.attempted_at, "message": cstr(message)}
