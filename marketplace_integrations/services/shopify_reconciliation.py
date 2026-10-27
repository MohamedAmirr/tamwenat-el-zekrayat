# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Read-only queries backing the Shopify Reconciliation dashboard's Script
Reports (#139 section 4, converted from a hand-built custom Page to a
native Frappe Script Report, plus the "Shopify Reconciliation Summary"
report that replaced the dashboard's 5 Number Cards - see
marketplace_integrations/report/shopify_order_import_failures/ and
.../report/shopify_reconciliation_summary/, whose execute() functions call
these same functions directly so there is one source of truth per query):
recent Order Import failures and the reconciliation summary's metrics. All
are computed fresh on every call - no caching, no new persisted field.

Section 6 (Shopify orders with a refund status but no recorded refund
evidence) and its backing get_refund_evidence_gaps() were removed entirely
in favor of a "Show Refunded Orders" Marketplace Order list view filter -
see docs/marketplace-integrations/shopify-reconciliation-dashboard.md.

Authorization (#323): every function calls ``require_shopify_reader()``
(System Manager or Shopify User) and reads through ``frappe.get_list``, so
the session user's DocPerm and the platform row filter from
``permissions.py`` still apply on top of the role check. Order, mapping and
log reads are limited to Shopify accounts explicitly.
"""

import frappe
from frappe import _
from frappe.utils import flt, get_datetime, getdate

from marketplace_integrations.services.sync_status import CANCELLED, SYNC_ERROR, SYNCED
from marketplace_integrations.integrations.shopify.webhooks import (
	MAX_WEBHOOK_DELIVERY_ATTEMPTS,
)
from marketplace_integrations.services.marketplace_attention import get_resolved_import_failures

ORDER_IMPORT_FAILURES_LIMIT = 50
SHOPIFY_READER_ROLES = ("System Manager", "Shopify User")


def require_shopify_reader():
	"""Shopify reconciliation data is for System Manager and Shopify User only."""
	frappe.only_for(SHOPIFY_READER_ROLES)


def _shopify_accounts():
	"""Names of the Shopify Marketplace Accounts the session user may read."""
	return frappe.get_list(
		"Marketplace Account", filters={"platform": "Shopify"}, pluck="name", limit_page_length=0
	)


def _count(doctype, filters):
	"""Permission-aware row count (``frappe.db.count`` ignores permissions)."""
	rows = frappe.get_list(doctype, filters=filters, fields=[{"COUNT": "name", "as": "total"}])
	return rows[0].total if rows else 0


def get_order_import_failures(limit=ORDER_IMPORT_FAILURES_LIMIT):
	"""Recent ``Marketplace Sync Log`` rows for a failed Shopify Order Import
	(#139 section 4), most recent first. Other platforms' Order Import
	failures are excluded, and so are failures a later import of the same
	order already resolved (same rule as the Needs Attention report)."""
	require_shopify_reader()
	shopify_accounts = _shopify_accounts()
	if not shopify_accounts:
		return []
	rows = frappe.get_list(
		"Marketplace Sync Log",
		filters={
			"sync_type": "Order Import",
			"status": "Error",
			"marketplace_account": ["in", shopify_accounts],
		},
		fields=[
			"name",
			"marketplace_account",
			"trigger_source",
			"attempted_at",
			"request_data",
			"traceback",
			"creation",
		],
		order_by="attempted_at desc",
		limit_page_length=0,
	)
	resolved = get_resolved_import_failures(rows)
	return [row for row in rows if row.name not in resolved][:limit]


def _end_of_day(date_value):
	"""Return a Datetime for the last instant of ``date_value``'s calendar
	day, so a Date-typed "To Date" filter compared against a Datetime field
	(``received_on``, ``last_platform_update_on``, ``received_at``) includes
	every row on that day rather than only rows at/before midnight."""
	return get_datetime(f"{getdate(date_value)} 23:59:59.999999")


def _date_range_filters(fieldname, from_date=None, to_date=None):
	"""Return a list of frappe.get_all()-style range conditions on
	``fieldname``, only including the bounds actually supplied.

	``fieldname`` is always a Datetime field on the underlying doctype, while
	``from_date``/``to_date`` come from Date-typed report filters. The lower
	bound compares as-is (a bare date coerces to that day's midnight, which
	is exactly "From Date onwards"), but the upper bound is expanded to the
	end of ``to_date``'s day via ``_end_of_day()`` - otherwise a bare date
	string compared with ``<=`` against a Datetime column excludes every row
	timestamped later than midnight on the To Date itself, silently dropping
	that whole day from the range.
	"""
	conditions = []
	if from_date:
		conditions.append([fieldname, ">=", from_date])
	if to_date:
		conditions.append([fieldname, "<=", _end_of_day(to_date)])
	return conditions


def _mapping_count(filters):
	"""Count of Shopify Product Mapping rows only: a mapping carries no platform
	of its own, so it is limited to the Shopify accounts the user can read."""
	accounts = _shopify_accounts()
	if not accounts:
		return 0
	return _count("Marketplace Product Mapping", {**filters, "marketplace_account": ["in", accounts]})


def get_catalog_push_error_count():
	"""Current-state count of ``Marketplace Product Mapping`` rows stuck at
	``sync_status = Error`` (#139 Number Card "Shopify Catalog Push Errors",
	now superseded by this function). Not date-ranged - this mirrors the
	Number Card's own current-state semantics exactly."""
	require_shopify_reader()
	return _mapping_count({"sync_status": "Error"})


def get_inventory_sync_error_count():
	"""Current-state count of ``Marketplace Product Mapping`` rows stuck at
	``inventory_sync_status = Error`` (#139 Number Card "Shopify Inventory
	Sync Errors"). Not date-ranged."""
	require_shopify_reader()
	return _mapping_count({"inventory_sync_status": "Error"})


def get_order_sync_error_count():
	"""Current-state count of ``Marketplace Order`` rows at
	``erpnext_sync_status = Sync Error`` (#139 Number Card "Marketplace
	Orders: Sync Error", now "Shopify Orders: Sync Error"). Not date-ranged.
	Limited to orders of the Shopify accounts the user can read (by the account's
	platform, like the mapping counts), not the stored ``Order.platform``."""
	require_shopify_reader()
	accounts = _shopify_accounts()
	if not accounts:
		return 0
	return _count(
		"Marketplace Order",
		{"marketplace_account": ["in", accounts], "erpnext_sync_status": SYNC_ERROR},
	)


def get_webhook_delivery_error_count():
	"""Current-state count of ``Shopify Webhook Delivery`` rows at
	``status = Error`` (#139 Number Card "Shopify Webhook Deliveries: Error").
	Not date-ranged."""
	require_shopify_reader()
	return _count("Shopify Webhook Delivery", {"status": "Error"})


def get_webhook_delivery_exhausted_retry_count():
	"""Current-state count of ``Shopify Webhook Delivery`` rows at
	``status = Error`` AND ``attempt_count >= MAX_WEBHOOK_DELIVERY_ATTEMPTS``
	(#139 Number Card "Shopify Webhook Deliveries: Exhausted Retries") - these
	will never self-heal via the hourly retry sweep, which excludes them by
	the same threshold. Not date-ranged."""
	require_shopify_reader()
	return _count(
		"Shopify Webhook Delivery",
		{"status": "Error", "attempt_count": [">=", MAX_WEBHOOK_DELIVERY_ATTEMPTS]},
	)


def get_orders_received_count_in_range(from_date=None, to_date=None):
	"""Count of Shopify ``Marketplace Order`` rows whose ``received_on``
	(the original order date) falls in the given date range."""
	require_shopify_reader()
	filters = [["platform", "=", "Shopify"], *_date_range_filters("received_on", from_date, to_date)]
	return _count("Marketplace Order", filters)


def get_total_order_value_in_range(from_date=None, to_date=None):
	"""Sum of ``order_total`` for Shopify orders received in the given date
	range, grouped by ``shop_currency``.

	``order_total`` is always populated from Shopify's ``shopMoney`` amount
	(see integrations/shopify/order_mapper.py::_money()), i.e. it is always
	in that order's ``shop_currency`` - never ``presentment_currency``. If
	every order in range shares one ``shop_currency`` the sum is meaningful
	and returned directly; if more than one currency is present (e.g. orders
	from multiple Shopify shops/accounts with different home currencies), the
	amounts are NOT silently added together - instead each currency's own
	subtotal is returned in ``breakdown`` and ``is_mixed`` is set so callers
	can flag this instead of presenting a meaningless cross-currency sum.

	Returns ``{"total": float | None, "currency": str | None,
	"breakdown": {currency: float}, "is_mixed": bool}``.
	"""
	require_shopify_reader()
	filters = [["platform", "=", "Shopify"], *_date_range_filters("received_on", from_date, to_date)]
	rows = frappe.get_list(
		"Marketplace Order",
		filters=filters,
		fields=["shop_currency", {"SUM": "order_total", "as": "total"}],
		group_by="shop_currency",
		limit_page_length=0,
	)

	breakdown = {}
	for row in rows:
		currency = row.shop_currency or "Unknown"
		breakdown[currency] = breakdown.get(currency, 0.0) + flt(row.total)

	if not breakdown:
		return {"total": 0.0, "currency": None, "breakdown": {}, "is_mixed": False}
	if len(breakdown) > 1:
		return {"total": None, "currency": None, "breakdown": breakdown, "is_mixed": True}

	(currency, total), = breakdown.items()
	return {
		"total": total,
		"currency": None if currency == "Unknown" else currency,
		"breakdown": breakdown,
		"is_mixed": False,
	}


def get_sales_order_sync_success_rate_in_range(from_date=None, to_date=None):
	"""Of the Shopify orders received in the given date range, what fraction
	have ``erpnext_sync_status = Synced`` (as opposed to ``Syncing``, ``Needs Action``
	or ``Sync Error``). ``Cancelled`` orders (cancelled
	before any Sales Order was needed, #353) are left out of the denominator.

	Returns ``{"rate": float | None, "numerator": int, "denominator": int}``.
	``rate`` is ``None`` (not a ZeroDivisionError) when no orders were
	received in range.
	"""
	require_shopify_reader()
	filters = [
		["platform", "=", "Shopify"],
		["erpnext_sync_status", "!=", CANCELLED],
		*_date_range_filters("received_on", from_date, to_date),
	]
	rows = frappe.get_list(
		"Marketplace Order", filters=filters, fields=["erpnext_sync_status"], limit_page_length=0
	)
	denominator = len(rows)
	numerator = sum(1 for row in rows if row.erpnext_sync_status == SYNCED)
	rate = (numerator / denominator * 100) if denominator else None
	return {"rate": rate, "numerator": numerator, "denominator": denominator}


def get_total_refunded_amount_in_range(from_date=None, to_date=None):
	"""Sum of refund amounts for Shopify orders whose ``last_platform_update_on``
	falls in the given date range and whose ``refunds`` JSON is populated.

	Each element of the ``refunds`` list is a Shopify GraphQL ``Refund`` node
	(see integrations/shopify/test_orders.py::make_order_payload() and
	order_mapper.py, which persist it without interpretation); the refund
	amount is its ``totalRefundedSet.shopMoney.amount``. An order can have
	multiple refunds, so all of an order's refund amounts are summed.

	Like get_total_order_value_in_range() above, amounts are grouped by their
	own ``currencyCode`` and only summed directly when every refund in range
	shares one currency - a genuine mix is surfaced via ``is_mixed`` /
	``breakdown`` rather than silently added together.

	Returns ``{"total": float | None, "currency": str | None,
	"breakdown": {currency: float}, "is_mixed": bool}``.
	"""
	require_shopify_reader()
	filters = [
		["platform", "=", "Shopify"],
		["refunds", "is", "set"],
		["refunds", "!=", ""],
		["refunds", "!=", "[]"],
		*_date_range_filters("last_platform_update_on", from_date, to_date),
	]
	rows = frappe.get_list("Marketplace Order", filters=filters, fields=["refunds"], limit_page_length=0)

	breakdown = {}
	for row in rows:
		refunds = frappe.parse_json(row.refunds) or []
		for refund in refunds:
			money = (refund.get("totalRefundedSet") or {}).get("shopMoney") or {}
			amount = money.get("amount")
			if amount is None:
				continue
			currency = money.get("currencyCode") or "Unknown"
			breakdown[currency] = breakdown.get(currency, 0.0) + flt(amount)

	if not breakdown:
		return {"total": 0.0, "currency": None, "breakdown": {}, "is_mixed": False}
	if len(breakdown) > 1:
		return {"total": None, "currency": None, "breakdown": breakdown, "is_mixed": True}

	(currency, total), = breakdown.items()
	return {
		"total": total,
		"currency": None if currency == "Unknown" else currency,
		"breakdown": breakdown,
		"is_mixed": False,
	}


def get_webhook_processing_success_rate_in_range(from_date=None, to_date=None):
	"""Of ``Shopify Webhook Delivery`` rows whose ``received_at`` timestamp
	falls in the given date range, what fraction reached
	``status = Processed`` (as opposed to ``Error`` or still-pending
	``Received``).

	Returns ``{"rate": float | None, "numerator": int, "denominator": int}``.
	``rate`` is ``None`` (not a ZeroDivisionError) when no deliveries were
	received in range.
	"""
	require_shopify_reader()
	filters = _date_range_filters("received_at", from_date, to_date)
	rows = frappe.get_list("Shopify Webhook Delivery", filters=filters, fields=["status"], limit_page_length=0)
	denominator = len(rows)
	numerator = sum(1 for row in rows if row.status == "Processed")
	rate = (numerator / denominator * 100) if denominator else None
	return {"rate": rate, "numerator": numerator, "denominator": denominator}


def get_reconciliation_summary(from_date=None, to_date=None):
	"""Builds the 10 metric rows backing the "Shopify Reconciliation Summary"
	Script Report (#139), which replaces the dashboard's 5 Number Cards with
	one date-filterable report covering both those error/failure counts AND
	new business metrics.

	Metrics 1-5 (the former Number Cards) are current-state snapshots and
	deliberately IGNORE ``from_date``/``to_date`` - the Number Cards they
	replace were never date-scoped either. Metrics 6-10 are new and DO
	respect the date range. Each row's ``metric`` label says "(current)" or
	"(in selected range)" so this distinction is visible in the report
	itself, not just in this docstring.

	Returns a list of ``{"metric": str, "value": int | float | None,
	"detail": str}`` dicts, one per metric, in the order listed above.
	"""
	require_shopify_reader()

	rows = [
		{
			"metric": _("Shopify Catalog Push Errors (current)"),
			"value": get_catalog_push_error_count(),
			"detail": "",
		},
		{
			"metric": _("Shopify Inventory Sync Errors (current)"),
			"value": get_inventory_sync_error_count(),
			"detail": "",
		},
		{
			"metric": _("Shopify Orders: Sync Error (current)"),
			"value": get_order_sync_error_count(),
			"detail": "",
		},
		{
			"metric": _("Shopify Webhook Deliveries: Error (current)"),
			"value": get_webhook_delivery_error_count(),
			"detail": "",
		},
		{
			"metric": _("Shopify Webhook Deliveries: Exhausted Retries (current)"),
			"value": get_webhook_delivery_exhausted_retry_count(),
			"detail": f"attempt_count >= {MAX_WEBHOOK_DELIVERY_ATTEMPTS}",
		},
		{
			"metric": _("Total Shopify Orders Received (in selected range)"),
			"value": get_orders_received_count_in_range(from_date, to_date),
			"detail": "",
		},
	]

	order_value = get_total_order_value_in_range(from_date, to_date)
	rows.append(_money_metric_row(_("Total Order Value (in selected range)"), order_value))

	sync_rate = get_sales_order_sync_success_rate_in_range(from_date, to_date)
	rows.append(_rate_metric_row(_("Sales Order Sync Success Rate (in selected range)"), sync_rate))

	refund_total = get_total_refunded_amount_in_range(from_date, to_date)
	rows.append(_money_metric_row(_("Total Refunded Amount (in selected range)"), refund_total))

	webhook_rate = get_webhook_processing_success_rate_in_range(from_date, to_date)
	rows.append(_rate_metric_row(_("Webhook Processing Success Rate (in selected range)"), webhook_rate))

	return rows


def _money_metric_row(metric, money_result):
	"""Shared formatting for the two currency-bearing metrics
	(get_total_order_value_in_range() and get_total_refunded_amount_in_range()
	results), so a genuine currency mix is flagged identically for both
	instead of silently summed."""
	if money_result["is_mixed"]:
		breakdown = ", ".join(f"{amount:.2f} {currency}" for currency, amount in money_result["breakdown"].items())
		return {
			"metric": metric,
			"value": None,
			"detail": _("Mixed currencies, not summed: {0}").format(breakdown),
		}
	return {
		"metric": metric,
		"value": money_result["total"],
		"detail": money_result["currency"] or "",
	}


def _rate_metric_row(metric, rate_result):
	"""Shared formatting for the two success-rate metrics
	(get_sales_order_sync_success_rate_in_range() and
	get_webhook_processing_success_rate_in_range() results), including the
	zero-denominator case."""
	if rate_result["denominator"] == 0:
		return {
			"metric": metric,
			"value": None,
			"detail": _("0/0 - none in selected range"),
		}
	return {
		"metric": metric,
		"value": round(rate_result["rate"], 2),
		"detail": f"{rate_result['numerator']}/{rate_result['denominator']}",
	}
