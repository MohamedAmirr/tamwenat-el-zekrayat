# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Talabat order retrieval: single-order detail and vendor order history.

This module owns Talabat-specific order HTTP semantics only. It is
deliberately side-effect free - it never writes a Marketplace Order. Callers
(api/talabat_orders.py) normalize a raw response through order_mapper.py and
persist explicitly via services/marketplace_orders.py.

Account/connection resolution always goes through Marketplace Account ->
Talabat Connection; there is no fallback to the legacy Talabat Settings
Single. Unlike catalog browsing (integrations/talabat/catalog.py), order
retrieval also requires both the account and its connection to be enabled -
refreshing/importing operational order data should not run against a
disabled account or connection.
"""

from urllib.parse import quote

import frappe
from frappe import _
from frappe.utils import cint, cstr

from marketplace_integrations.integrations.talabat.client import TalabatClient

MIN_PAGE_SIZE = 1
MAX_PAGE_SIZE = 500
DEFAULT_PAGE_SIZE = 20
ORDER_UPDATE_STATUSES = {"CANCELLED", "DISPATCHED", "READY_FOR_PICKUP", "UPDATE_CART"}
ITEM_UPDATE_STATUSES = {"IN_CART", "NOT_FOUND", "REPLACED", "ADDITION"}
CANCELLATION_REASONS = {"CLOSED", "ITEM_UNAVAILABLE", "TOO_BUSY"}


def get_account(account_name):
	"""Resolve and authorize the Talabat Marketplace Account + Connection for
	order retrieval."""
	account = frappe.get_doc("Marketplace Account", account_name)
	account.check_permission("read")
	if account.platform != "Talabat":
		frappe.throw(_("Marketplace Account {0} is not a Talabat account.").format(account.name))
	if account.connection_doctype != "Talabat Connection":
		frappe.throw(_("Marketplace Account {0} does not use a Talabat Connection.").format(account.name))
	if not account.enabled:
		frappe.throw(_("Marketplace Account {0} is disabled.").format(account.name))

	connection = frappe.get_doc("Talabat Connection", account.connection)
	connection.check_permission("read")
	if not connection.enabled:
		frappe.throw(_("Talabat Connection {0} is disabled.").format(connection.name))

	return account, connection


def get_order(account, order_id):
	"""Fetch one Talabat order's current detail.

	GET /v2/chains/{chain_id}/orders/{order_id}. Returns the raw order.Order
	response body (dict) - normalize it via order_mapper.normalize_order()
	before persisting.

	Unlike get_vendor_orders(), this endpoint is addressed only by order_id -
	there is no vendor scoping in the URL itself. A Talabat Connection may be
	shared by several Marketplace Accounts (several vendors on the same
	chain), so a fetched order is verified to actually belong to this
	account's vendor (see _verify_order_belongs_to_account()) before it is
	returned - otherwise refresh_order() could silently pull one vendor's
	order into another vendor's Marketplace Account.
	"""
	account_doc, connection = get_account(account)
	order_id = cstr(order_id).strip()
	if not order_id:
		frappe.throw(_("Order ID is required."))

	response = TalabatClient(connection.name).request(
		"GET",
		f"/v2/chains/{quote(connection.chain_id, safe='')}/orders/{quote(order_id, safe='')}",
	)
	if response.status_code == 404:
		frappe.throw(_("Talabat order {0} was not found.").format(order_id))
	if not response.ok:
		frappe.throw(_("Failed to fetch Talabat order {0}.").format(order_id))

	order = response.json()
	_verify_order_belongs_to_account(order, account_doc)
	return order


def _verify_order_belongs_to_account(order, account_doc):
	"""Guard against a Talabat order fetched via a shared Connection
	belonging to a different vendor than the requested Marketplace Account.

	Per docs/talabat-integration/openapi.json's order.OrderClient schema,
	``client.id`` is the "Internal Client/Store/Vendor ID" - the same
	identifier get_vendor_orders() addresses vendors by via
	account.external_account_id - and ``client.store_id`` is the optional
	"External Client/Store/Vendor ID". Either matching the account's
	external_account_id is accepted. A payload with no client identity at
	all has nothing to verify against and is let through rather than
	raised, since a completely absent client block is not itself evidence
	of a mismatch.
	"""
	client = (order or {}).get("client") or {}
	client_id = cstr(client.get("id")).strip()
	store_id = cstr(client.get("store_id")).strip()
	if not client_id and not store_id:
		return

	expected = cstr(account_doc.external_account_id).strip()
	if expected and (client_id == expected or store_id == expected):
		return

	frappe.throw(
		_("Talabat order {0} belongs to a different vendor than Marketplace Account {1}.").format(
			cstr((order or {}).get("order_id")).strip(), account_doc.name
		)
	)


def get_vendor_orders(
	account,
	start_created_at_datetime=None,
	end_created_at_datetime=None,
	page=1,
	page_size=20,
):
	"""Fetch one page of a Talabat vendor's order history.

	GET /v2/chains/{chain_id}/vendors/{vendor_id}/orders. ``page_size`` is
	bounded to Talabat's documented 1-500 range. Returns the raw
	order.PagedOrderHistory response body (dict with ``orders``,
	``page_number``, ``page_size``, ``total_pages``) - normalize each entry
	of ``orders`` via order_mapper.normalize_order() before persisting.

	Talabat's documented query parameters for this date range are
	``start_time``/``end_time``; ``start_created_at_datetime``/
	``end_created_at_datetime`` are the corresponding (now deprecated per
	docs/talabat-integration/openapi.json) parameter names, kept here as this
	function's argument names to match #57's locked scope. They are sent on
	the wire as ``start_time``/``end_time``.
	"""
	account_doc, connection = get_account(account)
	page = max(cint(page), 1)
	page_size = min(max(cint(page_size), MIN_PAGE_SIZE), MAX_PAGE_SIZE)

	params = {"page": page, "page_size": page_size}
	start_created_at_datetime = cstr(start_created_at_datetime).strip()
	end_created_at_datetime = cstr(end_created_at_datetime).strip()
	if start_created_at_datetime:
		params["start_time"] = start_created_at_datetime
	if end_created_at_datetime:
		params["end_time"] = end_created_at_datetime

	response = TalabatClient(connection.name).request(
		"GET",
		f"/v2/chains/{quote(connection.chain_id, safe='')}/vendors/"
		f"{quote(account_doc.external_account_id, safe='')}/orders",
		params=params,
	)
	if not response.ok:
		frappe.throw(
			_("Failed to fetch Talabat vendor order history for {0}.").format(account_doc.name)
		)
	return response.json()


def update_order(account, order_id, payload):
	"""Send a validated order update to Talabat and return its raw order."""
	account_doc, connection = get_account(account)
	order_id = cstr(order_id).strip()
	if not order_id:
		frappe.throw(_("Order ID is required."))
	validate_order_update(payload)
	response = TalabatClient(connection.name).request(
		"PUT",
		f"/v2/chains/{quote(connection.chain_id, safe='')}/orders/{quote(order_id, safe='')}",
		json=payload,
	)
	if not response.ok:
		frappe.throw(_("Talabat rejected the order update."))
	result = response.json()
	_verify_order_belongs_to_account(result, account_doc)
	return result


def validate_order_update(payload):
	if not isinstance(payload, dict):
		frappe.throw(_("Order update must be an object."))
	status = cstr(payload.get("status")).strip()
	if status not in ORDER_UPDATE_STATUSES:
		frappe.throw(_("Unsupported Talabat order update status."))
	payload["status"] = status
	if not isinstance(payload.get("items"), list):
		frappe.throw(_("Order update items must be a list."))
	if status == "CANCELLED":
		reason = cstr((payload.get("cancellation") or {}).get("reason")).strip()
		if reason not in CANCELLATION_REASONS:
			frappe.throw(_("A valid cancellation reason is required."))
	for item in payload["items"]:
		if not isinstance(item, dict) or (
			not cstr(item.get("_id")).strip() and not cstr(item.get("sku")).strip()
		):
			frappe.throw(_("Every order item must include an ID or SKU."))
		if cstr(item.get("status")).strip() not in ITEM_UPDATE_STATUSES:
			frappe.throw(_("Every order item must have a valid update status."))
		item_status = cstr(item.get("status")).strip()
		item["status"] = item_status
		pricing = item.get("pricing")
		if not isinstance(pricing, dict) or pricing.get("pricing_type") not in {"UNIT", "KG"}:
			frappe.throw(_("Every order item must include pricing information."))
		try:
			quantity = float(pricing.get("quantity")) if "quantity" in pricing else None
			weight = float(pricing.get("weight")) if "weight" in pricing else None
			weighted_pieces = int(pricing.get("weighted_pieces")) if "weighted_pieces" in pricing else None
		except (TypeError, ValueError):
			frappe.throw(_("Order item pricing values must be numbers."))
		if item_status in {"IN_CART", "ADDITION"} and pricing["pricing_type"] == "UNIT":
			if quantity is None or quantity < 0:
				frappe.throw(_("UNIT items require a non-negative quantity."))
		elif item_status in {"IN_CART", "ADDITION"}:
			if weight is None or weight < 0:
				frappe.throw(_("KG items require a non-negative weight."))
			if weighted_pieces is not None and weighted_pieces < 0:
				frappe.throw(_("Weighted pieces cannot be negative."))
		if item_status in {"NOT_FOUND", "REPLACED"} and any(k in pricing for k in ("quantity", "weight")):
			frappe.throw(_("NOT_FOUND and REPLACED items cannot include fulfilled quantity or weight."))
	if status == "UPDATE_CART":
		by_id = {cstr(item.get("_id")).strip(): item for item in payload["items"] if cstr(item.get("_id")).strip()}
		for item in payload["items"]:
			item_status = cstr(item.get("status")).strip()
			replacement_id = cstr(item.get("replaced_id")).strip()
			if item_status == "REPLACED" and replacement_id:
				frappe.throw(_("Original replaced items must not include replaced_id."))
			if replacement_id:
				replacement = by_id.get(replacement_id)
				if item_status != "IN_CART" or not replacement or cstr(replacement.get("status")).strip().upper() != "REPLACED":
					frappe.throw(_("Replacement items must be IN_CART and reference a REPLACED item."))
		replaced_ids = {cstr(item.get("_id")).strip() for item in payload["items"] if cstr(item.get("status")).strip().upper() == "REPLACED"}
		for replaced_id in replaced_ids:
			if sum(cstr(item.get("replaced_id")).strip() == replaced_id for item in payload["items"]) != 1:
				frappe.throw(_("Each REPLACED item must have exactly one replacement."))
