# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Backfill #108's ``Marketplace Order.platform`` for orders that already
exist on this site before the field was added.

``platform`` is a read-only ``fetch_from: marketplace_account.platform``
field: Frappe's own ``_validate_links()`` re-fetches it on every future
``.insert()``/``.save()`` of a Marketplace Order (it always re-fetches when
the field is blank, and unconditionally since no ``fetch_if_empty`` is set on
it), so any order upserted or resaved after this migration lands already gets
it for free. This patch exists only to fill it in bulk for rows that will not
necessarily be resaved any time soon.

A single bulk ``UPDATE ... JOIN`` fills every Marketplace Order whose
``platform`` is still blank from its linked Marketplace Account, in one
statement - no ``doc.save()``, no per-row ORM round trip, and no interaction
with ``order_identity_key`` (computed only from ``marketplace_account``/
``external_order_id``, both left untouched here). Idempotent: a row whose
``platform`` is already set is excluded by the ``WHERE`` clause and is never
touched again on a later run.
"""

import frappe


def execute():
	if not frappe.db.table_exists("Marketplace Order"):
		return
	if not frappe.db.has_column("Marketplace Order", "platform"):
		return

	frappe.db.sql(
		"""
		UPDATE `tabMarketplace Order` AS mo
		INNER JOIN `tabMarketplace Account` AS ma ON ma.name = mo.marketplace_account
		SET mo.platform = ma.platform
		WHERE (mo.platform IS NULL OR mo.platform = '')
			AND ma.platform IS NOT NULL
			AND ma.platform != ''
		"""
	)
	# A raw SQL UPDATE bypasses Document and does not invalidate Frappe's
	# per-document cache (docs/frappe-conventions.md) - clear the whole
	# doctype's cache rather than one document at a time for a bulk update
	# like this, so a get_cached_doc() call anywhere later in the same
	# migrate run never returns a pre-backfill copy.
	frappe.clear_cache(doctype="Marketplace Order")
