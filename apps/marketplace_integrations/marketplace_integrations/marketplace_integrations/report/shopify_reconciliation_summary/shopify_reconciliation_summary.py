# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Script Report for #139 (Shopify Reconciliation Summary).

Replaces the "Shopify Reconciliation" Workspace's 5 Number Cards (Shopify
Catalog Push Errors, Shopify Inventory Sync Errors, Marketplace Orders: Sync
Error, Shopify Webhook Deliveries: Error, Shopify Webhook Deliveries:
Exhausted Retries) with one date-filterable report, because a Number Card
can't offer a live user-adjustable date range. Also adds 5 new business
metrics (orders received, order value, Sales Order sync success rate,
refunded amount, webhook processing success rate), all scoped to the
``from_date``/``to_date`` filter below.

Calls services/shopify_reconciliation.py::get_reconciliation_summary() - the
single source of truth for all 10 metrics, so this report has no query logic
of its own, matching the sibling shopify_order_import_failures/ report.

The dashboard's other two former Script Reports, "Shopify Refund Evidence
Gaps" and "Shopify Refunded Orders", were removed in favor of a "Show
Refunded Orders" Marketplace Order list view filter (redundant with this
report's metric #9, Total Refunded Amount) - see
docs/marketplace-integrations/shopify-reconciliation-dashboard.md.

Metrics 1-5 are current-state snapshots (same semantics as the Number Cards
they replace) and ignore this filter; metrics 6-10 respect it. Each row's
``metric`` label says "(current)" or "(in selected range)" so this isn't
misleading in the rendered report.
"""

from frappe import _

from marketplace_integrations.services.shopify_reconciliation import (
	get_reconciliation_summary,
)


def execute(filters=None):
	filters = filters or {}
	columns = get_columns()
	data = get_reconciliation_summary(
		from_date=filters.get("from_date"),
		to_date=filters.get("to_date"),
	)
	return columns, data


def get_columns():
	return [
		{
			"label": _("Metric"),
			"fieldname": "metric",
			"fieldtype": "Data",
			"width": 340,
		},
		{
			"label": _("Value"),
			"fieldname": "value",
			"fieldtype": "Data",
			"width": 140,
		},
		{
			"label": _("Detail"),
			"fieldname": "detail",
			"fieldtype": "Data",
			"width": 280,
		},
	]
