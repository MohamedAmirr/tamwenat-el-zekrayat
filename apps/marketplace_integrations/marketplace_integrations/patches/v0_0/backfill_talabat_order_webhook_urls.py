# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Persist the initial order-webhook URL for existing Talabat Connections."""

import frappe


def execute():
	"""Backfill only blank URLs; later domain changes require explicit refresh."""
	if not frappe.db.table_exists("Talabat Connection"):
		return

	for name in frappe.get_all(
		"Talabat Connection",
		filters={"order_webhook_url": ["in", ["", None]]},
		pluck="name",
	):
		connection = frappe.get_doc("Talabat Connection", name)
		connection.set_order_webhook_url_if_missing()
		connection.save(ignore_permissions=True)
