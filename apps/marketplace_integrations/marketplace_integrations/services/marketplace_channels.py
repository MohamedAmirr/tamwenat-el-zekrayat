# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Marketplace Workspace & Channel Management (#167) - the one shared
service behind the "Marketplace Channel Status" Script Report and (once
built) the account-level "View" shortcuts on the Marketplace workspace.

Every query in this module uses ``frappe.get_list`` (never ``frappe.get_all``,
which bypasses permissions) - the whole result set is implicitly scoped to
whatever Marketplace Account / Marketplace Order / Marketplace Product
Mapping / Marketplace Sync Log / Shopify Connection rows the calling user may
read, rather than a hand-rolled permission check layered on top.

The order/import attention counts come from services/marketplace_attention.py
(#169) - the one definition of "needs attention" shared with the Marketplace
Needs Attention report. Only the product-mapping sync error count is computed
here, since mapping errors are not part of that definition.
"""

import frappe

from marketplace_integrations.services.marketplace_attention import (
	BOSTA_PLATFORM,
	get_attention_counts,
	get_attention_items,
)

EMPTY_COUNTS = {
	"orders_needing_attention": 0,
	"product_sync_errors": 0,
	"import_errors": 0,
	"fulfillment_errors": 0,
}


def get_channel_status():
	"""One row per readable Marketplace Account: platform-specific status
	fields, the attention counts, and an overall OK/Warning/
	Error/Disabled level. Returns plain dicts (JSON-serializable), not
	Document objects.
	"""
	accounts = frappe.get_list(
		"Marketplace Account",
		fields=[
			"name",
			"account_name",
			"platform",
			"enabled",
			"connection_doctype",
			"connection",
			"external_status",
			"external_status_reason",
			"external_status_until",
			"external_status_checked_on",
			"external_status_error",
			"shopify_location_gid",
			"shopify_location_name",
			"shopify_orders_reconciled_through",
			"noon_warehouse_code",
			"noon_country_code",
		],
		order_by="account_name asc",
	)
	if not accounts:
		return _bosta_rows()

	account_names = [account.name for account in accounts]
	shopify_connection_health = _get_shopify_connection_health(accounts)
	counts_by_account = get_account_counts(account_names)

	rows = []
	for account in accounts:
		counts = counts_by_account.get(account.name, EMPTY_COUNTS)
		connection_health = (
			shopify_connection_health.get(account.connection) if account.platform == "Shopify" else None
		)
		row = {
			"account": account.name,
			"account_name": account.account_name,
			"platform": account.platform,
			"enabled": account.enabled,
			"orders_needing_attention": counts["orders_needing_attention"],
			"product_sync_errors": counts["product_sync_errors"],
			"import_errors": counts["import_errors"],
			"fulfillment_errors": counts["fulfillment_errors"],
			"last_checked": account.external_status_checked_on if account.platform == "Talabat" else None,
			# Talabat
			"external_status": account.external_status,
			"external_status_reason": account.external_status_reason,
			"external_status_until": account.external_status_until,
			"external_status_checked_on": account.external_status_checked_on,
			"external_status_error": account.external_status_error,
			# Shopify
			"shopify_location_gid": account.shopify_location_gid,
			"shopify_location_name": account.shopify_location_name,
			"shopify_orders_reconciled_through": account.shopify_orders_reconciled_through,
			"webhook_registration_status": connection_health.get("webhook_registration_status")
			if connection_health
			else None,
			"webhook_missing_topics": connection_health.get("webhook_missing_topics")
			if connection_health
			else None,
			"last_webhook_error": connection_health.get("last_webhook_error") if connection_health else None,
			# Noon - no saved health/last-poll field exists yet (see
			# docs/marketplace-integrations/architecture.md); status here is
			# limited to "configuration complete" plus the counts above.
			"noon_warehouse_code": account.noon_warehouse_code,
			"noon_country_code": account.noon_country_code,
		}
		row["level"] = _compute_level(account, row, connection_health)
		rows.append(row)
	return rows + _bosta_rows()


def _bosta_rows():
	"""One carrier row for Bosta (not a Marketplace Account): shown when it is enabled or
	has shipments whose Delivery Note action failed. ``orders_needing_attention`` counts
	those shipments."""
	enabled = frappe.db.get_single_value("Bosta Settings", "enabled")
	failed = {item["reference_name"] for item in get_attention_items({"platform": BOSTA_PLATFORM})}
	if not enabled and not failed:
		return []
	return [
		{
			**EMPTY_COUNTS,
			"account": "",
			"account_name": "Bosta",
			"platform": BOSTA_PLATFORM,
			"enabled": enabled,
			"orders_needing_attention": len(failed),
			"last_checked": None,
			"level": "Error" if failed else ("OK" if enabled else "Disabled"),
		}
	]


def _get_shopify_connection_health(accounts):
	connection_names = list(
		{
			account.connection
			for account in accounts
			if account.platform == "Shopify"
			and account.connection_doctype == "Shopify Connection"
			and account.connection
		}
	)
	if not connection_names:
		return {}

	rows = frappe.get_list(
		"Shopify Connection",
		filters={"name": ["in", connection_names]},
		fields=["name", "webhook_registration_status", "webhook_missing_topics", "last_webhook_error"],
	)
	return {row.name: row for row in rows}


def get_account_counts(account_names):
	"""Per-account counts keyed by Marketplace Account name: the attention
	counts (``orders_needing_attention``, ``import_errors`` = Order Import
	errors only, ``fulfillment_errors``) from
	services/marketplace_attention.py, plus ``product_sync_errors``: Marketplace
	Product Mapping rows whose ``sync_status`` or ``inventory_sync_status`` is
	Error (one grouped query, never a per-account loop).
	"""
	counts = {name: dict(EMPTY_COUNTS, **row) for name, row in get_attention_counts(account_names).items()}
	if not account_names:
		return counts

	for row in frappe.get_list(
		"Marketplace Product Mapping",
		filters={"marketplace_account": ["in", account_names]},
		or_filters={"sync_status": "Error", "inventory_sync_status": "Error"},
		fields=["marketplace_account", {"COUNT": "name", "as": "total"}],
		group_by="marketplace_account",
	):
		counts[row.marketplace_account]["product_sync_errors"] = row.total

	return counts


def _compute_level(account, row, connection_health):
	"""Overall level, derived purely from the counts above plus each
	platform's own already-stored status fields - not a new
	source of truth. Kept intentionally small; see this module's docstring.
	"""
	if not account.enabled:
		return "Disabled"

	has_error = bool(
		row["orders_needing_attention"]
		or row["product_sync_errors"]
		or row["import_errors"]
		or row["fulfillment_errors"]
	)

	if account.platform == "Talabat":
		if account.external_status_error:
			return "Error"
		if account.external_status and account.external_status != "OPEN":
			return "Error" if has_error else "Warning"
	elif account.platform == "Shopify":
		if connection_health and connection_health.get("last_webhook_error"):
			return "Error"
		registration_status = (
			connection_health.get("webhook_registration_status") if connection_health else None
		)
		if registration_status == "Error":
			return "Error"
		if registration_status in ("Incomplete", "Not Checked"):
			return "Error" if has_error else "Warning"

	return "Error" if has_error else "OK"
