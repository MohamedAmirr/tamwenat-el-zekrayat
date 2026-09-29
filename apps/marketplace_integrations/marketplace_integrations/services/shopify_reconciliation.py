# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Read-only queries backing the Shopify Reconciliation dashboard's custom
Page sections that a Number Card can't express (#139 sections 4 and 6):
recent Order Import failures, and Shopify orders with refund evidence but no
recorded refund. Both are computed fresh on every call - no caching, no new
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
