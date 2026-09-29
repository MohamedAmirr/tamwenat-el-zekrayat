# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Script Report for #139 section 6 (Shopify refund evidence gaps).

Calls services/shopify_reconciliation.py::get_refund_evidence_gaps() - the
same function the dashboard's custom Page used before being converted to
this native Frappe Script Report - so there is a single source of truth for
the underlying query instead of two implementations that could drift.
"""

from marketplace_integrations.services.shopify_reconciliation import (
	get_refund_evidence_gaps,
)


def execute(filters=None):
	columns = get_columns()
	data = get_refund_evidence_gaps()
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
	]
