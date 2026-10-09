# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Script Report for #139 section 4 (recent Shopify Order Import failures).

Calls services/shopify_reconciliation.py::get_order_import_failures() - the
same function the dashboard's custom Page used before being converted to
this native Frappe Script Report - so there is a single source of truth for
the underlying query instead of two implementations that could drift.
"""

from marketplace_integrations.services.shopify_reconciliation import (
	get_order_import_failures,
)


def execute(filters=None):
	columns = get_columns()
	data = get_order_import_failures()
	return columns, data


def get_columns():
	return [
		{
			"label": "Log",
			"fieldname": "name",
			"fieldtype": "Link",
			"options": "Marketplace Sync Log",
			"width": 180,
		},
		{
			"label": "Marketplace Account",
			"fieldname": "marketplace_account",
			"fieldtype": "Link",
			"options": "Marketplace Account",
			"width": 180,
		},
		{
			"label": "Trigger Source",
			"fieldname": "trigger_source",
			"fieldtype": "Data",
			"width": 150,
		},
		{
			"label": "Attempted At",
			"fieldname": "attempted_at",
			"fieldtype": "Datetime",
			"width": 160,
		},
		{
			"label": "Request Data",
			"fieldname": "request_data",
			"fieldtype": "Small Text",
			"width": 220,
		},
		{
			"label": "Error",
			"fieldname": "traceback",
			"fieldtype": "Small Text",
			"width": 320,
		},
	]
