# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Script Report for #169 (Marketplace Needs Attention) - one row per
problem. Calls services/marketplace_attention.py::get_attention_items(), the
single definition of "needs attention", so this report has no query logic of
its own.
"""

from marketplace_integrations.services.marketplace_attention import get_attention_items


def execute(filters=None):
	return get_columns(), get_attention_items(filters)


def get_columns():
	return [
		{"label": "Problem", "fieldname": "problem_type", "fieldtype": "Data", "width": 190},
		{"label": "Platform", "fieldname": "platform", "fieldtype": "Data", "width": 90},
		{
			"label": "Account",
			"fieldname": "account",
			"fieldtype": "Link",
			"options": "Marketplace Account",
			"width": 160,
		},
		{"label": "Reference Type", "fieldname": "reference_doctype", "fieldtype": "Data", "width": 150},
		{
			"label": "Reference",
			"fieldname": "reference_name",
			"fieldtype": "Dynamic Link",
			"options": "reference_doctype",
			"width": 150,
		},
		{"label": "What Happened", "fieldname": "explanation", "fieldtype": "Data", "width": 380},
		{"label": "Detected", "fieldname": "detected_at", "fieldtype": "Datetime", "width": 160},
		{"label": "Next Action", "fieldname": "next_action", "fieldtype": "Data", "width": 120},
	]
