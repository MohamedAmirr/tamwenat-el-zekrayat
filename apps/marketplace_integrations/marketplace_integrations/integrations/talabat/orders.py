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
