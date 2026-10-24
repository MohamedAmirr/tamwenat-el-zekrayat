# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Script Report for #167 (Marketplace Channel Status) - one row per
Marketplace Account, replacing a Number Cards/Workspace HTML block surface
with a single report, matching the pattern #139 already settled on for
Shopify Reconciliation.

Calls services/marketplace_channels.py::get_channel_status() - the single
source of truth for every status field and the placeholder attention
counts, so this report has no query logic of its own.
"""

from frappe import _

from marketplace_integrations.services.marketplace_channels import get_channel_status


def execute(filters=None):
	columns = get_columns()
	data = get_channel_status()
	return columns, data


def get_columns():
	return [
		{
			"label": _("Account"),
			"fieldname": "account",
			"fieldtype": "Link",
			"options": "Marketplace Account",
			"width": 180,
		},
		{
			"label": _("Platform"),
			"fieldname": "platform",
			"fieldtype": "Data",
			"width": 90,
		},
		{
			"label": _("Enabled"),
			"fieldname": "enabled",
			"fieldtype": "Check",
			"width": 80,
		},
		{
			"label": _("Channel Status"),
			"fieldname": "level",
			"fieldtype": "Data",
			"width": 120,
		},
		{
			"label": _("Orders Needing Attention"),
			"fieldname": "orders_needing_attention",
			"fieldtype": "Int",
			"width": 160,
		},
		{
			"label": _("Product Sync Errors"),
			"fieldname": "product_sync_errors",
			"fieldtype": "Int",
			"width": 150,
		},
		{
			"label": _("Import Errors"),
			"fieldname": "import_errors",
			"fieldtype": "Int",
			"width": 120,
		},
		{
			"label": _("Fulfillment Errors"),
			"fieldname": "fulfillment_errors",
			"fieldtype": "Int",
			"width": 130,
		},
		{
			"label": _("Payment Issues"),
			"fieldname": "payment_errors",
			"fieldtype": "Int",
			"width": 120,
		},
		{
			"label": _("Last Checked"),
			"fieldname": "last_checked",
			"fieldtype": "Datetime",
			"width": 160,
		},
	]
