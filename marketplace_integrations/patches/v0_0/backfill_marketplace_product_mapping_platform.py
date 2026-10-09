# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Backfill #168's ``Marketplace Product Mapping.platform`` for mappings that
already exist on this site before the field was added.

``platform`` is a read-only ``fetch_from: marketplace_account.platform`` field,
so any mapping inserted or saved after this migration gets it from Frappe's
own ``_validate_links()`` fetch. This patch fills only the rows that will not
necessarily be resaved soon, in one bulk ``UPDATE ... JOIN`` (no ``doc.save()``,
no ``modified`` bump, no interaction with ``mapping_identity_key``).

Idempotent: rows whose ``platform`` is already set are excluded by the WHERE
clause and never touched again on a later run.
"""

import frappe


def execute():
	if not frappe.db.table_exists("Marketplace Product Mapping"):
		return
	if not frappe.db.has_column("Marketplace Product Mapping", "platform"):
		return

	frappe.db.sql(
		"""
		UPDATE `tabMarketplace Product Mapping` AS mpm
		INNER JOIN `tabMarketplace Account` AS ma ON ma.name = mpm.marketplace_account
		SET mpm.platform = ma.platform
		WHERE (mpm.platform IS NULL OR mpm.platform = '')
			AND ma.platform IS NOT NULL
			AND ma.platform != ''
		"""
	)
	# Raw SQL bypasses Document and its per-document cache - clear the doctype's
	# whole cache so a later get_cached_doc() in the same migrate never returns a
	# pre-backfill copy (docs/frappe-conventions.md).
	frappe.clear_cache(doctype="Marketplace Product Mapping")
