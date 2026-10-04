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

The channel/order-error counts here are an explicit PLACEHOLDER definition,
confined to ``get_placeholder_attention_counts()`` below so a later issue
(#169, which owns the canonical "needs attention" logic) can replace that one
function's body wholesale without touching ``get_channel_status()`` or the
report built on top of it. Don't grow this placeholder into something more
elaborate - see docs/marketplace-integrations/architecture.md and CLAUDE.md's
"Don't over-engineer" rule.
"""

import frappe

# Mirrors Marketplace Order.erpnext_sync_status's own Select options that
# represent "this order needs a human to look at it" - see
# marketplace_order.json.
PLACEHOLDER_ORDER_ERROR_STATUSES = ["Sync Error", "Modified After Sync"]

EMPTY_COUNTS = {"orders_needing_attention": 0, "product_sync_errors": 0, "import_errors": 0}


def get_channel_status():
	"""One row per readable Marketplace Account: platform-specific status
	fields, the placeholder attention counts, and an overall OK/Warning/
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
		return []

	account_names = [account.name for account in accounts]
	shopify_connection_health = _get_shopify_connection_health(accounts)
	counts_by_account = get_placeholder_attention_counts(account_names)

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
			# limited to "configuration complete" plus the placeholder error
			# counts above.
			"noon_warehouse_code": account.noon_warehouse_code,
			"noon_country_code": account.noon_country_code,
		}
		row["level"] = _compute_level(account, row, connection_health)
		rows.append(row)
	return rows


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


def get_placeholder_attention_counts(account_names):
	"""PLACEHOLDER "needs attention" counts, grouped per Marketplace
	Account via aggregate queries (never a per-account Python loop):

	- ``orders_needing_attention``: Marketplace Order rows whose
	  ``erpnext_sync_status`` is Sync Error or Modified After Sync.
	- ``product_sync_errors``: Marketplace Product Mapping rows whose
	  ``sync_status`` or ``inventory_sync_status`` is Error.
	- ``import_errors``: Marketplace Sync Log rows with ``status`` Error,
	  summed across every ``sync_type`` (the per-``sync_type`` breakdown is
	  kept under ``import_errors_by_type`` for a future detail view, but the
	  report column only needs the total).

	#169 owns the canonical "needs attention" definition and is expected to
	replace this function's body outright - every caller only depends on the
	returned shape (a dict keyed by Marketplace Account name).
	"""
	counts = {name: dict(EMPTY_COUNTS, import_errors_by_type={}) for name in account_names}
	if not account_names:
		return counts

	for row in frappe.get_list(
		"Marketplace Order",
		filters={
			"marketplace_account": ["in", account_names],
			"erpnext_sync_status": ["in", PLACEHOLDER_ORDER_ERROR_STATUSES],
		},
		fields=["marketplace_account", {"COUNT": "name", "as": "total"}],
		group_by="marketplace_account",
	):
		counts[row.marketplace_account]["orders_needing_attention"] = row.total

	for row in frappe.get_list(
		"Marketplace Product Mapping",
		filters={"marketplace_account": ["in", account_names]},
		or_filters={"sync_status": "Error", "inventory_sync_status": "Error"},
		fields=["marketplace_account", {"COUNT": "name", "as": "total"}],
		group_by="marketplace_account",
	):
		counts[row.marketplace_account]["product_sync_errors"] = row.total

	for row in frappe.get_list(
		"Marketplace Sync Log",
		filters={"marketplace_account": ["in", account_names], "status": "Error"},
		fields=["marketplace_account", "sync_type", {"COUNT": "name", "as": "total"}],
		group_by="marketplace_account, sync_type",
	):
		counts[row.marketplace_account]["import_errors"] += row.total
		counts[row.marketplace_account]["import_errors_by_type"][row.sync_type] = row.total

	return counts


def _compute_level(account, row, connection_health):
	"""PLACEHOLDER overall level, derived purely from the placeholder counts
	above plus each platform's own already-stored status fields - not a new
	source of truth. Kept intentionally small; see this module's docstring.
	"""
	if not account.enabled:
		return "Disabled"

	has_error = bool(row["orders_needing_attention"] or row["product_sync_errors"] or row["import_errors"])

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
