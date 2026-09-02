# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Persist the initial catalog-feedback webhook URL and secret for existing
Talabat Connections.

Both fields are normally set together in ``TalabatConnection.before_insert()``
- but that hook never runs again for a connection that already existed before
the catalog webhook feature was added, so a pre-existing connection (matched
here by having a blank ``catalog_webhook_url``) would otherwise keep a
permanently blank ``catalog_webhook_secret`` too, causing every real Talabat
catalog feedback callback for it to be rejected as unauthenticated forever.
"""

import frappe


def execute():
	"""Backfill only blank URLs/secrets; later domain changes require explicit refresh."""
	if not frappe.db.table_exists("Talabat Connection"):
		return

	for name in frappe.get_all(
		"Talabat Connection",
		filters={"catalog_webhook_url": ["in", ["", None]]},
		pluck="name",
	):
		connection = frappe.get_doc("Talabat Connection", name)
		connection.set_catalog_webhook_url_if_missing()
		connection.set_catalog_webhook_secret_if_missing()
		connection.save(ignore_permissions=True)
