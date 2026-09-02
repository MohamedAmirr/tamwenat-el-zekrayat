# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Talabat order.Order -> normalized Marketplace Order translation.

This is the single shared translation path between manual GET/history
retrieval (integrations/talabat/orders.py, api/talabat_orders.py) and the
future incoming webhook processor (#58) - both must reuse
``normalize_order()`` rather than reimplementing order.Order field mapping.

``normalize_order()`` accepts one raw order.Order dict (either the direct
GET .../orders/{order_id} response body, or one entry of a
.../vendors/{vendor_id}/orders PagedOrderHistory's ``orders`` array - both
share the identical schema per docs/talabat-integration/openapi.json) and
returns the dict shape services/marketplace_orders.py's
``upsert_marketplace_order()`` expects. This module never calls Talabat or
writes a document - it is pure data translation.

Talabat's own external_status/item-status enum spellings (e.g. ``CANCELLED``)
are preserved exactly as documented, not remapped to an invented spelling.

Order-level ``promotion_status`` and per-item ``container_deposit``/
``promotion`` (-> ``promotions``) are preserved as external facts only (#83):
this module never derives merchant paid/settled state, commission, or any
other accounting interpretation from them.
"""

import frappe
from frappe import _
from frappe.utils import convert_utc_to_system_timezone, cstr, get_datetime, get_datetime_str


def normalize_order(order):
	"""Translate one raw Talabat order.Order object into the normalized
	structure ``upsert_marketplace_order()`` expects.

	Talabat's UUID ``order_id`` becomes the normalized ``external_order_id``
	(the Marketplace Order identity join key, since it's what
	GET/refresh/webhook calls all address the order by). Talabat's own
	merchant-facing order reference - confusingly also named
	``external_order_id`` in Talabat's own schema - is preserved separately
	as ``platform_order_reference``.
	"""
	if not isinstance(order, dict):
		frappe.throw(_("Talabat order payload must be an object."))

	order_id = cstr(order.get("order_id")).strip()
	if not order_id:
		frappe.throw(_("Talabat order payload is missing order_id."))

	customer = order.get("customer") or {}
	delivery_address = customer.get("delivery_address") or {}
	cancellation = order.get("cancellation") or {}
	payment = order.get("payment") or {}
	sys = order.get("sys") or {}

	return {
		"external_order_id": order_id,
		"platform_order_reference": order.get("external_order_id"),
		"order_code": order.get("order_code"),
		"external_status": order.get("status"),
		"order_type": order.get("order_type"),
		"transport_type": order.get("transport_type"),
		"promotion_status": order.get("promotion_status"),
		"is_preorder": 1 if order.get("isPreorder") else 0,
		"accepted_for": _parse_datetime(order.get("accepted_for")),
		"promised_for": _parse_datetime(order.get("promised_for")),
		"received_on": _parse_datetime(sys.get("created_at")),
		"last_platform_update_on": _parse_datetime(sys.get("updated_at")),
		"cancelled_by": cancellation.get("cancelled_by"),
		"cancellation_reason": cancellation.get("reason"),
		"post_picked_up": 1 if cancellation.get("post_picked_up") else 0,
		"customer_external_id": customer.get("_id"),
		"customer_first_name": customer.get("first_name"),
		"customer_last_name": customer.get("last_name"),
		"customer_phone_number": customer.get("phone_number"),
		"customer_formatted_address": delivery_address.get("formattedAddress"),
		"customer_city": delivery_address.get("city"),
		"customer_latitude": delivery_address.get("latitude"),
		"customer_longitude": delivery_address.get("longitude"),
		"payment_type": payment.get("type"),
		"sub_total": payment.get("sub_total"),
		"discount": payment.get("discount"),
		"order_total": payment.get("order_total"),
		"total_taxes": payment.get("total_taxes"),
		"delivery_fee": payment.get("delivery_fee"),
		"service_fee": payment.get("service_fee"),
		"container_charge": payment.get("container_charge"),
		"difference_to_minimum": payment.get("difference_to_minimum"),
		"collect_at_pickup": payment.get("collect_at_pickup"),
		"discounts": payment.get("discounts"),
		"additional_fees": payment.get("additional_fees"),
		"items": [_normalize_item(item) for item in order.get("items") or []],
		"latest_payload": order,
	}


def _normalize_item(item):
	if not isinstance(item, dict):
		frappe.throw(_("Talabat order item payload must be an object."))

	original_pricing = item.get("original_pricing") or {}
	pricing = item.get("pricing") or {}

	return {
		"external_item_id": item.get("_id"),
		"external_sku": item.get("sku"),
		"product_name": item.get("name"),
		"instructions": item.get("instructions"),
		"barcodes": item.get("barcode"),
		"external_status": item.get("status"),
		"replaced_external_item_id": item.get("replaced_id"),
		"pricing_type": original_pricing.get("pricing_type") or pricing.get("pricing_type"),
		"original_quantity": original_pricing.get("quantity"),
		"original_weight": original_pricing.get("weight"),
		"original_weighted_pieces": original_pricing.get("weighted_pieces"),
		"original_unit_price": original_pricing.get("unit_price"),
		"original_list_price": original_pricing.get("list_price"),
		"original_total_price": original_pricing.get("total_price"),
		"fulfilled_quantity": pricing.get("quantity"),
		"fulfilled_weight": pricing.get("weight"),
		"fulfilled_weighted_pieces": pricing.get("weighted_pieces"),
		"fulfilled_unit_price": pricing.get("unit_price"),
		"fulfilled_total_price": pricing.get("total_price"),
		"container_deposit": item.get("container_deposit"),
		"promotions": item.get("promotion"),
	}


def _parse_datetime(value):
	"""Convert a Talabat ISO 8601 UTC datetime string (e.g. with a trailing
	``Z`` or fractional seconds) into Frappe's canonical Datetime string, in
	the site's system timezone.

	Talabat's timestamps are UTC instants. ``frappe.utils.get_datetime()``
	parses an ISO ``...Z`` string into a UTC-aware ``datetime`` (Python's
	``datetime.fromisoformat`` recognizes the ``Z`` suffix), but
	``frappe.utils.get_datetime_str()`` only formats a datetime object's own
	wall-clock fields - it does not convert timezones. Calling it directly
	on the UTC-aware value would store the UTC digits verbatim as though
	they were already site-local wall-clock time. Converting through
	``convert_utc_to_system_timezone()`` first shifts the wall-clock fields
	to the site's configured timezone (Frappe's "System Settings > Time
	Zone"), so the stored Datetime string represents the correct site-local
	instant rather than a mislabeled copy of the UTC one.

	Returns ``None`` for a missing/blank/unparseable value rather than
	raising - a whole order should not fail to persist merely because one
	timestamp is absent or malformed; the raw value is still preserved
	verbatim in ``latest_payload`` regardless.
	"""
	value = cstr(value).strip()
	if not value:
		return None
	try:
		parsed = get_datetime(value)
		return get_datetime_str(convert_utc_to_system_timezone(parsed))
	except Exception:
		frappe.log_error(
			title="Talabat order: unparseable timestamp",
			message=f"Could not parse Talabat timestamp value: {value!r}",
		)
		return None
