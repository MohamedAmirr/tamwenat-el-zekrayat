# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Shared Marketplace Account -> Warehouse validation.

Both `integrations/talabat/catalog_sync.py` (catalog sync) and
`services/sales_order_sync.py` (#85 Sales Order sync) need the configured
account Warehouse to actually be usable before building a stock/price/sales
payload from it. This is the one place that check lives, so the two call
sites cannot drift on wording or on which conditions are actually checked.
"""

import frappe
from frappe import _


def validate_account_warehouse(account_doc, context):
	"""Raise ``frappe.ValidationError`` unless ``account_doc.warehouse`` is a
	usable leaf Warehouse for ``context`` (a translated phrase describing the
	calling operation, e.g. "catalog sync" or "Sales Order synchronization").

	The Company match check only applies when ``account_doc.erpnext_company``
	is actually set - it is optional/blank for an account only used for
	catalog sync (see Marketplace Account's field docs), so an unset Company
	is not itself an error here; callers that require a Company validate that
	separately.
	"""
	if not account_doc.warehouse:
		frappe.throw(
			_("Marketplace Account {0} requires a Warehouse for {1}.").format(account_doc.name, context)
		)

	warehouse = frappe.db.get_value(
		"Warehouse", account_doc.warehouse, ["is_group", "disabled", "company"], as_dict=True
	)
	if not warehouse:
		frappe.throw(
			_("Marketplace Account {0}'s Warehouse {1} does not exist.").format(
				account_doc.name, account_doc.warehouse
			)
		)
	if warehouse.is_group:
		frappe.throw(
			_("Marketplace Account {0} requires a leaf Warehouse for {1}.").format(account_doc.name, context)
		)
	if warehouse.disabled:
		frappe.throw(
			_("Marketplace Account {0}'s Warehouse {1} is disabled.").format(
				account_doc.name, account_doc.warehouse
			)
		)
	if account_doc.erpnext_company and warehouse.company != account_doc.erpnext_company:
		frappe.throw(
			_(
				"Marketplace Account {0}'s Warehouse {1} belongs to Company {2}, not the account's "
				"configured Company {3}."
			).format(account_doc.name, account_doc.warehouse, warehouse.company, account_doc.erpnext_company)
		)
