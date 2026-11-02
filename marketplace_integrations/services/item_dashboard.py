# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Item dashboard override (#374): a "Marketplace" connection group on the Item form.

Registered as ``override_doctype_dashboards`` for Item in hooks.py. Frappe passes the dashboard
data ERPNext built (``Meta.get_dashboard_data``); this adds to it and never replaces ERPNext's own
``non_standard_fieldnames`` or ``transactions``.

Frappe counts connections with ``frappe.get_all`` (``get_doc_count``), which skips the mapping row
filter of the platform roles (#322): a Talabat User would see Noon listings in the badge. The
dashboard data therefore points ``method`` (a Frappe dashboard key the form calls instead of
``frappe.desk.notifications.get_open_count``) at ``get_open_count`` below, which runs Frappe's own
counting and only recounts the Marketplace entry through ``frappe.get_list``.
"""

import frappe
from frappe import _
from frappe.desk.notifications import get_open_count as frappe_get_open_count

MAPPING = "Marketplace Product Mapping"
OPEN_COUNT_METHOD = "marketplace_integrations.services.item_dashboard.get_open_count"
COUNT_LIMIT = 100  # same cap as Frappe's own connection counts


def get_data(data):
	non_standard_fieldnames = dict(data.get("non_standard_fieldnames") or {})
	non_standard_fieldnames[MAPPING] = "item"
	data["non_standard_fieldnames"] = non_standard_fieldnames

	transactions = list(data.get("transactions") or [])
	transactions.append({"label": _("Marketplace"), "items": [MAPPING]})
	data["transactions"] = transactions

	data["method"] = OPEN_COUNT_METHOD
	return data


@frappe.whitelist(methods=["GET"])
def get_open_count(doctype: str, name: str, items=None):
	"""Frappe's connection counts, with the Marketplace count limited to rows the user can read."""
	result = frappe_get_open_count(doctype, name, items)

	for link in (result.get("count") or {}).get("external_links_found", []):
		if link.get("doctype") == MAPPING:
			link["count"] = _visible_mapping_count(name)
	return result


def _visible_mapping_count(item):
	if not frappe.has_permission(MAPPING, "read"):
		return 0
	# get_list applies the #322 platform row filter, matching the rows of the Marketplace Listings tab.
	# Frappe sets a 1 s statement timeout for the counts and shows "?" for a slow one; this recount
	# runs under the same timeout, so it does the same instead of failing the whole request.
	try:
		return len(
			frappe.get_list(MAPPING, filters={"item": item}, pluck="name", limit=COUNT_LIMIT, order_by=None)
		)
	except Exception as e:
		if frappe.db.is_statement_timeout(e):
			return "?"
		raise
