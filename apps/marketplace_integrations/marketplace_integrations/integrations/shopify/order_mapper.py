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
	customer = order.get("customer") or {}
	shipping_address = order.get("shippingAddress")
	billing_address = order.get("billingAddress")
	address = shipping_address or billing_address or {}
	default_email = customer.get("defaultEmailAddress") or {}
	default_phone = customer.get("defaultPhoneNumber") or {}

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
		"customer_external_id": customer.get("id"),
		"customer_first_name": customer.get("firstName"),
		"customer_last_name": customer.get("lastName"),
		"customer_phone_number": default_phone.get("phoneNumber"),
		"customer_email": default_email.get("emailAddress"),
		"customer_formatted_address": ", ".join(address.get("formatted") or []),
		"customer_city": address.get("city"),
		"customer_latitude": address.get("latitude"),
		"customer_longitude": address.get("longitude"),
		"billing_address": billing_address,
		"shipping_address": shipping_address,
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
	# Absent/null stays None (unknown): the Float columns coerce it to 0, so
	# ``fulfillment_quantities_known`` is what records that both were really read.
	unfulfilled_quantity = item.get("unfulfilledQuantity")
	non_fulfillable_quantity = item.get("nonFulfillableQuantity")
	transaction_money_bag = item.get("priceAfterAllDiscountsBeforeTaxesSet")
	transaction_amount = _money(transaction_money_bag)
	# Not originalUnitPrice x currentQuantity minus discountAllocations: allocations still include
	# discounts on refunded/removed quantities (shopify.dev LineItem), understating a partly refunded line.
	net_amount = _decimal(transaction_amount)
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
		"unfulfilled_quantity": unfulfilled_quantity,
		"non_fulfillable_quantity": non_fulfillable_quantity,
		"fulfillment_quantities_known": int(
			unfulfilled_quantity is not None and non_fulfillable_quantity is not None
		),
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
		frappe.throw(
			_("Shopify order {0} exceeds the supported {1} page size.").format(order.get("id"), fieldname)
		)
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
	except InvalidOperation, ValueError:
		return Decimal(0)


def _parse_datetime(value):
	if not value:
		return None
	datetime_value = get_datetime(value)
	if not datetime_value:
		return None
	return get_datetime_str(convert_utc_to_system_timezone(datetime_value))
