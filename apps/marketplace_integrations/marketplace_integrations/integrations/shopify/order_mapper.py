# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Pure Shopify Admin GraphQL Order -> Marketplace Order normalization (#109)."""

from decimal import Decimal, InvalidOperation

import frappe
from frappe import _
from frappe.utils import convert_utc_to_system_timezone, cstr, get_datetime, get_datetime_str


def normalize_order(order):
	"""Normalize one fully-fetched Shopify GraphQL ``Order`` node.

	The Shopify GID is the identity value. The mapper intentionally accepts
	``None`` for protected customer fields so a later approved query can add
	them without changing the persistence contract.
	"""
	if not isinstance(order, dict):
		frappe.throw(_("Shopify order payload must be an object."))

	order_gid = cstr(order.get("id")).strip()
	if not order_gid:
		frappe.throw(_("Shopify order payload is missing id."))
	_validate_evidence_pages(order)

	return {
		"external_order_id": order_gid,
		"platform_order_reference": order.get("name"),
		"order_code": order.get("name"),
		"external_status": None,
		"external_financial_status": order.get("displayFinancialStatus"),
		"external_fulfillment_status": order.get("displayFulfillmentStatus"),
		"received_on": _parse_datetime(order.get("createdAt")),
		"processed_on": _parse_datetime(order.get("processedAt")),
		"last_platform_update_on": _parse_datetime(order.get("updatedAt")),
		"cancelled_at": _parse_datetime(order.get("cancelledAt")),
		"cancelled_by": None,
		"cancellation_reason": order.get("cancelReason"),
		"post_picked_up": None,
		"sub_total": _money(order.get("currentSubtotalPriceSet")),
		"discount": _money(order.get("currentTotalDiscountsSet")),
		"order_total": _money(order.get("currentTotalPriceSet")),
		"total_taxes": _money(order.get("currentTotalTaxSet")),
		"shipping_total": _money(order.get("currentShippingPriceSet")),
		"duties_total": _money(order.get("currentTotalDutiesSet")),
		"shop_currency": order.get("currencyCode"),
		"presentment_currency": order.get("presentmentCurrencyCode"),
		"payment_transactions": order.get("transactions"),
		"refunds": order.get("refunds"),
		"customer_external_id": None,
		"customer_first_name": None,
		"customer_last_name": None,
		"customer_phone_number": None,
		"customer_email": None,
		"customer_formatted_address": None,
		"customer_city": None,
		"customer_latitude": None,
		"customer_longitude": None,
		"billing_address": None,
		"shipping_address": None,
		"items": [_normalize_item(item) for item in _nodes(order, "lineItems")],
		"latest_payload": order,
	}


def _normalize_item(item):
	if not isinstance(item, dict):
		frappe.throw(_("Shopify line item payload must be an object."))

	variant = item.get("variant") or {}
	unit_price = _money(item.get("originalUnitPriceSet"))
	quantity = item.get("quantity")
	current_quantity = item.get("currentQuantity")
	net_amount = _decimal(unit_price) * _decimal(current_quantity)
	for allocation in item.get("discountAllocations") or []:
		net_amount -= _decimal(_money(allocation.get("allocatedAmountSet")))
	transaction_money_bag = item.get("priceAfterAllDiscountsBeforeTaxesSet")
	transaction_amount = _money(transaction_money_bag)
	transaction_amount_missing = int(
		not isinstance(transaction_money_bag, dict)
		or not isinstance(transaction_money_bag.get("shopMoney"), dict)
		or transaction_money_bag["shopMoney"].get("amount") is None
	)

	return {
		"external_item_id": item.get("id"),
		"external_sku": item.get("sku"),
		"external_variant_id": variant.get("id"),
		"product_name": item.get("name"),
		"original_quantity": quantity,
		"current_quantity": current_quantity,
		"original_unit_price": unit_price,
		"original_total_price": _decimal(unit_price) * _decimal(quantity),
		"net_amount": net_amount,
		"transaction_amount": transaction_amount,
		"transaction_amount_missing": transaction_amount_missing,
		"barcodes": None,
		"promotions": item.get("discountAllocations"),
	}


def _nodes(order, fieldname):
	connection = order.get(fieldname) or {}
	if connection.get("pageInfo", {}).get("hasNextPage"):
		frappe.throw(_("Shopify order {0} exceeds the supported {1} page size.").format(order.get("id"), fieldname))
	nodes = connection.get("nodes")
	if nodes is not None:
		return nodes
	return [edge.get("node") or {} for edge in connection.get("edges") or []]


def _validate_evidence_pages(order):
	connection = order.get("lineItems") or {}
	if (connection.get("pageInfo") or {}).get("hasNextPage"):
		frappe.throw(
			_("Shopify order {0} exceeds the supported lineItems page size.").format(order.get("id"))
		)
	for refund in order.get("refunds") or []:
		connection = refund.get("refundLineItems") or {}
		if (connection.get("pageInfo") or {}).get("hasNextPage"):
			frappe.throw(
				_("Shopify order {0} exceeds the supported refundLineItems page size.").format(
					order.get("id")
				)
			)


def _money(value):
	if not isinstance(value, dict):
		return None
	shop_money = value.get("shopMoney") or {}
	return shop_money.get("amount")


def _decimal(value):
	try:
		return Decimal(str(value or 0))
	except (InvalidOperation, ValueError):
		return Decimal(0)


def _parse_datetime(value):
	if not value:
		return None
	datetime_value = get_datetime(value)
	if not datetime_value:
		return None
	return get_datetime_str(convert_utc_to_system_timezone(datetime_value))
