# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Backfill ``Marketplace Sync Log.marketplace_account`` for existing
``Order Import`` rows created before the field was added.

``_log_order_import_failure()`` (integrations/shopify/orders.py) now
populates ``marketplace_account`` directly on every new row going forward,
but every ``Order Import`` row written before that change only carries the
account as the ``"account"`` key inside its ``request_data`` JSON blob (see
that function's own ``frappe.as_json({"account": account.name, ...})``
call). This patch re-derives ``marketplace_account`` for those historical
rows from that stored JSON, leaving every other field untouched.

``Sales Order Sync`` rows are deliberately left alone: ``marketplace_account``
is optional (not ``reqd``) on the doctype, and those rows link to
``marketplace_order``, from which the account is already reachable
transitively - ``_create_sync_log()`` (services/sales_order_sync.py) was
never changed to populate this field and this patch does not second-guess
that decision.
"""

import json

import frappe


def execute():
	if not frappe.db.table_exists("Marketplace Sync Log"):
		return
	if not frappe.db.has_column("Marketplace Sync Log", "marketplace_account"):
		return

	rows = frappe.get_all(
		"Marketplace Sync Log",
		filters={
			"sync_type": "Order Import",
			"marketplace_account": ["in", ["", None]],
		},
		fields=["name", "request_data"],
	)

	for row in rows:
		if not row.request_data:
			continue

		try:
			request_data = json.loads(row.request_data)
		except (TypeError, ValueError):
			# Not valid JSON (or already a non-string type) - skip this row
			# rather than failing the whole patch over one malformed record.
			continue

		if not isinstance(request_data, dict):
			continue

		account = request_data.get("account")
		if not account or not isinstance(account, str):
			continue

		if not frappe.db.exists("Marketplace Account", account):
			# Don't backfill a Link to an account that no longer exists -
			# that would only create a dangling reference.
			continue

		frappe.db.set_value(
			"Marketplace Sync Log",
			row.name,
			"marketplace_account",
			account,
			update_modified=False,
		)
