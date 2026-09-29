# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Read-only queries backing the Shopify Reconciliation dashboard's three
Script Reports (#139 sections 4 and 6, converted from a hand-built custom
Page to native Frappe Script Reports, plus the general-purpose "Shopify
Refunded Orders" report - see
marketplace_integrations/report/shopify_order_import_failures/,
.../report/shopify_refund_evidence_gaps/, and
.../report/shopify_refunded_orders/, whose execute() functions call these
same functions directly so there is one source of truth per query): recent
Order Import failures, Shopify orders with a refund status but no recorded
refund evidence, and all Shopify orders with a refund status regardless of
evidence. All are computed fresh on every call - no caching, no new
persisted field.

Both underlying doctypes (``Marketplace Sync Log``, ``Marketplace Order``)
restrict ``read`` to the System Manager role only (see their own DocType
permissions) - these functions require the same role explicitly, matching
``Marketplace Sync Log.resync()``'s ``frappe.only_for("System Manager")``,
rather than relying solely on ``frappe.get_all()``'s own permission
enforcement.
"""

import frappe

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
