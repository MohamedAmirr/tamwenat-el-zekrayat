# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Persistence for normalized marketplace orders.

Platform adapters own HTTP and schema normalization. This service owns only
the shared Marketplace Order write model and deliberately never resolves a
product mapping or creates ERPNext commercial documents.
"""

import frappe
from frappe import _
from frappe.utils import cstr

from marketplace_integrations.marketplace_integrations.doctype.marketplace_order.marketplace_order import (
	compute_order_identity_key,
)

ORDER_FIELDS = (
	"platform_order_reference",
	"order_code",
	"external_status",
	"external_financial_status",
	"external_fulfillment_status",
	"order_type",
	"transport_type",
	"promotion_status",
	"is_preorder",
	"accepted_for",
	"promised_for",
	"received_on",
	"processed_on",
	"cancelled_at",
	"last_platform_update_on",
	"cancelled_by",
	"cancellation_reason",
	"post_picked_up",
	"customer_external_id",
	"customer_first_name",
	"customer_last_name",
	"customer_phone_number",
	"customer_formatted_address",
	"customer_city",
	"customer_latitude",
	"customer_longitude",
	"customer_email",
	"payment_type",
	"sub_total",
	"discount",
	"order_total",
	"total_taxes",
	"delivery_fee",
	"service_fee",
	"container_charge",
	"difference_to_minimum",
	"collect_at_pickup",
	"shop_currency",
	"presentment_currency",
	"shipping_total",
	"duties_total",
)
JSON_ORDER_FIELDS = (
	"discounts",
	"additional_fees",
	"latest_payload",
	"billing_address",
	"shipping_address",
	"payment_transactions",
	"refunds",
)
ITEM_FIELDS = (
	"external_sku",
	"external_variant_id",
	"product_name",
	"instructions",
	"external_status",
	"integration_status",
	"replaced_external_item_id",
	"pricing_type",
	"original_quantity",
	"original_weight",
	"original_weighted_pieces",
	"original_unit_price",
	"original_list_price",
	"original_total_price",
	"fulfilled_quantity",
	"fulfilled_weight",
	"fulfilled_weighted_pieces",
	"fulfilled_unit_price",
	"fulfilled_total_price",
	"fulfilled_total_price_missing",
	"current_quantity",
	"unfulfilled_quantity",
	"non_fulfillable_quantity",
	"fulfillment_quantities_known",
	"net_amount",
	"transaction_amount",
	"transaction_amount_missing",
	"container_deposit",
)
JSON_ITEM_FIELDS = ("barcodes", "promotions")


def upsert_marketplace_order(marketplace_account, normalized_order, is_stale=None):
	"""Create or update one Marketplace Order and its Talabat line rows.

	``normalized_order`` is the platform adapter's canonical result. It must
	include an opaque ``external_order_id``, a full ``latest_payload``, and a
	list of normalized ``items``. Existing lines are found only by Talabat's
	``external_item_id``; SKU stays a product-mapping input, never an identity.
	Rows absent from a later payload are retained so a partial/recovery import
	cannot erase operational replacement history.
	"""
	if not isinstance(normalized_order, dict):
		frappe.throw(_("Normalized marketplace order must be an object."))
	if "latest_payload" not in normalized_order:
		frappe.throw(_("Normalized marketplace order must include the latest payload."))

	marketplace_account = cstr(marketplace_account).strip()
	external_order_id = cstr(normalized_order.get("external_order_id")).strip()
	identity_key = compute_order_identity_key(marketplace_account, external_order_id)
	name = frappe.db.get_value(
		"Marketplace Order",
		{"order_identity_key": identity_key},
		"name",
		for_update=bool(is_stale),
	)
	doc = frappe.get_doc("Marketplace Order", name) if name else frappe.new_doc("Marketplace Order")
	if name and is_stale and is_stale(doc):
		return doc
	doc.marketplace_account = marketplace_account
	doc.external_order_id = external_order_id
	_apply_order_fields(doc, normalized_order)
	_upsert_order_items(doc, normalized_order.get("items") or [])

	if doc.is_new():
		try:
			doc.insert()
		except frappe.UniqueValidationError:
			# The unique identity key is the atomic backstop for two workers
			# receiving the same order concurrently. Reload the winner and
			# apply this normalized payload to it rather than creating a second.
			name = frappe.db.get_value("Marketplace Order", {"order_identity_key": identity_key}, "name")
			if not name:
				raise
			# Lock the winner before comparing the incoming platform timestamp;
			# otherwise an older concurrent fetch could overwrite it immediately
			# after the first insert commits.
			frappe.db.get_value(
				"Marketplace Order", {"order_identity_key": identity_key}, "name", for_update=True
			)
			doc = frappe.get_doc("Marketplace Order", name)
			if is_stale and is_stale(doc):
				return doc
			_apply_order_fields(doc, normalized_order)
			_upsert_order_items(doc, normalized_order.get("items") or [])
			doc.save()
	else:
		doc.save()
	return doc


def _apply_order_fields(doc, normalized_order):
	for fieldname in ORDER_FIELDS:
		doc.set(fieldname, normalized_order.get(fieldname))
	for fieldname in JSON_ORDER_FIELDS:
		doc.set(fieldname, _as_json(normalized_order.get(fieldname)))


def _upsert_order_items(doc, items):
	if not isinstance(items, list):
		frappe.throw(_("Normalized marketplace order items must be a list."))

	item_meta = frappe.get_meta("Marketplace Order Item")
	by_external_item_id = {cstr(row.external_item_id).strip(): row for row in doc.items or []}
	seen_external_item_ids = set()
	for values in items:
		if not isinstance(values, dict):
			frappe.throw(_("Normalized marketplace order item must be an object."))
		external_item_id = cstr(values.get("external_item_id")).strip()
		if not external_item_id:
			frappe.throw(_("Normalized marketplace order item requires an External Item ID."))
		if external_item_id in seen_external_item_ids:
			frappe.throw(_("Normalized marketplace order contains duplicate External Item IDs."))
		seen_external_item_ids.add(external_item_id)

		row = by_external_item_id.get(external_item_id)
		if not row:
			row = doc.append("items", {})
			by_external_item_id[external_item_id] = row
		row.external_item_id = external_item_id
		for fieldname in ITEM_FIELDS:
			value = values.get(fieldname)
			if value is None:
				# On insert Frappe fills an unset Select with its first option (e.g. a
				# Shopify row's external_status "NOT_PROCESSED", pricing_type "UNIT"), but
				# not on update, so a re-import would blank it and look like a change to
				# the evidence fingerprint. Store the same value both times.
				field = item_meta.get_field(fieldname)
				value = field.default
				if value is None and field.fieldtype == "Select" and field.options:
					value = field.options.split("\n")[0]
			row.set(fieldname, value)
		for fieldname in JSON_ITEM_FIELDS:
			row.set(fieldname, _as_json(values.get(fieldname)))


def _as_json(value):
	if value is None or isinstance(value, str):
		return value
	return frappe.as_json(value)
