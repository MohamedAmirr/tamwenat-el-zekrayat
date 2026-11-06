# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""The Shopify "Sync Orders From" cutoff (``Marketplace Account.shopify_orders_sync_from``).

An order created before the cutoff is never imported as a new Marketplace Order, and an unlinked
one never gets a Sales Order. Both the cutoff and ``Marketplace Order.received_on`` (Shopify
``createdAt``, normalized by ``order_mapper``) are naive datetimes in the System Settings time
zone, so they compare directly. An empty cutoff changes nothing. An order created exactly at the
cutoff is imported.
"""

import frappe
from frappe.utils import get_datetime


class OrderBeforeSyncStart(Exception):
	"""``import_order()`` skipped an order created before the account's cutoff (no row written)."""


def get_sync_start(account):
	"""The account's cutoff as a datetime, or ``None`` when it is empty.

	``account`` is a Marketplace Account document, a dict-like row carrying
	``shopify_orders_sync_from``, or an account name.
	"""
	if isinstance(account, str):
		value = frappe.db.get_value("Marketplace Account", account, "shopify_orders_sync_from")
	else:
		value = account.get("shopify_orders_sync_from")
	return get_datetime(value) if value else None


def is_before_sync_start(account, received_on):
	"""True when ``received_on`` is strictly earlier than the account's cutoff."""
	cutoff = get_sync_start(account)
	if not cutoff or not received_on:
		return False
	return get_datetime(received_on) < cutoff


def clamp_listing_start(account, start):
	"""``max(start, cutoff)``: the listing never reaches back past the cutoff, so a sweep or a range
	import does not list (or fetch) the old backlog. Updates to rows that already exist and were
	made before the cutoff are not listed either; webhooks still refresh them."""
	cutoff = get_sync_start(account)
	start = get_datetime(start)
	return max(start, cutoff) if cutoff else start
