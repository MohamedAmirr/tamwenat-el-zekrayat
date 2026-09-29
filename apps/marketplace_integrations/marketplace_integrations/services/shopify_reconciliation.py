# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Read-only queries backing the Shopify Reconciliation dashboard's Script
Reports (#139 sections 4 and 6, converted from a hand-built custom Page to
native Frappe Script Reports, plus the general-purpose "Shopify Refunded
Orders" report, plus the "Shopify Reconciliation Summary" report that
replaced the dashboard's 5 Number Cards - see
marketplace_integrations/report/shopify_order_import_failures/,
.../report/shopify_refund_evidence_gaps/, .../report/shopify_refunded_orders/,
and .../report/shopify_reconciliation_summary/, whose execute() functions
call these same functions directly so there is one source of truth per
query): recent Order Import failures, Shopify orders with a refund status but
no recorded refund evidence, all Shopify orders with a refund status
regardless of evidence, and the reconciliation summary's 10 metrics. All are
computed fresh on every call - no caching, no new persisted field.

Both underlying doctypes (``Marketplace Sync Log``, ``Marketplace Order``)
restrict ``read`` to the System Manager role only (see their own DocType
permissions) - these functions require the same role explicitly, matching
``Marketplace Sync Log.resync()``'s ``frappe.only_for("System Manager")``,
rather than relying solely on ``frappe.get_all()``'s own permission
enforcement.
"""

import frappe
from frappe.utils import flt

from marketplace_integrations.integrations.shopify.webhooks import (
	MAX_WEBHOOK_DELIVERY_ATTEMPTS,
)

ORDER_IMPORT_FAILURES_LIMIT = 50
REFUND_EVIDENCE_GAPS_LIMIT = 50
REFUNDED_ORDERS_LIMIT = 50


def get_order_import_failures(limit=ORDER_IMPORT_FAILURES_LIMIT):
	"""Recent ``Marketplace Sync Log`` rows for a failed Order Import
	(#139 section 4), most recent first."""
	frappe.only_for("System Manager")
	return frappe.get_all(
		"Marketplace Sync Log",
		filters={"sync_type": "Order Import", "status": "Error"},
		fields=["name", "trigger_source", "attempted_at", "request_data", "traceback"],
		order_by="attempted_at desc",
		limit_page_length=limit,
	)


def get_refund_evidence_gaps(limit=REFUND_EVIDENCE_GAPS_LIMIT):
	"""Shopify Marketplace Order rows with a refund financial status but no
	recorded refund evidence (#139 section 6) - a live query, not a persisted
	field: ``platform="Shopify"``, ``external_financial_status`` in
	(REFUNDED, PARTIALLY_REFUNDED), and ``refunds`` empty.

	"Empty" is checked against the raw DB text representation - ``NULL``,
	``''``, or ``'[]'`` - confirmed against
	integrations/shopify/test_refund_notification.py's already-shipped
	``doc.refunds`` truthiness check (an explicit empty list is falsy/no
	refund there too). An OR across NULL/''/'[]' on one field, ordered and
	limited across the whole result, can't be expressed by
	frappe.get_all()'s AND-only filter dict - see
	docs/frappe-conventions.md's "prefer the ORM, raw SQL only when it
	genuinely can't express the query" guidance (same rationale as
	integrations/shopify/webhooks.py::retry_errored_webhook_deliveries()).
	"""
	frappe.only_for("System Manager")
	return frappe.db.sql(
		"""
		SELECT name, order_code, external_financial_status, last_platform_update_on
		FROM `tabMarketplace Order`
		WHERE platform = 'Shopify'
		  AND external_financial_status IN ('REFUNDED', 'PARTIALLY_REFUNDED')
		  AND (refunds IS NULL OR refunds = '' OR refunds = '[]')
		ORDER BY last_platform_update_on DESC
		LIMIT %(limit)s
		""",
		{"limit": limit},
		as_dict=True,
	)


def get_refunded_orders(from_date=None, to_date=None, limit=REFUNDED_ORDERS_LIMIT):
	"""ALL Shopify Marketplace Order rows with a refund financial status
	(the general "show me every refunded order" view), regardless of
	whether ``refunds`` evidence is populated - unlike
	get_refund_evidence_gaps() above, which only shows the subset missing
	evidence. Same base filter: ``platform="Shopify"``,
	``external_financial_status`` in (REFUNDED, PARTIALLY_REFUNDED).

	Optionally filtered to a ``last_platform_update_on`` date range - that's
	the field that changes when Shopify sends a refund update, unlike
	``received_on`` (the original order date, not relevant to "when was it
	refunded").

	``has_refund_evidence`` reuses the exact same NULL/''/'[]' emptiness
	test as get_refund_evidence_gaps() (just inverted), so the two reports
	can't disagree on what counts as "has evidence".
	"""
	frappe.only_for("System Manager")
	conditions = [
		"platform = 'Shopify'",
		"external_financial_status IN ('REFUNDED', 'PARTIALLY_REFUNDED')",
	]
	values = {"limit": limit}
	if from_date:
		conditions.append("DATE(last_platform_update_on) >= %(from_date)s")
		values["from_date"] = from_date
	if to_date:
		conditions.append("DATE(last_platform_update_on) <= %(to_date)s")
		values["to_date"] = to_date

	return frappe.db.sql(
		"""
		SELECT name, order_code, external_financial_status, last_platform_update_on,
			CASE WHEN (refunds IS NULL OR refunds = '' OR refunds = '[]') THEN 0 ELSE 1 END
				AS has_refund_evidence
		FROM `tabMarketplace Order`
		WHERE {conditions}
		ORDER BY last_platform_update_on DESC
		LIMIT %(limit)s
		""".format(conditions=" AND ".join(conditions)),
		values,
		as_dict=True,
	)


def _date_range_filters(fieldname, from_date=None, to_date=None):
	"""Return a list of frappe.get_all()-style range conditions on
	``fieldname``, only including the bounds actually supplied."""
	conditions = []
	if from_date:
		conditions.append([fieldname, ">=", from_date])
	if to_date:
		conditions.append([fieldname, "<=", to_date])
	return conditions


def get_catalog_push_error_count():
	"""Current-state count of ``Marketplace Product Mapping`` rows stuck at
	``sync_status = Error`` (#139 Number Card "Shopify Catalog Push Errors",
	now superseded by this function). Not date-ranged - this mirrors the
	Number Card's own current-state semantics exactly."""
	frappe.only_for("System Manager")
	return frappe.db.count("Marketplace Product Mapping", {"sync_status": "Error"})


def get_inventory_sync_error_count():
	"""Current-state count of ``Marketplace Product Mapping`` rows stuck at
	``inventory_sync_status = Error`` (#139 Number Card "Shopify Inventory
	Sync Errors"). Not date-ranged."""
	frappe.only_for("System Manager")
	return frappe.db.count("Marketplace Product Mapping", {"inventory_sync_status": "Error"})


def get_order_sync_error_count():
	"""Current-state count of ``Marketplace Order`` rows at
	``erpnext_sync_status = Sync Error`` (#139 Number Card "Marketplace
	Orders: Sync Error"). Not date-ranged."""
	frappe.only_for("System Manager")
	return frappe.db.count("Marketplace Order", {"erpnext_sync_status": "Sync Error"})


def get_webhook_delivery_error_count():
	"""Current-state count of ``Shopify Webhook Delivery`` rows at
	``status = Error`` (#139 Number Card "Shopify Webhook Deliveries: Error").
	Not date-ranged."""
	frappe.only_for("System Manager")
	return frappe.db.count("Shopify Webhook Delivery", {"status": "Error"})


def get_webhook_delivery_exhausted_retry_count():
	"""Current-state count of ``Shopify Webhook Delivery`` rows at
	``status = Error`` AND ``attempt_count >= MAX_WEBHOOK_DELIVERY_ATTEMPTS``
	(#139 Number Card "Shopify Webhook Deliveries: Exhausted Retries") - these
	will never self-heal via the hourly retry sweep, which excludes them by
	the same threshold. Not date-ranged."""
	frappe.only_for("System Manager")
	return frappe.db.count(
		"Shopify Webhook Delivery",
		{"status": "Error", "attempt_count": [">=", MAX_WEBHOOK_DELIVERY_ATTEMPTS]},
	)


def get_orders_received_count_in_range(from_date=None, to_date=None):
	"""Count of Shopify ``Marketplace Order`` rows whose ``received_on``
	(the original order date) falls in the given date range."""
	frappe.only_for("System Manager")
	filters = [["platform", "=", "Shopify"], *_date_range_filters("received_on", from_date, to_date)]
	return frappe.db.count("Marketplace Order", filters)


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
	frappe.only_for("System Manager")
	filters = [["platform", "=", "Shopify"], *_date_range_filters("received_on", from_date, to_date)]
	rows = frappe.get_all("Marketplace Order", filters=filters, fields=["order_total", "shop_currency"])

	breakdown = {}
	for row in rows:
		currency = row.shop_currency or "Unknown"
		breakdown[currency] = breakdown.get(currency, 0.0) + flt(row.order_total)

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
	have ``erpnext_sync_status = Synced`` (as opposed to ``Sync Error``,
	``Not Synced``, or ``Modified After Sync``).

	Returns ``{"rate": float | None, "numerator": int, "denominator": int}``.
	``rate`` is ``None`` (not a ZeroDivisionError) when no orders were
	received in range.
	"""
	frappe.only_for("System Manager")
	filters = [["platform", "=", "Shopify"], *_date_range_filters("received_on", from_date, to_date)]
	rows = frappe.get_all("Marketplace Order", filters=filters, fields=["erpnext_sync_status"])
	denominator = len(rows)
	numerator = len([row for row in rows if row.erpnext_sync_status == "Synced"])
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
	frappe.only_for("System Manager")
	filters = [
		["platform", "=", "Shopify"],
		["refunds", "is", "set"],
		["refunds", "!=", ""],
		["refunds", "!=", "[]"],
		*_date_range_filters("last_platform_update_on", from_date, to_date),
	]
	rows = frappe.get_all("Marketplace Order", filters=filters, fields=["refunds"])

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
	frappe.only_for("System Manager")
	filters = _date_range_filters("received_at", from_date, to_date)
	rows = frappe.get_all("Shopify Webhook Delivery", filters=filters, fields=["status"])
	denominator = len(rows)
	numerator = len([row for row in rows if row.status == "Processed"])
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
	frappe.only_for("System Manager")

	rows = [
		{
			"metric": "Shopify Catalog Push Errors (current)",
			"value": get_catalog_push_error_count(),
			"detail": "",
		},
		{
			"metric": "Shopify Inventory Sync Errors (current)",
			"value": get_inventory_sync_error_count(),
			"detail": "",
		},
		{
			"metric": "Marketplace Orders: Sync Error (current)",
			"value": get_order_sync_error_count(),
			"detail": "",
		},
		{
			"metric": "Shopify Webhook Deliveries: Error (current)",
			"value": get_webhook_delivery_error_count(),
			"detail": "",
		},
		{
			"metric": "Shopify Webhook Deliveries: Exhausted Retries (current)",
			"value": get_webhook_delivery_exhausted_retry_count(),
			"detail": f"attempt_count >= {MAX_WEBHOOK_DELIVERY_ATTEMPTS}",
		},
		{
			"metric": "Total Shopify Orders Received (in selected range)",
			"value": get_orders_received_count_in_range(from_date, to_date),
			"detail": "",
		},
	]

	order_value = get_total_order_value_in_range(from_date, to_date)
	rows.append(_money_metric_row("Total Order Value (in selected range)", order_value))

	sync_rate = get_sales_order_sync_success_rate_in_range(from_date, to_date)
	rows.append(_rate_metric_row("Sales Order Sync Success Rate (in selected range)", sync_rate))

	refund_total = get_total_refunded_amount_in_range(from_date, to_date)
	rows.append(_money_metric_row("Total Refunded Amount (in selected range)", refund_total))

	webhook_rate = get_webhook_processing_success_rate_in_range(from_date, to_date)
	rows.append(_rate_metric_row("Webhook Processing Success Rate (in selected range)", webhook_rate))

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
			"detail": f"Mixed currencies, not summed: {breakdown}",
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
			"detail": "0/0 - none in selected range",
		}
	return {
		"metric": metric,
		"value": round(rate_result["rate"], 2),
		"detail": f"{rate_result['numerator']}/{rate_result['denominator']}",
	}
