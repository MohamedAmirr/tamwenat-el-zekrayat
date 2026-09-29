# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Script Report for #139 (general-purpose "Shopify Refunded Orders" view).

Calls services/shopify_reconciliation.py::get_refunded_orders() - the same
pattern as the sibling shopify_order_import_failures/ and
shopify_refund_evidence_gaps/ reports, so the underlying query lives in one
place. Unlike "Shopify Refund Evidence Gaps" (which only shows the subset of
refunded orders MISSING ``refunds`` evidence), this report shows ALL Shopify
orders with a refund financial status regardless of whether ``refunds`` is
populated - the "show me every refunded order" view - with a boolean column
so evidence gaps are still visible at a glance.

Takes a native Frappe Report date-range filter (``from_date``/``to_date``)
on ``last_platform_update_on``, the field that changes when Shopify sends a
refund update (as opposed to ``received_on``, the original order date).
"""

from marketplace_integrations.services.shopify_reconciliation import (
	get_refunded_orders,
)


def execute(filters=None):
	filters = filters or {}
	columns = get_columns()
	data = get_refunded_orders(
		from_date=filters.get("from_date"),
		to_date=filters.get("to_date"),
	)
	return columns, data


def get_columns():
	return [
		{
			"label": "Order",
			"fieldname": "name",
			"fieldtype": "Link",
			"options": "Marketplace Order",
			"width": 180,
		},
		{
			"label": "Order Code",
			"fieldname": "order_code",
			"fieldtype": "Data",
			"width": 140,
		},
		{
			"label": "Financial Status",
			"fieldname": "external_financial_status",
			"fieldtype": "Data",
			"width": 160,
		},
		{
			"label": "Last Platform Update",
			"fieldname": "last_platform_update_on",
			"fieldtype": "Datetime",
			"width": 180,
		},
		{
			"label": "Has Refund Evidence",
			"fieldname": "has_refund_evidence",
			"fieldtype": "Check",
			"width": 140,
		},
	]
