# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Script Report for #169 (Marketplace Needs Attention) - one row per
problem. Calls services/marketplace_attention.py::get_attention_items(), the
single definition of "needs attention", so this report has no query logic of
its own.
"""

import frappe
from frappe import _

from marketplace_integrations.services.marketplace_attention import get_attention_items


def execute(filters=None):
	rows = get_attention_items(filters)
	add_reference_labels(rows)
	return get_columns(), rows


def add_reference_labels(rows):
	"""#324: Frappe resolves link titles for Link columns only, not Dynamic Link, so give
	each row a ``reference_label`` (the referenced document's title, else its name) for the
	client to show. One permission-aware query per referenced doctype, not per row."""
	names_by_doctype = {}
	for row in rows:
		if row.get("reference_doctype") and row.get("reference_name"):
			names_by_doctype.setdefault(row["reference_doctype"], set()).add(row["reference_name"])

	titles = {}
	for doctype, names in names_by_doctype.items():
		meta = frappe.get_meta(doctype)
		if not (meta.show_title_field_in_link and meta.title_field):
			continue
		for doc in frappe.get_list(
			doctype,
			filters={"name": ["in", list(names)]},
			fields=["name", meta.title_field],
			limit_page_length=0,
		):
			if doc.get(meta.title_field):
				titles[(doctype, doc.name)] = doc.get(meta.title_field)

	for row in rows:
		row["reference_label"] = titles.get((row.get("reference_doctype"), row.get("reference_name")))


def get_columns():
	return [
		{"label": _("Problem"), "fieldname": "problem_type", "fieldtype": "Data", "width": 190},
		{"label": _("Platform"), "fieldname": "platform", "fieldtype": "Data", "width": 90},
		{
			"label": _("Account"),
			"fieldname": "account",
			"fieldtype": "Link",
			"options": "Marketplace Account",
			"width": 160,
		},
		{"label": _("Reference Type"), "fieldname": "reference_doctype", "fieldtype": "Data", "width": 150},
		{
			"label": _("Reference"),
			"fieldname": "reference_name",
			"fieldtype": "Dynamic Link",
			"options": "reference_doctype",
			"width": 150,
		},
		{"label": _("What Happened"), "fieldname": "explanation", "fieldtype": "Data", "width": 380},
		{"label": _("Detected"), "fieldname": "detected_at", "fieldtype": "Datetime", "width": 160},
		{"label": _("Next Action"), "fieldname": "next_action", "fieldtype": "Data", "width": 120},
	]
